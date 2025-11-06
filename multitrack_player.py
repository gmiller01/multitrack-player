#!/usr/bin/env python3
# Multitrack Player v6 - PyQt6
# - Global transport, per-track outputs, per-track volume/mute/solo
# - Global pitch, global loop (named), settings saved to multitrack_settings.json
# - Native file dialog, dark theme
# - Custom Timeline widget: shows progress, allows seeking by dragging, shows loop region
#   with draggable start/end handles.
# Requirements:
#   sudo apt install mpv pulseaudio-utils rubberband-cli rubberband-ladspa
#   python3 -m pip install PyQt6 python-mpv
#
import locale
locale.setlocale(locale.LC_NUMERIC, "C")

import os, sys, json, subprocess, time, math
from pathlib import Path
from functools import partial

from PyQt6.QtWidgets import QApplication, QWidget, QMainWindow, QFileDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QSlider, QScrollArea, QMessageBox, QComboBox, QCheckBox, QDoubleSpinBox, QInputDialog
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QRectF, QPointF
from PyQt6.QtGui import QPainter, QColor, QPen, QBrush

try:
    import mpv
except Exception:
    mpv = None

SETTINGS_NAME = "multitrack_settings.json"
AUDIO_EXTS = ('.wav', '.mp3', '.flac', '.ogg', '.m4a')

def list_pactl_sinks():
    try:
        out = subprocess.check_output(['pactl', 'list', 'short', 'sinks'], text=True)
    except Exception:
        return []
    sinks = []
    for line in out.strip().splitlines():
        parts = line.split('\t')
        if len(parts) >= 2:
            idx = parts[0]; name = parts[1]; desc = parts[-1]
            sinks.append((idx, name, desc))
    return sinks

# --- TrackPlayer (same as v5) ---
class TrackPlayer:
    def __init__(self, file_path, sink_name=None):
        self.file_path = str(file_path)
        self.sink_name = sink_name
        self.player = None
        self.is_playing = False
        self.create_player()

    def create_player(self, volume=100, pitch=1.0):
        opts = {'no_video': True, 'volume': volume, 'keep-open': True, 'msg-level': 'all=no'}
        try:
            self.player = mpv.MPV(**opts) if mpv else None
        except Exception:
            try:
                self.player = mpv.MPV() if mpv else None
                if self.player:
                    self.player.volume = volume
            except Exception:
                self.player = None

    def set_sink(self, sink_name):
        self.sink_name = sink_name
        if not self.player:
            return
        try:
            self.player.set_property('audio-device', f"pulse/{sink_name}")
        except Exception:
            pass

    def play(self, start_pos=0.0, speed=1.0):
        if not self.player:
            self.create_player()
        try:
            self.player.play(self.file_path)
            if start_pos and start_pos > 0.0:
                time.sleep(0.02)
                try:
                    self.player.seek(start_pos, reference='absolute')
                except Exception:
                    pass
            try:
                self.player.speed = speed
            except Exception:
                pass
            self.is_playing = True
        except Exception as e:
            print("Play error:", e)
            self.is_playing = False

    def stop(self):
        if self.player:
            try:
                self.player.stop()
            except Exception:
                pass
        self.is_playing = False

    def pause(self):
        if self.player:
            try:
                self.player.pause = True
            except Exception:
                pass
        self.is_playing = False

    def resume(self):
        if self.player:
            try:
                self.player.pause = False
            except Exception:
                pass
        self.is_playing = True

    def set_volume(self, vol):
        if self.player:
            try:
                self.player.volume = vol
            except Exception:
                pass

    def set_pitch_filter(self, pitch):
        if not self.player:
            return False
        af_str = f"rubberband=pitch-scale={pitch}"
        try:
            try:
                self.player.command('af', 'add', af_str)
                return True
            except Exception:
                pass
            try:
                self.player.set_property('af', af_str)
                return True
            except Exception:
                return False
        except Exception:
            return False

    def get_time_pos(self):
        if not self.player:
            return 0.0
        try:
            pos = self.player.time_pos
            return float(pos) if pos else 0.0
        except Exception:
            return 0.0

    def seek(self, seconds):
        if not self.player:
            return
        try:
            self.player.seek(seconds, reference='absolute')
        except Exception:
            pass

    def get_duration(self):
        if not self.player:
            return 0.0
        try:
            d = self.player.duration
            return float(d) if d else 0.0
        except Exception:
            return 0.0

# --- Timeline widget ---
class Timeline(QWidget):
    seekRequested = pyqtSignal(float)
    loopChanged = pyqtSignal(float, float)

    def __init__(self, duration=1.0):
        super().__init__()
        self.duration = max(1.0, duration)
        self.position = 0.0
        self.loop_start = 0.0
        self.loop_end = self.duration
        self.dragging = None  # 'pos', 'start', 'end'
        self.setMinimumHeight(48)
        self.setMouseTracking(True)

    def set_duration(self, d):
        self.duration = max(1.0, d)
        if self.loop_end > self.duration:
            self.loop_end = self.duration
        self.update()

    def set_position(self, pos):
        self.position = max(0.0, min(pos, self.duration))
        self.update()

    def set_loop(self, start, end):
        self.loop_start = max(0.0, min(start, self.duration))
        self.loop_end = max(self.loop_start, min(end, self.duration))
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        rect = self.rect()
        w = rect.width(); h = rect.height()
        # background
        p.fillRect(rect, QColor("#3a3a3a"))
        # progress bar background
        bar_h = 12; bar_y = h//2 - bar_h//2
        bar_rect = QRectF(10, bar_y, w-20, bar_h)
        p.setBrush(QColor("#2f2f2f"))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(bar_rect, 4, 4)
        # loop region
        def x_for(t):
            return 10 + (w-20) * (t / self.duration)
        lsx = x_for(self.loop_start); lex = x_for(self.loop_end)
        loop_rect = QRectF(lsx, bar_y, max(4, lex-lsx), bar_h)
        p.setBrush(QColor(80, 160, 200, 120))
        p.drawRect(loop_rect)
        # filled progress up to current pos
        posx = x_for(self.position)
        prog_rect = QRectF(10, bar_y, max(2, posx-10), bar_h)
        p.setBrush(QColor("#66bb6a"))
        p.drawRect(prog_rect)
        # draw handles for loop start/end
        handle_w = 10; handle_h = 18
        p.setBrush(QColor("#bbbbbb"))
        p.setPen(QPen(QColor("#888888")))
        p.drawRect(lsx-handle_w/2, bar_y-handle_h/2+bar_h/2, handle_w, handle_h)
        p.drawRect(lex-handle_w/2, bar_y-handle_h/2+bar_h/2, handle_w, handle_h)
        # draw playhead
        p.setPen(QPen(QColor("#ffffff"), 2))
        p.drawLine(posx, bar_y-6, posx, bar_y+bar_h+6)
        # time labels
        p.setPen(QPen(QColor("#e6e6e6")))
        p.drawText(10, bar_y-8, f"{self._fmt(self.position)}")
        p.drawText(w-80, bar_y-8, f"{self._fmt(self.duration)}")

    def _fmt(self, t):
        m = int(t // 60); s = int(t % 60); return f"{m:02d}:{s:02d}"

    def mousePressEvent(self, ev):
        x = ev.position().x(); w = self.rect().width()
        def t_from_x(xv): return (xv - 10) / (w-20) * self.duration
        # check if near handles
        lsx = 10 + (w-20) * (self.loop_start / self.duration)
        lex = 10 + (w-20) * (self.loop_end / self.duration)
        if abs(x - lsx) < 12:
            self.dragging = 'start'; return
        if abs(x - lex) < 12:
            self.dragging = 'end'; return
        # else set position and start dragging playhead
        self.dragging = 'pos'
        newt = max(0.0, min(self.duration, t_from_x(x)))
        self.set_position(newt)
        self.seekRequested.emit(newt)

    def mouseMoveEvent(self, ev):
        if not self.dragging:
            return
        x = ev.position().x(); w = self.rect().width()
        def t_from_x(xv): return (xv - 10) / (w-20) * self.duration
        t = max(0.0, min(self.duration, t_from_x(x)))
        if self.dragging == 'pos':
            self.set_position(t)
            self.seekRequested.emit(t)
        elif self.dragging == 'start':
            # ensure start <= end
            new_start = min(t, self.loop_end - 0.01)
            self.loop_start = max(0.0, new_start)
            self.loopChanged.emit(self.loop_start, self.loop_end)
            self.update()
        elif self.dragging == 'end':
            new_end = max(t, self.loop_start + 0.01)
            self.loop_end = min(self.duration, new_end)
            self.loopChanged.emit(self.loop_start, self.loop_end)
            self.update()

    def mouseReleaseEvent(self, ev):
        self.dragging = None

# --- TrackRow and MainWindow similar to v5 but using PyQt6 widgets and Timeline ---
class TrackRow(QWidget):
    def __init__(self, filename, sinks, assigned_sink=None, settings=None):
        super().__init__()
        self.filename = filename
        self.sinks = sinks
        self.assigned_sink = assigned_sink
        self.settings = settings or {}
        self.player = TrackPlayer(filename, sink_name=assigned_sink)
        self.current_volume = 100
        self.muted = False
        self.solo = False
        self.init_ui()
        self.load_settings()

    def init_ui(self):
        layout = QHBoxLayout(); self.setLayout(layout)
        self.name_label = QLabel(Path(self.filename).name); layout.addWidget(self.name_label,3)
        self.sink_combo = QComboBox()
        for s in self.sinks: self.sink_combo.addItem(s[1], s[1])
        if self.assigned_sink:
            idx = self.sink_combo.findData(self.assigned_sink)
            if idx != -1: self.sink_combo.setCurrentIndex(idx)
        layout.addWidget(self.sink_combo,2)
        # volume slider
        self.vol_slider = QSlider(Qt.Orientation.Horizontal); self.vol_slider.setRange(0,200); self.vol_slider.setValue(100)
        layout.addWidget(QLabel("Vol")); layout.addWidget(self.vol_slider,1)
        self.vol_label = QLabel("100%"); layout.addWidget(self.vol_label)
        self.mute_btn = QPushButton("Mute"); self.mute_btn.setCheckable(True); layout.addWidget(self.mute_btn)
        self.solo_btn = QPushButton("Solo"); self.solo_btn.setCheckable(True); layout.addWidget(self.solo_btn)
        # connections
        self.sink_combo.currentIndexChanged.connect(self.on_sink_changed)
        self.vol_slider.valueChanged.connect(self.on_volume_changed)
        self.mute_btn.toggled.connect(self.on_mute_toggled)
        self.solo_btn.toggled.connect(self.on_solo_toggled)

    def load_settings(self):
        s = self.settings or {}
        name = Path(self.filename).name
        entry = s.get(name, {})
        vol = entry.get('volume', 100)
        self.vol_slider.setValue(int(vol))
        self.current_volume = int(vol)
        self.muted = entry.get('muted', False)
        self.solo = entry.get('solo', False)
        self.mute_btn.setChecked(self.muted)
        self.solo_btn.setChecked(self.solo)

    def save_settings(self):
        return {'sink': self.sink_combo.currentData(), 'volume': self.current_volume, 'muted': self.muted, 'solo': self.solo}

    def on_sink_changed(self, idx):
        sink = self.sink_combo.currentData()
        self.player.set_sink(sink)

    def on_volume_changed(self, val):
        self.current_volume = int(val)
        self.vol_label.setText(f"{val}%")
        try: self.player.set_volume(self.current_volume)
        except Exception: pass

    def on_mute_toggled(self, state):
        self.muted = bool(state)

    def on_solo_toggled(self, state):
        self.solo = bool(state)

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Multitrack Player v6 - PyQt6")
        self.resize(1100, 780)
        self.setStyleSheet("QWidget { background-color: #2f2f2f; color: #e6e6e6; } QPushButton { background-color: #444444; } QLineEdit, QComboBox { background-color: #3a3a3a; }")
        self.sinks = list_pactl_sinks()
        self.settings = {}
        self.current_folder = None
        self.track_rows = []
        self.global_players = []
        self.global_pitch = 1.0
        self.loop_list = []
        self.current_loop = None

        central = QWidget(); self.setCentralWidget(central); v = QVBoxLayout(); central.setLayout(v)
        top = QHBoxLayout(); v.addLayout(top)
        self.open_btn = QPushButton("Open Folder"); top.addWidget(self.open_btn)
        self.folder_label = QLabel("No folder selected"); top.addWidget(self.folder_label,1)
        self.refresh_btn = QPushButton("Refresh Outputs"); top.addWidget(self.refresh_btn)
        self.save_btn = QPushButton("Save Settings"); top.addWidget(self.save_btn)
        # timeline & transport
        timeline_h = QVBoxLayout(); v.addLayout(timeline_h)
        self.timeline = Timeline(duration=10.0); timeline_h.addWidget(self.timeline)
        transport = QHBoxLayout(); timeline_h.addLayout(transport)
        self.play_btn = QPushButton("Play"); transport.addWidget(self.play_btn)
        self.pause_btn = QPushButton("Pause"); transport.addWidget(self.pause_btn)
        self.stop_btn = QPushButton("Stop"); transport.addWidget(self.stop_btn)
        transport.addWidget(QLabel("Pitch")); self.pitch_slider = QSlider(Qt.Orientation.Horizontal); self.pitch_slider.setRange(50,200); self.pitch_slider.setValue(100); transport.addWidget(self.pitch_slider,2); self.pitch_label = QLabel("1.00x"); transport.addWidget(self.pitch_label)
        loop_h = QHBoxLayout(); v.addLayout(loop_h)
        loop_h.addWidget(QLabel("Loop:")); loop_h.addWidget(self.loop_editor := QWidget(),3)  # placeholder for visual alignment
        # create a separate loop editor controls (we'll reuse timeline's loop region)
        self.loop_toggle = QCheckBox("Loop On"); loop_h.addWidget(self.loop_toggle)
        self.loop_save_btn = QPushButton("Save Loop"); loop_h.addWidget(self.loop_save_btn)
        self.loop_select = QComboBox(); loop_h.addWidget(self.loop_select,1)
        # tracks area
        self.tracks_area = QScrollArea(); self.tracks_widget = QWidget(); self.tracks_layout = QVBoxLayout(); self.tracks_widget.setLayout(self.tracks_layout); self.tracks_area.setWidgetResizable(True); self.tracks_area.setWidget(self.tracks_widget); v.addWidget(self.tracks_area)
        # signals
        self.open_btn.clicked.connect(self.on_open); self.refresh_btn.clicked.connect(self.on_refresh); self.save_btn.clicked.connect(self.on_save)
        self.play_btn.clicked.connect(self.on_play); self.pause_btn.clicked.connect(self.on_pause); self.stop_btn.clicked.connect(self.on_stop)
        self.pitch_slider.valueChanged.connect(self.on_pitch_changed); self.loop_save_btn.clicked.connect(self.on_save_loop); self.loop_select.currentIndexChanged.connect(self.on_loop_selected)
        # timeline signals
        self.timeline.seekRequested.connect(self.on_seek_requested); self.timeline.loopChanged.connect(self.on_loop_changed)
        # ui timer
        self.ui_timer = QTimer(); self.ui_timer.setInterval(100); self.ui_timer.timeout.connect(self.ui_tick); self.ui_timer.start()

    def on_open(self):
        folder = QFileDialog.getExistingDirectory(self, "Select folder with audio files", str(Path.home()))
        if not folder: return
        self.current_folder = Path(folder); self.folder_label.setText(folder)
        settings_path = self.current_folder / SETTINGS_NAME
        if settings_path.exists():
            try:
                with open(settings_path, 'r') as f: self.settings = json.load(f)
            except Exception: self.settings = {}
        else:
            self.settings = {}
        self.load_tracks(folder)

    def load_tracks(self, folder):
        for r in self.track_rows:
            try: r.player.stop(); r.deleteLater()
            except Exception: pass
        self.track_rows = []; self.global_players = []
        self.sinks = list_pactl_sinks()
        files = [p for p in sorted(Path(folder).iterdir()) if p.suffix.lower() in AUDIO_EXTS]
        for p in files:
            assigned = None; s = self.settings.get(p.name, {})
            if s: assigned = s.get('sink')
            row = TrackRow(str(p), self.sinks, assigned_sink=assigned, settings=self.settings)
            self.tracks_layout.addWidget(row); self.track_rows.append(row); self.global_players.append(row.player)
        self.tracks_layout.addStretch(1)
        global_settings = self.settings.get('_global', {})
        if global_settings:
            self.global_pitch = global_settings.get('pitch', 1.0); self.pitch_slider.setValue(int(self.global_pitch * 100))
            loops = global_settings.get('loops', [])
            if loops: self.loop_list = loops; self.update_loop_dropdown()
        if self.global_players:
            dur = self.global_players[0].get_duration()
            if dur and dur > 0: self.timeline.set_duration(dur)

    def on_refresh(self):
        self.sinks = list_pactl_sinks()
        for row in self.track_rows:
            cur = row.sink_combo.currentData(); row.sink_combo.clear()
            for s in self.sinks: row.sink_combo.addItem(s[1], s[1])
            if cur:
                idx = row.sink_combo.findData(cur)
                if idx != -1: row.sink_combo.setCurrentIndex(idx)

    def apply_mute_solo_logic(self):
        solos = [r for r in self.track_rows if r.solo]
        if solos:
            for r in self.track_rows:
                if r.solo and not r.muted:
                    try: r.player.set_volume(r.current_volume)
                    except Exception: pass
                else:
                    try: r.player.set_volume(0)
                    except Exception: pass
        else:
            for r in self.track_rows:
                if r.muted:
                    try: r.player.set_volume(0)
                    except Exception: pass
                else:
                    try: r.player.set_volume(r.current_volume)
                    except Exception: pass

    def on_save(self):
        if not self.current_folder: QMessageBox.information(self, "Info", "No folder selected"); return
        data = {}
        for row in self.track_rows: data[row.name_label.text()] = row.save_settings()
        data['_global'] = {'pitch': self.global_pitch, 'loops': self.loop_list}
        settings_path = self.current_folder / SETTINGS_NAME
        try:
            with open(settings_path, 'w') as f: json.dump(data, f, indent=2)
            QMessageBox.information(self, "Saved", f"Settings saved to {settings_path}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save settings: {e}")

    def on_play(self):
        start = 0.0
        if self.loop_toggle.isChecked(): start, end = self.timeline.loop_start, self.timeline.loop_end
        speed = self.global_pitch
        for r in self.track_rows:
            try: r.player.set_sink(r.sink_combo.currentData()); r.player.set_volume(r.current_volume)
            except Exception: pass
        for p in self.global_players:
            try: p.play(start_pos=start, speed=speed)
            except Exception: pass
        self.apply_mute_solo_logic()

    def on_pause(self):
        for p in self.global_players:
            try: p.pause()
            except Exception: pass

    def on_stop(self):
        for p in self.global_players:
            try: p.stop()
            except Exception: pass

    def on_pitch_changed(self, val):
        speed = val / 100.0; self.pitch_label.setText(f"{speed:.2f}x"); self.global_pitch = speed
        for p in self.global_players:
            try:
                ok = p.set_pitch_filter(speed)
                if not ok and p.player:
                    try: p.player.speed = speed
                    except Exception: pass
            except Exception: pass

    def on_save_loop(self):
        name, ok = QInputDialog.getText(self, "Save loop", "Loop name:")
        if not ok or not name: return
        start, end = self.timeline.loop_start, self.timeline.loop_end; entry = {'name': name, 'start': start, 'end': end}; self.loop_list.append(entry); self.update_loop_dropdown()

    def update_loop_dropdown(self):
        self.loop_select.blockSignals(True); self.loop_select.clear(); self.loop_select.addItem("Select loop...", None)
        for l in self.loop_list: self.loop_select.addItem(l['name'], l)
        self.loop_select.blockSignals(False)

    def on_loop_selected(self, idx):
        data = self.loop_select.currentData(); 
        if not data: return
        self.timeline.set_loop(data['start'], data['end'])

    def on_seek_requested(self, seconds):
        # seek all players to seconds
        for p in self.global_players:
            try: p.seek(seconds)
            except Exception: pass

    def on_loop_changed(self, s, e):
        # sync loop editor state and emit if needed
        self.apply_mute_solo_logic()  # mute/solo unaffected, but we keep this hook
        # store current loop maybe
        pass

    def ui_tick(self):
        if not self.global_players: return
        # update timeline position from first player
        pos = self.global_players[0].get_time_pos()
        self.timeline.set_position(pos)
        if self.loop_toggle.isChecked():
            start, end = self.timeline.loop_start, self.timeline.loop_end
            if pos >= end - 0.02:
                for p in self.global_players:
                    try: p.seek(start)
                    except Exception: pass
        # update duration if unknown
        dur = self.global_players[0].get_duration()
        if dur and dur > 0: self.timeline.set_duration(dur)

    def closeEvent(self, event):
        for r in self.track_rows:
            try: r.player.stop()
            except Exception: pass
        super().closeEvent(event)

def main():
    if mpv is None:
        print("Error: python-mpv not installed. Install with: python3 -m pip install python-mpv"); return
    app = QApplication(sys.argv); mw = MainWindow(); mw.show(); sys.exit(app.exec())

if __name__ == '__main__':
    main()
