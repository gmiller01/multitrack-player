#!/usr/bin/env python3
# multitrack_player_v18.py
# Multitrack Player v18 - Pulse/PipeWire (pactl) alapú routing
# - per-track mpv folyamat
# - sink_input keresés és move-sink-input (pactl)
# - mute/solo via pactl
# - project config: multitrack_config.json (bpm, playback_rate, tick_enabled)
# - global config: ~/.config/multitrack_player/config.json
#
# Fontos: os.environ["LC_NUMERIC"]="C" az elején

import os, sys, time, json, threading, subprocess, shlex
from pathlib import Path
from typing import List, Dict, Optional
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QFileDialog, QScrollArea, QComboBox, QSlider,
    QCheckBox, QLineEdit, QMessageBox, QSpinBox, QInputDialog, QSizePolicy
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QRectF, QPointF
from PyQt6.QtGui import QFontMetrics, QColor, QPainter, QPen

# locale fix
import locale
locale.setlocale(locale.LC_NUMERIC, "C")
os.environ["LC_NUMERIC"] = "C"

# constants
GLOBAL_CONFIG_DIR = Path.home() / ".config" / "multitrack_player"
GLOBAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
GLOBAL_CONFIG_FILE = GLOBAL_CONFIG_DIR / "config.json"
PROJECT_CONFIG_NAME = "multitrack_config.json"
AUDIO_EXTS = ('.wav', '.flac', '.ogg', '.mp3', '.m4a')
DEFAULT_BPM = 80
PLAYBACK_RATE_STEP = 0.05
PLAYBACK_RATE_MIN = 0.5
PLAYBACK_RATE_MAX = 2.0

# ---------------------------------------------------------
# helpers: pactl wrappers (simple, blocking subprocess calls)
# ---------------------------------------------------------
def pactl_list_sinks() -> List[Dict]:
    """Return list of sinks as dicts: {index, name, desc}"""
    try:
        out = subprocess.check_output(['pactl', 'list', 'short', 'sinks'], text=True)
    except Exception:
        return []
    sinks = []
    for line in out.strip().splitlines():
        parts = line.split('\t')
        if len(parts) >= 2:
            idx = parts[0]; name = parts[1]
            sinks.append({'index': int(idx), 'name': name})
    return sinks

def pactl_list_sink_inputs() -> List[Dict]:
    """Parse `pactl list sink-inputs` for indexes and properties"""
    try:
        out = subprocess.check_output(['pactl', 'list', 'sink-inputs'], text=True, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        return []
    sink_inputs = []
    cur = {}
    for ln in out.splitlines():
        ln = ln.rstrip()
        if ln.startswith('Sink Input #'):
            if cur:
                sink_inputs.append(cur)
            cur = {'index': int(ln.split('#')[-1]), 'props': {}}
        elif 'application.name' in ln:
            # format: application.name = "mpv"
            try:
                val = ln.split('=')[-1].strip().strip('"')
                cur['props']['application.name'] = val
            except Exception:
                pass
        elif 'media.name' in ln:
            try:
                val = ln.split('=')[-1].strip().strip('"')
                cur['props']['media.name'] = val
            except Exception:
                pass
        elif 'application.process.id' in ln:
            try:
                val = ln.split('=')[-1].strip().strip('"')
                cur['props']['process.id'] = val
            except Exception:
                pass
        # other lines ignored
    if cur:
        sink_inputs.append(cur)
    return sink_inputs

def pactl_move_sink_input(sink_input_idx: int, sink_name: str) -> bool:
    try:
        subprocess.check_call(['pactl', 'move-sink-input', str(sink_input_idx), sink_name])
        return True
    except Exception as e:
        print(f"[pactl] move-sink-input failed: {e}")
        return False

def pactl_set_sink_input_mute(sink_input_idx: int, mute: bool) -> bool:
    try:
        subprocess.check_call(['pactl', 'set-sink-input-mute', str(sink_input_idx), '1' if mute else '0'])
        return True
    except Exception as e:
        print(f"[pactl] set-sink-input-mute failed: {e}")
        return False

def pactl_set_sink_input_volume(sink_input_idx: int, percent: int) -> bool:
    try:
        vol_arg = f'{percent}%'
        subprocess.check_call(['pactl', 'set-sink-input-volume', str(sink_input_idx), vol_arg])
        return True
    except Exception as e:
        print(f"[pactl] set-sink-input-volume failed: {e}")
        return False

# ---------------------------------------------------------
# TrackProcess: launches mpv as a subprocess for each track
# ---------------------------------------------------------
class TrackProcess:
    def __init__(self, path: Path, playback_rate: float = 1.0):
        self.path = Path(path)
        self.proc: Optional[subprocess.Popen] = None
        self.sink_input_idx: Optional[int] = None
        self.desired_sink: Optional[str] = None  # pactl sink name
        self.volume_pct = 100
        self.muted = False
        self.playback_rate = playback_rate
        self._start_lock = threading.Lock()
        self._start_process()

    def _start_process(self):
        # start mpv in paused mode (we will seek/play via --start and --no-resume-playback)
        cmd = [
            'mpv', '--no-video', '--really-quiet',
            '--play-dir=no', '--pause', '--input-ipc-server=/tmp/mtp-mpv-{}'.format(os.getpid()),
            '--term-status-msg', 'MPTV', str(self.path)
        ]
        # ensure mpv doesn't try to open GUI or similar
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            print(f"[TrackProcess] Failed to start mpv for {self.path}: {e}")
            self.proc = None

    def is_running(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        try:
            if self.proc:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=1.0)
                except Exception:
                    self.proc.kill()
        except Exception:
            pass
        self.proc = None
        self.sink_input_idx = None

    def _refresh_sink_input(self, retries=10, delay=0.12):
        """Try to find sink_input idx for this mpv process by process id or media.name"""
        for attempt in range(retries):
            sis = pactl_list_sink_inputs()
            pid = None
            if self.proc:
                pid = self.proc.pid
            for si in sis:
                props = si.get('props', {})
                # match by process id
                if pid and 'process.id' in props and str(pid) == props.get('process.id'):
                    self.sink_input_idx = si['index']; return True
                # match by media.name containing file name
                mname = props.get('media.name','')
                if mname and self.path.name in mname:
                    self.sink_input_idx = si['index']; return True
            time.sleep(delay)
        return False

    def move_to_sink(self, sink_name: str):
        self.desired_sink = sink_name
        # ensure sink_input exists
        ok = self._refresh_sink_input()
        if not ok:
            print(f"[AudioRouting] Could not find sink_input for {self.path.name}")
            return False
        # try move (with a few retries)
        for i in range(6):
            try:
                if pactl_move_sink_input(self.sink_input_idx, sink_name):
                    print(f"[AudioRouting] Moved {self.path.name} -> {sink_name}")
                    return True
            except Exception:
                pass
            time.sleep(0.12 * (i+1))
        print(f"[AudioRouting] Failed to move {self.path.name} to {sink_name}")
        return False

    def set_volume(self, pct: int):
        self.volume_pct = pct
        if self.sink_input_idx is None:
            self._refresh_sink_input()
        if self.sink_input_idx:
            pactl_set_sink_input_volume(self.sink_input_idx, pct)

    def set_mute(self, mute: bool):
        self.muted = bool(mute)
        if self.sink_input_idx is None:
            self._refresh_sink_input()
        if self.sink_input_idx:
            pactl_set_sink_input_mute(self.sink_input_idx, self.muted)

    def play(self, start_pos: float = 0.0):
        # use mpv --start to jump or via input-ipc? Simpler: kill and restart mpv with --start
        with self._start_lock:
            self.stop()
            cmd = [
                'mpv', '--no-video', '--really-quiet',
                f'--start={start_pos}', f'--speed={self.playback_rate}',
                str(self.path)
            ]
            try:
                self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:
                print(f"[TrackProcess] play start failed: {e}")
                self.proc = None
            # after start, try to move sink_input if desired sink set
            if self.desired_sink:
                threading.Thread(target=self.move_to_sink, args=(self.desired_sink,), daemon=True).start()

    def seek(self, seconds: float):
        # easiest approach: restart at desired position
        self.play(start_pos=seconds)

# ---------------------------------------------------------
# Tick player: use paplay (Pulse) or a short mpv
# ---------------------------------------------------------
class TickPlayer:
    def __init__(self, tick_file: Optional[str]=None, vol_pct: int = 100):
        self.tick_file = tick_file
        self.vol_pct = vol_pct

    def set_tick_file(self, f: Optional[str]):
        self.tick_file = f

    def play_tick(self, device_sink: Optional[str] = None):
        # use paplay -> routes through Pulse (device selection via pactl move if needed)
        if self.tick_file and Path(self.tick_file).exists():
            try:
                # start paplay (it will create a sink_input); if device_sink provided, move it
                p = subprocess.Popen(['paplay', self.tick_file], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if device_sink:
                    # try to find sink_input for paplay process and move it
                    pid = p.pid
                    # small thread to move after creation
                    def mover():
                        for _ in range(12):
                            sis = pactl_list_sink_inputs()
                            for si in sis:
                                props = si.get('props', {})
                                if 'process.id' in props and str(pid) == props.get('process.id'):
                                    pactl_move_sink_input(si['index'], device_sink)
                                    return
                            time.sleep(0.06)
                    threading.Thread(target=mover, daemon=True).start()
                return True
            except Exception as e:
                print(f"[Tick] paplay failed: {e}")
        # fallback: simple beep via Python (may not route well)
        print("[Tick] No tick file or paplay failed; fallback beep skipped.")
        return False

# ---------------------------------------------------------
# Small Timeline widget (progress + loop visualization)
# ---------------------------------------------------------
from PyQt6.QtWidgets import QWidget
class Timeline(QWidget):
    seekRequested = pyqtSignal(float)
    loopChanged = pyqtSignal(float, float)

    def __init__(self, duration=10.0):
        super().__init__()
        self.duration = float(max(1.0, duration))
        self.position = 0.0
        self.loop_start = 0.0
        self.loop_end = self.duration
        self.setMinimumHeight(64)
        self.loop_pulse_active = False
        self.prog_pulse_active = False
        self.pulse_phase = 0.0
        self.pulse_speed = 2.0

    def set_duration(self, d: float):
        self.duration = max(1.0, float(d)); self.update()

    def set_position(self, pos: float):
        self.position = max(0.0, min(pos, self.duration)); self.update()

    def set_loop(self, s: float, e: float):
        self.loop_start = max(0.0, s); self.loop_end = min(self.duration, e); self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        r = self.rect()
        w, h = r.width(), r.height()
        p.fillRect(r, QColor("#333333"))
        bar_h = 14
        bar_y = int(h/2 - bar_h/2)
        # base bar
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#2f2f2f")); p.drawRoundedRect(QRectF(10, bar_y, max(10, w-20), bar_h), 4.0, 4.0)
        def x_for(t): return 10 + (w-20) * (t / max(1.0, self.duration))
        lsx, lex = x_for(self.loop_start), x_for(self.loop_end)
        # loop rect
        p.setBrush(QColor(120,120,120,150)); p.drawRect(QRectF(lsx, bar_y, max(4, lex-lsx), bar_h))
        # progress
        posx = x_for(self.position)
        p.setBrush(QColor(80,180,80)); p.drawRect(QRectF(10, bar_y, max(2, posx-10), bar_h))
        # handles
        p.setBrush(QColor("#bbbbbb")); p.setPen(QPen(QColor("#888888")))
        p.drawRect(QRectF(lsx-6, bar_y-4, 12, bar_h+8)); p.drawRect(QRectF(lex-6, bar_y-4, 12, bar_h+8))
        p.setPen(QPen(QColor("#fff"), 2)); p.drawLine(QPointF(posx, bar_y-6), QPointF(posx, bar_y+bar_h+6))

    # mouse handling omitted for brevity in this sample (we keep earlier behaviour in full version)

# ---------------------------------------------------------
# TrackRow widget: UI per track
# ---------------------------------------------------------
from PyQt6.QtWidgets import QHBoxLayout
class TrackRow(QWidget):
    def __init__(self, filepath: str, sinks: List[Dict], settings: Dict):
        super().__init__()
        self.filepath = filepath
        self.sinks = sinks
        self.settings = settings or {}
        self.player: Optional[TrackProcess] = None
        self.sink_name = None
        self._build_ui()
        self.load_settings()

    def _build_ui(self):
        h = QHBoxLayout(); self.setLayout(h)
        self.label = QLabel(Path(self.filepath).name); h.addWidget(self.label, 3)
        self.vol_slider = QSlider(Qt.Orientation.Horizontal); self.vol_slider.setRange(0,120); self.vol_slider.setValue(100); self.vol_slider.setFixedWidth(260)
        h.addWidget(QLabel("Vol")); h.addWidget(self.vol_slider)
        self.vol_label = QLabel("100%"); h.addWidget(self.vol_label)
        self.mute_cb = QCheckBox("Mute"); self.solo_cb = QCheckBox("Solo")
        h.addWidget(self.mute_cb); h.addWidget(self.solo_cb)
        h.addStretch()
        self.sink_combo = QComboBox(); self.sink_combo.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.sink_combo.addItem("default", "default")
        for s in self.sinks:
            name = s.get('name')
            display = name if len(name) < 48 else (name[:45] + "...")
            self.sink_combo.addItem(display, name)
        h.addWidget(self.sink_combo)
        self.test_btn = QPushButton("Test"); self.test_btn.setFixedWidth(56); h.addWidget(self.test_btn)

        # signals
        self.vol_slider.valueChanged.connect(self._vol_changed)
        self.mute_cb.stateChanged.connect(self._mute_changed)
        self.solo_cb.stateChanged.connect(self._solo_changed)
        self.sink_combo.currentIndexChanged.connect(self._sink_changed)
        self.test_btn.clicked.connect(self._on_test)

    def load_settings(self):
        n = Path(self.filepath).name
        ent = self.settings.get(n, {})
        vol = ent.get('volume', 100); self.vol_slider.setValue(vol); self.vol_label.setText(f"{int(vol)}%")
        sink = ent.get('sink', None)
        if sink:
            idx = self.sink_combo.findData(sink)
            if idx != -1:
                self.sink_combo.setCurrentIndex(idx)

    def set_player(self, player: TrackProcess):
        self.player = player
        # apply stored sink if any
        chosen = self.sink_combo.currentData()
        if chosen:
            self.sink_name = chosen
            player.desired_sink = chosen
            threading.Thread(target=player.move_to_sink, args=(chosen,), daemon=True).start()
        player.set_volume(self.vol_slider.value())
        player.set_mute(self.mute_cb.isChecked())

    def _vol_changed(self, v):
        self.vol_label.setText(f"{int(v)}%")
        if self.player:
            self.player.set_volume(v)

    def _mute_changed(self, state):
        if self.player:
            self.player.set_mute(state == Qt.CheckState.Checked)

    def _solo_changed(self, state):
        # solo logic implemented in main window (needs access to all rows)
        pass

    def _sink_changed(self, idx):
        data = self.sink_combo.currentData()
        self.sink_name = data
        if self.player:
            self.player.desired_sink = data
            threading.Thread(target=self.player.move_to_sink, args=(data,), daemon=True).start()

    def _on_test(self):
        # play short test tone via paplay on default and then move if sink selected
        chosen = self.sink_combo.currentData()
        # create a short wav via ffmpeg or use built-in beep file? we'll use "paplay /usr/share/sounds/alsa/Front_Center.wav"
        try:
            p = subprocess.Popen(['paplay', '/usr/share/sounds/alsa/Front_Center.wav'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if chosen and chosen != "default":
                # move sink_input to chosen
                pid = p.pid
                def mover():
                    for _ in range(12):
                        sis = pactl_list_sink_inputs()
                        for si in sis:
                            props = si.get('props', {})
                            if 'process.id' in props and str(pid) == props.get('process.id'):
                                pactl_move_sink_input(si['index'], chosen)
                                return
                        time.sleep(0.06)
                threading.Thread(target=mover, daemon=True).start()
        except Exception as e:
            QMessageBox.warning(self, "Test failed", f"Test tone failed: {e}")

# ---------------------------------------------------------
# MainWindow: glue everything together
# ---------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Multitrack Player v18 (Pulse/PipeWire routing)")
        self.resize(1100, 800)
        self.setStyleSheet("QWidget { background-color: #2f2f2f; color: #e6e6e6 }")
        self.global_cfg = self._load_global_cfg()
        if 'default_project_folder' not in self.global_cfg:
            self.global_cfg['default_project_folder'] = str(Path.home())
        if 'tick_file' not in self.global_cfg:
            self.global_cfg['tick_file'] = ""
        self.tick_player = TickPlayer(self.global_cfg.get('tick_file',''))
        self.device_sinks = pactl_list_sinks()
        self.current_folder: Optional[Path] = None
        self.project_settings = {}
        self.track_rows: List[TrackRow] = []
        self.track_players: List[TrackProcess] = []
        self.timeline = Timeline(10.0)
        self._build_ui()
        self.ui_timer = QTimer(); self.ui_timer.setInterval(120); self.ui_timer.timeout.connect(self._ui_tick); self.ui_timer.start()

    def _load_global_cfg(self):
        try:
            if GLOBAL_CONFIG_FILE.exists():
                return json.loads(GLOBAL_CONFIG_FILE.read_text())
        except Exception:
            pass
        return {}

    def _save_global_cfg(self):
        try:
            GLOBAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            GLOBAL_CONFIG_FILE.write_text(json.dumps(self.global_cfg, indent=2))
        except Exception as e:
            print("Failed to save global config:", e)

    def _build_ui(self):
        central = QWidget(); self.setCentralWidget(central); v = QVBoxLayout(); central.setLayout(v)
        top = QHBoxLayout()
        self.open_btn = QPushButton("Open Folder"); top.addWidget(self.open_btn)
        self.default_btn = QPushButton("Set Default Folder"); top.addWidget(self.default_btn)
        self.settings_btn = QPushButton("Settings"); top.addWidget(self.settings_btn)
        self.folder_label = QLabel("No folder"); top.addWidget(self.folder_label, 1)
        self.refresh_btn = QPushButton("Refresh Outputs"); top.addWidget(self.refresh_btn)
        self.save_btn = QPushButton("Save Settings"); top.addWidget(self.save_btn)
        v.addLayout(top)

        v.addWidget(self.timeline)
        transport = QHBoxLayout()
        self.play_btn = QPushButton("Play"); transport.addWidget(self.play_btn)
        self.stop_btn = QPushButton("Stop"); transport.addWidget(self.stop_btn)
        transport.addWidget(QLabel("Playback rate:"))
        self.rate_minus = QPushButton("–"); self.rate_plus = QPushButton("+")
        self.rate_minus.setFixedWidth(28); self.rate_plus.setFixedWidth(28)
        transport.addWidget(self.rate_minus); self.rate_label = QLabel("100%"); transport.addWidget(self.rate_label); transport.addWidget(self.rate_plus)
        v.addLayout(transport)

        loop_h = QHBoxLayout()
        self.loop_toggle = QCheckBox("Loop On"); loop_h.addWidget(self.loop_toggle)
        self.loop_save = QPushButton("Save Loop"); loop_h.addWidget(self.loop_save)
        self.loop_delete = QPushButton("Delete Loop"); loop_h.addWidget(self.loop_delete)
        self.loop_select = QComboBox(); loop_h.addWidget(self.loop_select, 1)
        loop_h.addWidget(QLabel("BPM:"))
        self.bpm_spin = QSpinBox(); self.bpm_spin.setRange(30,300); self.bpm_spin.setValue(DEFAULT_BPM); loop_h.addWidget(self.bpm_spin)
        loop_h.addWidget(QLabel("Tick Enabled:"))
        self.tick_enabled_cb = QCheckBox("Tick"); loop_h.addWidget(self.tick_enabled_cb)
        loop_h.addWidget(QLabel("Tick (global):"))
        self.tick_label = QLineEdit(); self.tick_label.setReadOnly(True); self.tick_label.setText(self.global_cfg.get('tick_file','')); loop_h.addWidget(self.tick_label,2)
        self.tick_browse = QPushButton("Browse"); loop_h.addWidget(self.tick_browse)
        v.addLayout(loop_h)

        self.tracks_area = QScrollArea(); self.tracks_widget = QWidget(); self.tracks_layout = QVBoxLayout(); self.tracks_widget.setLayout(self.tracks_layout)
        self.tracks_area.setWidgetResizable(True); self.tracks_area.setWidget(self.tracks_widget); v.addWidget(self.tracks_area)

        # connections
        self.open_btn.clicked.connect(self.on_open)
        self.default_btn.clicked.connect(self.on_set_default_folder)
        self.settings_btn.clicked.connect(self.on_settings)
        self.refresh_btn.clicked.connect(self.on_refresh)
        #self.save_btn.clicked.connect(self.on_save)
        self.save_btn.clicked.connect(self._save_project_settings)
        self.play_btn.clicked.connect(self.on_play)
        self.stop_btn.clicked.connect(self.on_stop)
        self.rate_plus.clicked.connect(self.on_rate_plus)
        self.rate_minus.clicked.connect(self.on_rate_minus)
        self.loop_save.clicked.connect(self.on_save_loop)
        self.loop_delete.clicked.connect(self.on_delete_loop)
        self.loop_select.currentIndexChanged.connect(self.on_loop_selected)
        self.tick_browse.clicked.connect(self.on_browse_tick)

    # ---------------------------
    # folder open / load
    # ---------------------------
    def on_set_default_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Select default project folder", self.global_cfg.get('default_project_folder', str(Path.home())))
        if d:
            self.global_cfg['default_project_folder'] = d; self._save_global_cfg(); QMessageBox.information(self, "Saved", f"Default folder set to {d}")

    def on_settings(self):
        # simple settings: set default folder & tick file (reuse file dialog)
        d = QFileDialog.getExistingDirectory(self, "Select default project folder", self.global_cfg.get('default_project_folder', str(Path.home())))
        if d:
            self.global_cfg['default_project_folder'] = d
        f = QFileDialog.getOpenFileName(self, "Select tick sound file (global)", str(Path.home()), "Audio files (*.wav *.ogg *.mp3 *.flac)")[0]
        if f:
            self.global_cfg['tick_file'] = f; self.tick_label.setText(f)
        self._save_global_cfg()

    def on_open(self):
        start = self.global_cfg.get('default_project_folder', str(Path.home()))
        folder = QFileDialog.getExistingDirectory(self, "Select project folder", start)
        if not folder: return
        self.current_folder = Path(folder); self.folder_label.setText(str(self.current_folder))
        # load project config
        p = self.current_folder / PROJECT_CONFIG_NAME
        if p.exists():
            try:
                self.project_settings = json.loads(p.read_text())
            except Exception:
                self.project_settings = {}
        else:
            self.project_settings = {}
        pg = self.project_settings.get('_global', {})
        self.project_bpm = pg.get('bpm', DEFAULT_BPM)
        self.project_playback_rate = pg.get('playback_rate', 1.0)
        self.bpm_spin.setValue(self.project_bpm)
        self.rate_label.setText(f"{int(round(self.project_playback_rate*100))}%")
        self.tick_enabled_cb.setChecked(pg.get('tick_enabled', True))
        # load sinks
        self.device_sinks = pactl_list_sinks()
        # load tracks
        self._load_tracks(folder)

    def _load_tracks(self, folder):
        # cleanup
        for p in self.track_players:
            p.stop()
        for r in self.track_rows:
            try: r.deleteLater()
            except: pass
        self.track_players = []; self.track_rows = []
        files = [p for p in sorted(Path(folder).iterdir()) if p.suffix.lower() in AUDIO_EXTS]
        # build sink list for UI
        sinks = self.device_sinks
        for fpath in files:
            tr = TrackRow(str(fpath), sinks, settings=self.project_settings)
            self.tracks_layout.addWidget(tr)
            self.track_rows.append(tr)
            # create TrackProcess
            tp = TrackProcess(fpath, playback_rate=self.project_playback_rate)
            self.track_players.append(tp)
            tr.set_player(tp)
        self.tracks_layout.addStretch(1)
        # set timeline duration to the longest file (quick heuristic using ffprobe could be added)
        # For now set default duration
        self.timeline.set_duration(300.0)

    def on_refresh(self):
        self.device_sinks = pactl_list_sinks()
        for r in self.track_rows:
            cur = r.sink_combo.currentData()
            r.sink_combo.blockSignals(True)
            r.sink_combo.clear()
            r.sink_combo.addItem("default", "default")
            for s in self.device_sinks:
                name = s.get('name'); display = name if len(name)<48 else (name[:45]+"...")
                r.sink_combo.addItem(display, name)
            if cur:
                idx = r.sink_combo.findData(cur)
                if idx != -1:
                    r.sink_combo.setCurrentIndex(idx)
            r.sink_combo.blockSignals(False)

    # ---------------------------
    # loops/save
    # ---------------------------
    def on_save_loop(self):
        name, ok = QInputDialog.getText(self, "Save loop", "Loop name:")
        if not ok or not name: return
        start, end = float(self.timeline.loop_start), float(self.timeline.loop_end)
        if '_global' not in self.project_settings: self.project_settings['_global'] = {}
        loops = self.project_settings['_global'].get('loops', {})
        loops[name] = [start, end]
        self.project_settings['_global']['loops'] = loops
        self._save_project_settings()

    def on_delete_loop(self):
        data = self.loop_select.currentData()
        if not data: QMessageBox.information(self, "Info", "No loop selected"); return
        name = data
        loops = self.project_settings.get('_global', {}).get('loops', {})
        if name in loops: del loops[name]; self.project_settings['_global']['loops'] = loops; self._save_project_settings()
        self._populate_loops()

    def _populate_loops(self):
        self.loop_select.blockSignals(True); self.loop_select.clear()
        self.loop_select.addItem("Select loop...", None)
        loops = self.project_settings.get('_global', {}).get('loops', {})
        for name in loops:
            self.loop_select.addItem(name, name)
        self.loop_select.blockSignals(False)

    def on_loop_selected(self, idx):
        data = self.loop_select.currentData()
        if not data: return
        loops = self.project_settings.get('_global', {}).get('loops', {})
        rng = loops.get(data)
        if rng:
            self.timeline.set_loop(rng[0], rng[1])

    def _save_project_settings(self):
        if not self.current_folder: return
        # collect per-track settings
        data = {}
        for i, r in enumerate(self.track_rows):
            key = Path(r.filepath).name
            data[key] = {'sink': r.sink_name, 'volume': r.vol_slider.value(), 'mute': r.mute_cb.isChecked(), 'solo': r.solo_cb.isChecked()}
        data['_global'] = {
            'loops': self.project_settings.get('_global', {}).get('loops', {}),
            'last_used_loop': getattr(self, 'current_loop_name', None),
            'bpm': int(self.bpm_spin.value()),
            'tick_enabled': bool(self.tick_enabled_cb.isChecked()),
            'playback_rate': float(self.project_playback_rate)
        }
        try:
            with open(self.current_folder / PROJECT_CONFIG_NAME, 'w') as f:
                json.dump(data, f, indent=2)
            QMessageBox.information(self, "Saved", "Project settings saved.")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed saving project settings: {e}")

    # ---------------------------
    # playback controls
    # ---------------------------
    def on_play(self):
        if not self.track_players: return
        start = 0.0
        if self.loop_toggle.isChecked():
            start = self.timeline.loop_start
        # tick pre-count
        if self.tick_enabled_cb.isChecked():
            ticks = 4
            bpm = int(self.bpm_spin.value())
            beat_interval = 60.0 / bpm
            tick_sink = None  # currently use default
            for i in range(ticks):
                self.tick_player.play_tick(device_sink=tick_sink)
                time.sleep(beat_interval)
        # apply settings and start tracks
        for i, tp in enumerate(self.track_players):
            row = self.track_rows[i]
            if row.sink_name:
                tp.desired_sink = row.sink_name
            tp.set_volume(row.vol_slider.value())
            tp.set_mute(row.mute_cb.isChecked())
            tp.play(start_pos=start)

        # solo handling: if any solo checked, mute others
        solos = [r for r in self.track_rows if r.solo_cb.isChecked()]
        if solos:
            for i, r in enumerate(self.track_rows):
                if not r.solo_cb.isChecked():
                    self.track_players[i].set_mute(True)

    def on_stop(self):
        for tp in self.track_players:
            tp.stop()

    def on_rate_plus(self):
        new = round(self.project_playback_rate + PLAYBACK_RATE_STEP, 3)
        if new > PLAYBACK_RATE_MAX: new = PLAYBACK_RATE_MAX
        self.project_playback_rate = new
        self.rate_label.setText(f"{int(round(new*100))}%")
        for tp in self.track_players:
            tp.playback_rate = new

    def on_rate_minus(self):
        new = round(self.project_playback_rate - PLAYBACK_RATE_STEP, 3)
        if new < PLAYBACK_RATE_MIN: new = PLAYBACK_RATE_MIN
        self.project_playback_rate = new
        self.rate_label.setText(f"{int(round(new*100))}%")
        for tp in self.track_players:
            tp.playback_rate = new

    def on_browse_tick(self):
        f = QFileDialog.getOpenFileName(self, "Select tick file (global)", str(Path.home()), "Audio files (*.wav *.ogg *.mp3 *.flac)")[0]
        if not f: return
        self.global_cfg['tick_file'] = f; self.tick_label.setText(f); self._save_global_cfg()
        self.tick_player.set_tick_file(f)

    # ---------------------------
    # UI tick update
    # ---------------------------
    def _ui_tick(self):
        # refresh sinks list occasionally
        if int(time.time()) % 10 == 0:
            self.device_sinks = pactl_list_sinks()
        # attempt to refresh sink_input indexes for players (background moving)
        for tp in self.track_players:
            if tp.desired_sink and (tp.sink_input_idx is None):
                # try move in background
                threading.Thread(target=tp.move_to_sink, args=(tp.desired_sink,), daemon=True).start()

    def closeEvent(self, ev):
        for tp in self.track_players:
            tp.stop()
        self._save_global_cfg()
        return super().closeEvent(ev)

# ---------------------------------------------------------
# main
# ---------------------------------------------------------
def main():
    app = QApplication(sys.argv)
    mw = MainWindow()
    mw.show()
    sys.exit(app.exec())

if __name__ == '__main__':
    main()
