#!/usr/bin/env python3
# multitrack_player_v15.py
# Multitrack Player v15 - PyQt6 + sounddevice multi-output streaming
# - per-track output routing using sounddevice OutputStream
# - global tick config in ~/.config/multitrack_player/config.json
# - pulsing loop & pulsing progress bar inside loop
# - playback_rate applied via simple resampling (linear interp)

import os
import locale
locale.setlocale(locale.LC_NUMERIC, "C")
os.environ["LC_NUMERIC"] = "C"

import sys
import json
import time
import threading
import tempfile
import subprocess
from pathlib import Path
from typing import Optional, Dict, List
from math import sin, pi

import numpy as np

from PyQt6.QtWidgets import (
    QApplication, QWidget, QMainWindow, QFileDialog, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QSlider, QScrollArea, QMessageBox, QComboBox,
    QCheckBox, QInputDialog, QSpinBox, QLineEdit, QSizePolicy
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QRectF, QPointF
from PyQt6.QtGui import QPainter, QColor, QPen

# optional libs
try:
    import sounddevice as sd
    SD_AVAILABLE = True
except Exception:
    SD_AVAILABLE = False

try:
    import soundfile as sf
    SF_AVAILABLE = True
except Exception:
    SF_AVAILABLE = False

try:
    import pygame
    PYGAME_AVAILABLE = True
except Exception:
    PYGAME_AVAILABLE = False

try:
    from mutagen import File as MutagenFile
    MUTAGEN_AVAILABLE = True
except Exception:
    MUTAGEN_AVAILABLE = False

# Paths & constants
GLOBAL_CONFIG_DIR = Path.home() / ".config" / "multitrack_player"
GLOBAL_CONFIG_FILE = GLOBAL_CONFIG_DIR / "config.json"
SETTINGS_NAME = "multitrack_config.json"
AUDIO_EXTS = ('.wav', '.flac', '.ogg', '.mp3', '.m4a')
DEFAULT_BPM = 80
DEFAULT_TICK_VOL = 80
PLAYBACK_RATE_MIN = 0.5
PLAYBACK_RATE_MAX = 1.5
PLAYBACK_RATE_STEP = 0.05

GLOBAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)

def load_global_config():
    if GLOBAL_CONFIG_FILE.exists():
        try:
            with open(GLOBAL_CONFIG_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_global_config(cfg):
    try:
        GLOBAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(GLOBAL_CONFIG_FILE, 'w') as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print("Couldn't save global config:", e)

def list_pactl_sinks():
    """Try to obtain sinks via pactl; return list of (index, name)."""
    try:
        out = subprocess.check_output(['pactl', 'list', 'short', 'sinks'], text=True)
        sinks = []
        for line in out.strip().splitlines():
            parts = line.split('\t')
            if len(parts) >= 2:
                sinks.append((parts[0], parts[1]))
        return sinks
    except Exception:
        # fallback: list sounddevice devices (names)
        if SD_AVAILABLE:
            devs = sd.query_devices()
            sinks = []
            for i, d in enumerate(devs):
                # treat any output-capable device
                if d['max_output_channels'] > 0:
                    sinks.append((str(i), d['name']))
            return sinks
        return []

def semitone_to_scale(semitones):
    return 2.0 ** (float(semitones) / 12.0)

def get_audio_duration(path: Path) -> float:
    if MUTAGEN_AVAILABLE:
        try:
            f = MutagenFile(str(path))
            if f and hasattr(f.info, 'length'):
                return float(f.info.length)
        except Exception:
            pass
    # ffprobe fallback
    try:
        out = subprocess.check_output([
            'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1', str(path)
        ], stderr=subprocess.DEVNULL, text=True)
        return float(out.strip())
    except Exception:
        return 0.0

# ---------------------------
# Audio helpers: decoding & streaming
# ---------------------------
def decode_with_ffmpeg_to_wav(src_path: Path, dest_path: Path):
    """Use ffmpeg to decode src to WAV (16-bit PCM) into dest_path."""
    cmd = [
        "ffmpeg", "-y", "-v", "error", "-i", str(src_path),
        "-ar", "48000", "-ac", "2", "-f", "wav", str(dest_path)
    ]
    subprocess.check_call(cmd)

def open_soundfile_with_fallback(path: Path):
    """Try opening with soundfile. If fails (e.g. mp3), decode via ffmpeg to temp wav and open that."""
    if not SF_AVAILABLE:
        raise RuntimeError("soundfile (pysoundfile) is required for sounddevice playback.")
    try:
        sf_obj = sf.SoundFile(str(path))
        return sf_obj, None
    except Exception:
        # try ffmpeg fallback
        tmp = tempfile.NamedTemporaryFile(prefix="mtp_dec_", suffix=".wav", delete=False)
        tmp.close()
        try:
            decode_with_ffmpeg_to_wav(path, Path(tmp.name))
            sf_obj = sf.SoundFile(tmp.name)
            return sf_obj, tmp.name
        except Exception as e:
            # cleanup
            try:
                os.unlink(tmp.name)
            except Exception:
                pass
            raise e

# ---------------------------
# TrackPlayer using sounddevice
# ---------------------------
class TrackPlayerSD:
    """Stream audio file to selected device via sounddevice OutputStream.
       Supports simple playback_rate by resampling blocks (linear interp)."""
    def __init__(self, path: Path, device_index: Optional[int] = None):
        self.path = Path(path)
        self.device_index = device_index
        self.sf = None
        self._temp_wav = None
        self.stream = None
        self.lock = threading.Lock()
        self.playing = False
        self.position = 0.0  # seconds
        self.duration = 0.0
        self.playback_rate = 1.0
        self.blocksize = 4096
        self.channels = 2
        self.samplerate = 48000
        self.stop_flag = threading.Event()
        self._open_file()

    def _open_file(self):
        if not SF_AVAILABLE:
            raise RuntimeError("soundfile not installed")
        sf_obj = None
        tmp_path = None
        try:
            sf_obj, tmp = open_soundfile_with_fallback(self.path)
            self.sf = sf_obj
            self._temp_wav = tmp
            self.channels = self.sf.channels
            self.samplerate = int(self.sf.samplerate)
            # Duration
            try:
                self.duration = float(len(self.sf) / self.sf.samplerate)
            except Exception:
                # fallback using get_audio_duration
                self.duration = get_audio_duration(self.path)
        except Exception as e:
            raise RuntimeError(f"Unable to open audio file {self.path}: {e}")

    def set_device(self, device_index: Optional[int]):
        """Set sounddevice device index (int) or None for default."""
        self.device_index = device_index
        # If a stream is running, restart it to apply device change
        if self.playing:
            self.stop()
            time.sleep(0.02)
            self.play(start_pos=self.position)

    def set_playback_rate(self, rate: float):
        with self.lock:
            self.playback_rate = rate

    def set_volume_db(self, vol_pct: float):
        # simple: map 0..120 to linear gain (0..1.2). More advanced mapping optional.
        self.volume = max(0.0, float(vol_pct) / 100.0)

    def play(self, start_pos: float = 0.0):
        if not SD_AVAILABLE or not SF_AVAILABLE:
            raise RuntimeError("sounddevice and soundfile required")
        with self.lock:
            self.stop_flag.clear()
            self.playing = True
            self.sf.seek(int(start_pos * self.sf.samplerate))
            self.position = start_pos
            # create stream
            target_sr = self.sf.samplerate
            channels = self.sf.channels
            dev = int(self.device_index) if (self.device_index is not None and str(self.device_index).isdigit()) else None
            try:
                self.stream = sd.OutputStream(
                    samplerate=target_sr,
                    blocksize=self.blocksize,
                    device=dev,
                    channels=channels,
                    dtype='float32',
                    callback=self._callback,
                    finished_callback=self._stream_finished
                )
                self.stream.start()
            except Exception as e:
                # Try with default device without device argument
                try:
                    self.stream = sd.OutputStream(
                        samplerate=target_sr,
                        blocksize=self.blocksize,
                        channels=channels,
                        dtype='float32',
                        callback=self._callback,
                        finished_callback=self._stream_finished
                    )
                    self.stream.start()
                except Exception as e2:
                    print("Failed to start OutputStream:", e, e2)
                    self.playing = False

    def _stream_finished(self):
        self.playing = False

    def _callback(self, outdata, frames, time_info, status):
        """sounddevice callback: fill outdata (frames x channels) with audio (float32)."""
        if self.stop_flag.is_set():
            outdata[:] = np.zeros((frames, self.sf.channels), dtype='float32')
            raise sd.CallbackStop()
        # Read enough frames from file considering playback_rate
        with self.lock:
            rate = float(self.playback_rate)
            vol = getattr(self, 'volume', 1.0)
        # Calculate how many source frames we need to produce 'frames' output frames after resampling
        # source_frames_needed = ceil(frames * (1 / rate))
        src_frames_needed = int(np.ceil(frames / rate)) + 2
        try:
            src = self.sf.read(frames=src_frames_needed, dtype='float32', always_2d=True)
            if src.size == 0:
                # EOF -> stop
                outdata[:] = np.zeros((frames, self.sf.channels), dtype='float32')
                raise sd.CallbackStop()
            # perform linear resampling from src to frames
            src_len = src.shape[0]
            if rate == 1.0:
                # direct copy (may have fewer frames)
                if src_len >= frames:
                    out = src[:frames]
                else:
                    # pad zeros
                    out = np.zeros((frames, src.shape[1]), dtype='float32')
                    out[:src_len] = src
            else:
                # create position indices in source corresponding to output frames
                src_pos = np.linspace(0, max(0, src_len - 1), num=frames) * (1.0 / rate)
                # but safer: map output frame i to source position i / rate
                src_pos = (np.arange(frames) / rate)
                # If src doesn't contain enough frames for desired positions, read fewer or pad
                # We'll use numpy.interp per channel (simple linear)
                out = np.zeros((frames, src.shape[1]), dtype='float32')
                for ch in range(src.shape[1]):
                    # x coords for src: 0..src_len-1
                    xs = np.arange(src_len)
                    ys = src[:, ch]
                    # for positions beyond src_len-1, interp uses last value; to avoid that, pad with 0
                    # create xp and fp with extra zero at end
                    xp = np.concatenate([xs, [xs[-1] + 1]])
                    fp = np.concatenate([ys, [0.0]])
                    out[:, ch] = np.interp(src_pos, xp, fp).astype('float32')
            # apply volume scalar
            out *= vol
            # update position in seconds
            self.position += frames / float(self.sf.samplerate) * rate
            # ensure outdata shape matches expected channels (sounddevice expects matching channels)
            if out.shape[1] < outdata.shape[1]:
                # pad channels
                pad = np.zeros((frames, outdata.shape[1] - out.shape[1]), dtype='float32')
                out = np.concatenate([out, pad], axis=1)
            elif out.shape[1] > outdata.shape[1]:
                out = out[:, :outdata.shape[1]]
            outdata[:] = out
        except sd.CallbackStop:
            raise
        except Exception as e:
            print("Stream callback error:", e)
            outdata[:] = np.zeros((frames, outdata.shape[1]), dtype='float32')
            raise sd.CallbackStop()

    def seek(self, seconds: float):
        with self.lock:
            pos_frames = int(seconds * self.sf.samplerate)
            try:
                self.sf.seek(pos_frames)
                self.position = seconds
            except Exception:
                pass

    def stop(self):
        self.stop_flag.set()
        try:
            if self.stream:
                try:
                    self.stream.stop()
                    self.stream.close()
                except Exception:
                    pass
        finally:
            self.playing = False
            # close file and temporary wav
            try:
                if self.sf:
                    self.sf.close()
            except Exception:
                pass
            if self._temp_wav and Path(self._temp_wav).exists():
                try:
                    os.unlink(self._temp_wav)
                except Exception:
                    pass

    def get_time_pos(self):
        return float(self.position)

    def get_duration(self):
        return float(self.duration)

# ---------------------------
# Tick player (pygame fallback)
# ---------------------------
class TickPlayer:
    def __init__(self, path: Optional[str] = None, vol: int = DEFAULT_TICK_VOL):
        self.path = path
        self.vol = vol
        self._inited = False
        if PYGAME_AVAILABLE:
            try:
                pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
                self._inited = True
            except Exception:
                self._inited = False
        self.sound = None
        if self._inited and self.path and Path(self.path).exists():
            try:
                self.sound = pygame.mixer.Sound(self.path)
                self.sound.set_volume(self.vol / 100.0)
            except Exception:
                self.sound = None

    def set_tick_file(self, path):
        self.path = path
        if self._inited:
            try:
                if self.path and Path(self.path).exists():
                    self.sound = pygame.mixer.Sound(self.path)
                    self.sound.set_volume(self.vol / 100.0)
                else:
                    self.sound = None
            except Exception:
                self.sound = None

    def play_tick(self):
        if self._inited and self.sound:
            try:
                self.sound.play()
                return True
            except Exception:
                pass
        if self.path and Path(self.path).exists():
            try:
                subprocess.Popen(['mpv', '--no-video', '--really-quiet', self.path])
                return True
            except Exception:
                pass
        try:
            subprocess.Popen(['paplay', '/usr/share/sounds/alsa/Front_Center.wav'])
            return True
        except Exception:
            return False

# ---------------------------
# Timeline widget with pulsing of loop and progress inside loop
# ---------------------------
class Timeline(QWidget):
    seekRequested = pyqtSignal(float)
    loopChanged = pyqtSignal(float, float)

    def __init__(self, duration=10.0):
        super().__init__()
        self.duration = max(1.0, float(duration))
        self.position = 0.0
        self.loop_start = 0.0
        self.loop_end = self.duration
        self.dragging = None
        self.pulse_phase = 0.0
        self.pulse_speed = 2 * pi / 2.0  # 2 seconds
        self.setMinimumHeight(60)
        self.setMouseTracking(True)

    def set_duration(self, d: float):
        self.duration = max(1.0, float(d))
        if self.loop_end > self.duration:
            self.loop_end = self.duration
        self.update()

    def set_position(self, pos: float):
        self.position = max(0.0, min(pos, self.duration))
        self.update()

    def set_loop(self, start: float, end: float):
        self.loop_start = max(0.0, min(start, self.duration))
        self.loop_end = max(self.loop_start, min(end, self.duration))
        self.update()

    def advance_pulse(self, dt):
        self.pulse_phase += self.pulse_speed * dt
        if self.pulse_phase > 1e6:
            self.pulse_phase %= (2 * pi)

    def paintEvent(self, ev):
        p = QPainter(self)
        rect = self.rect()
        w = float(rect.width()); h = float(rect.height())
        p.fillRect(rect, QColor("#333333"))
        bar_h = 14.0
        bar_y = float(h / 2.0 - bar_h / 2.0)
        bar_rect = QRectF(10.0, bar_y, max(10.0, w - 20.0), bar_h)
        p.setBrush(QColor("#2f2f2f"))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(bar_rect, 4.0, 4.0)

        def x_for(t):
            return 10.0 + (w - 20.0) * (t / max(1.0, self.duration))

        lsx = x_for(self.loop_start)
        lex = x_for(self.loop_end)
        base_alpha = 80
        pulse_alpha = int(base_alpha + 60 * (0.5 + 0.5 * sin(self.pulse_phase)))
        loop_alpha = base_alpha
        if self.loop_start <= self.position <= self.loop_end:
            loop_alpha = pulse_alpha
        p.setBrush(QColor(80, 160, 200, loop_alpha))
        p.drawRect(QRectF(lsx, bar_y, max(4.0, lex - lsx), bar_h))

        # progress bar (pulser inside loop)
        posx = x_for(self.position)
        prog_rect = QRectF(10.0, bar_y, max(2.0, posx - 10.0), bar_h)
        prog_base_alpha = 200
        prog_pulse = int(55 * (0.5 + 0.5 * sin(self.pulse_phase))) if (self.loop_start <= self.position <= self.loop_end) else 0
        prog_alpha = min(255, prog_base_alpha + prog_pulse)
        col = QColor(102, 187, 106)  # green
        col.setAlpha(prog_alpha)
        p.setBrush(col)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRect(prog_rect)

        # handles
        handle_w = 10.0; handle_h = 18.0
        p.setBrush(QColor("#bbbbbb"))
        p.setPen(QPen(QColor("#888888")))
        p.drawRect(QRectF(lsx - handle_w/2.0, bar_y - handle_h/2.0 + bar_h/2.0, handle_w, handle_h))
        p.drawRect(QRectF(lex - handle_w/2.0, bar_y - handle_h/2.0 + bar_h/2.0, handle_w, handle_h))

        p.setPen(QPen(QColor("#ffffff"), 2))
        p.drawLine(QPointF(posx, bar_y - 6.0), QPointF(posx, bar_y + bar_h + 6.0))

        p.setPen(QPen(QColor("#e6e6e6")))
        p.drawText(10, int(bar_y - 8.0), f"{self._fmt(self.position)}")
        p.drawText(int(w - 80.0), int(bar_y - 8.0), f"{self._fmt(self.duration)}")
        p.end()

    def _fmt(self, t):
        m = int(t // 60); s = int(t % 60); return f"{m:02d}:{s:02d}"

    def mousePressEvent(self, ev):
        x = float(ev.position().x()); w = float(self.rect().width())
        def t_from_x(xv): return (xv - 10.0) / max(1.0, (w - 20.0)) * self.duration
        lsx = 10.0 + (w - 20.0) * (self.loop_start / max(1.0, self.duration))
        lex = 10.0 + (w - 20.0) * (self.loop_end / max(1.0, self.duration))
        if abs(x - lsx) < 12.0:
            self.dragging = 'start'; return
        if abs(x - lex) < 12.0:
            self.dragging = 'end'; return
        self.dragging = 'pos'
        newt = max(0.0, min(self.duration, t_from_x(x)))
        self.set_position(newt)
        self.seekRequested.emit(newt)

    def mouseMoveEvent(self, ev):
        if not self.dragging: return
        x = float(ev.position().x()); w = float(self.rect().width())
        def t_from_x(xv): return (xv - 10.0) / max(1.0, (w - 20.0)) * self.duration
        t = max(0.0, min(self.duration, t_from_x(x)))
        if self.dragging == 'pos':
            self.set_position(t); self.seekRequested.emit(t)
        elif self.dragging == 'start':
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

# ---------------------------
# TrackRow UI
# ---------------------------
class TrackRow(QWidget):
    def __init__(self, filepath: str, sinks: List, assigned_sink=None, settings=None):
        super().__init__()
        self.filepath = filepath
        self.sinks = sinks
        self.assigned_sink = assigned_sink
        self.settings = settings or {}
        self.player = None  # TrackPlayerSD instance created in MainWindow.load_tracks
        self.current_volume = 100
        self._build_ui()
        self._load_settings()

    def _build_ui(self):
        layout = QHBoxLayout(); self.setLayout(layout)
        self.name_label = QLabel(Path(self.filepath).name)
        layout.addWidget(self.name_label, 3)
        self.vol_slider = QSlider(Qt.Orientation.Horizontal)
        self.vol_slider.setRange(0, 120); self.vol_slider.setValue(100)
        self.vol_slider.setFixedWidth(320)
        layout.addWidget(QLabel("Vol")); layout.addWidget(self.vol_slider, 0)
        self.vol_label = QLabel("100%"); layout.addWidget(self.vol_label)
        self.mute_cb = QCheckBox("Mute"); self.mute_cb.setFixedWidth(60)
        self.solo_cb = QCheckBox("Solo"); self.solo_cb.setFixedWidth(60)
        layout.addWidget(self.mute_cb); layout.addWidget(self.solo_cb)
        layout.addStretch()
        self.sink_combo = QComboBox()
        for s in self.sinks:
            self.sink_combo.addItem(s[1], s[0])  # display name, data=index
        self.sink_combo.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        layout.addWidget(self.sink_combo, 0)

        self.sink_combo.currentIndexChanged.connect(self._on_sink_changed)
        self.vol_slider.valueChanged.connect(self._on_volume_changed)
        self.mute_cb.toggled.connect(lambda st: None)
        self.solo_cb.toggled.connect(lambda st: None)

    def _load_settings(self):
        n = Path(self.filepath).name
        e = self.settings.get(n, {})
        vol = e.get('volume', 100)
        self.vol_slider.setValue(int(vol)); self.vol_label.setText(f"{int(vol)}%")
        sink = e.get('sink')
        if sink is not None:
            idx = self.sink_combo.findData(sink)
            if idx != -1:
                self.sink_combo.setCurrentIndex(idx)

    def set_player(self, player: TrackPlayerSD):
        self.player = player
        # apply initial volume & sink
        try:
            self.player.set_volume_db(self.vol_slider.value())
        except Exception:
            pass

    def _on_sink_changed(self, idx):
        data = self.sink_combo.currentData()
        # data is sink index as string (from pactl) or device index int if fallback
        if self.player:
            try:
                # try to interpret as int device index; else pass as string to set_device
                try:
                    didx = int(data)
                except Exception:
                    didx = data
                self.player.set_device(didx)
            except Exception as e:
                print("Failed setting device:", e)

    def _on_volume_changed(self, val):
        self.vol_label.setText(f"{int(val)}%")
        self.current_volume = int(val)
        if self.player:
            try:
                self.player.set_volume_db(val)
            except Exception:
                pass

# ---------------------------
# Main Window
# ---------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Multitrack Player v15 - sounddevice")
        self.resize(1200, 820)
        self.setStyleSheet("QWidget { background-color: #2f2f2f; color: #e6e6e6 }")

        # globals
        self.global_cfg = load_global_config()
        self.global_tick = self.global_cfg.get('tick_file')
        self.global_playback_rate = float(self.global_cfg.get('playback_rate', 1.0))
        self.global_bpm = int(self.global_cfg.get('bpm', DEFAULT_BPM))

        self.sinks = list_pactl_sinks()
        # if no sinks via pactl, fallback to sounddevice device names
        if not self.sinks and SD_AVAILABLE:
            devs = sd.query_devices()
            self.sinks = [(str(i), d['name']) for i,d in enumerate(devs) if d['max_output_channels']>0]

        self.settings = {}
        self.current_folder = None
        self.track_rows: List[TrackRow] = []
        self.track_players: List[TrackPlayerSD] = []

        self.timeline = Timeline(duration=10.0)
        self.tick_player = TickPlayer(self.global_tick, vol=int(self.global_cfg.get('tick_volume', DEFAULT_TICK_VOL)))

        self._build_ui()

        self.ui_timer = QTimer(); self.ui_timer.setInterval(80)
        self._last_ui_time = time.time()
        self.ui_timer.timeout.connect(self.ui_tick); self.ui_timer.start()

    def _build_ui(self):
        central = QWidget(); self.setCentralWidget(central); v = QVBoxLayout(); central.setLayout(v)
        top = QHBoxLayout()
        self.open_btn = QPushButton("Open Folder"); top.addWidget(self.open_btn)
        self.folder_label = QLabel("No folder"); top.addWidget(self.folder_label, 1)
        self.refresh_btn = QPushButton("Refresh Outputs"); top.addWidget(self.refresh_btn)
        self.save_btn = QPushButton("Save Settings"); top.addWidget(self.save_btn)
        v.addLayout(top)

        v.addWidget(self.timeline)
        transport = QHBoxLayout()
        self.play_btn = QPushButton("Play"); transport.addWidget(self.play_btn)
        self.pause_btn = QPushButton("Pause"); transport.addWidget(self.pause_btn)
        self.stop_btn = QPushButton("Stop"); transport.addWidget(self.stop_btn)
        transport.addWidget(QLabel("Playback rate:"))
        self.rate_minus = QPushButton("–"); self.rate_plus = QPushButton("+")
        self.rate_minus.setFixedWidth(28); self.rate_plus.setFixedWidth(28)
        transport.addWidget(self.rate_minus)
        self.rate_label = QLabel(f"{int(round(self.global_playback_rate*100))}%"); transport.addWidget(self.rate_label)
        transport.addWidget(self.rate_plus)
        v.addLayout(transport)

        loop_h = QHBoxLayout()
        self.loop_toggle = QCheckBox("Loop On"); loop_h.addWidget(self.loop_toggle)
        self.loop_save = QPushButton("Save Loop"); loop_h.addWidget(self.loop_save)
        self.loop_delete = QPushButton("Delete Loop"); loop_h.addWidget(self.loop_delete)
        self.loop_select = QComboBox(); loop_h.addWidget(self.loop_select, 1)
        loop_h.addWidget(QLabel("BPM:"))
        self.bpm_spin = QSpinBox(); self.bpm_spin.setRange(30, 300); self.bpm_spin.setValue(self.global_bpm); loop_h.addWidget(self.bpm_spin)
        loop_h.addWidget(QLabel("Tick sound (global):"))
        self.tick_label = QLineEdit(); self.tick_label.setReadOnly(True); self.tick_label.setText(self.global_tick or ""); loop_h.addWidget(self.tick_label, 2)
        self.tick_browse = QPushButton("Browse"); loop_h.addWidget(self.tick_browse)
        loop_h.addWidget(QLabel("Tick vol:"))
        self.tick_vol_spin = QSpinBox(); self.tick_vol_spin.setRange(0, 200); self.tick_vol_spin.setValue(int(self.global_cfg.get('tick_volume', DEFAULT_TICK_VOL))); loop_h.addWidget(self.tick_vol_spin)
        v.addLayout(loop_h)

        self.tracks_area = QScrollArea(); self.tracks_widget = QWidget(); self.tracks_layout = QVBoxLayout(); self.tracks_widget.setLayout(self.tracks_layout)
        self.tracks_area.setWidgetResizable(True); self.tracks_area.setWidget(self.tracks_widget); v.addWidget(self.tracks_area)

        # connect
        self.open_btn.clicked.connect(self.on_open)
        self.refresh_btn.clicked.connect(self.on_refresh)
        self.save_btn.clicked.connect(self.on_save)
        self.play_btn.clicked.connect(self.on_play)
        self.pause_btn.clicked.connect(self.on_pause)
        self.stop_btn.clicked.connect(self.on_stop)
        self.rate_plus.clicked.connect(self.on_rate_plus)
        self.rate_minus.clicked.connect(self.on_rate_minus)
        self.loop_save.clicked.connect(self.on_save_loop)
        self.loop_delete.clicked.connect(self.on_delete_loop)
        self.loop_select.currentIndexChanged.connect(self.on_loop_selected)
        self.timeline.seekRequested.connect(self.on_seek_requested)
        self.timeline.loopChanged.connect(self.on_loop_changed)
        self.loop_toggle.stateChanged.connect(self.on_loop_toggled)
        self.bpm_spin.valueChanged.connect(lambda v: setattr(self, 'global_bpm', int(v)))
        self.tick_browse.clicked.connect(self.on_browse_tick)
        self.tick_vol_spin.valueChanged.connect(self.on_tick_vol_changed)

    # ---------------------------
    # File & load tracks
    # ---------------------------
    def on_open(self):
        folder = QFileDialog.getExistingDirectory(self, "Select folder", str(Path.home()))
        if not folder:
            return
        self.current_folder = Path(folder)
        self.folder_label.setText(str(self.current_folder))
        settings_path = self.current_folder / SETTINGS_NAME
        if settings_path.exists():
            try:
                with open(settings_path, 'r') as f:
                    self.settings = json.load(f)
            except Exception:
                self.settings = {}
        else:
            self.settings = {}
        # load global folder-specific settings
        g = self.settings.get('_global', {})
        if g.get('tick_file'):
            self.global_tick = g.get('tick_file')
        self.tick_label.setText(self.global_tick or "")
        self.tick_player.set_tick_file(self.global_tick)
        self.global_playback_rate = g.get('playback_rate', self.global_playback_rate)
        self._update_rate_label()
        self.load_tracks(folder)

    def load_tracks(self, folder):
        # cleanup old
        for p in self.track_players:
            try: p.stop()
            except Exception: pass
        for r in self.track_rows:
            try: r.deleteLater()
            except Exception: pass
        self.track_rows = []; self.track_players = []

        # refresh sinks
        self.sinks = list_pactl_sinks()
        if not self.sinks and SD_AVAILABLE:
            devs = sd.query_devices()
            self.sinks = [(str(i), d['name']) for i,d in enumerate(devs) if d['max_output_channels']>0]

        files = [p for p in sorted(Path(folder).iterdir()) if p.suffix.lower() in AUDIO_EXTS]
        durations = []
        for pth in files:
            row = TrackRow(str(pth), self.sinks, settings=self.settings)
            self.tracks_layout.addWidget(row)
            self.track_rows.append(row)
            # create TrackPlayerSD for each
            # determine assigned sink from settings
            assigned = None
            ent = self.settings.get(Path(pth).name, {})
            if ent:
                assigned = ent.get('sink')
            # map assigned sink: if assigned corresponds to pactl index string, use that; else try int index
            devidx = None
            if assigned is not None:
                try:
                    devidx = int(assigned)
                except Exception:
                    devidx = assigned
            try:
                tp = TrackPlayerSD(pth, device_index=devidx)
                tp.set_playback_rate(self.global_playback_rate)
                row.set_player(tp)
                self.track_players.append(tp)
                durations.append(tp.get_duration())
                # select sink combo if assigned
                if assigned is not None:
                    idx = row.sink_combo.findData(assigned)
                    if idx != -1:
                        row.sink_combo.setCurrentIndex(idx)
            except Exception as e:
                print("Failed to init TrackPlayerSD:", e)
        self.tracks_layout.addStretch(1)
        maxdur = max(durations) if durations else 10.0
        if not maxdur or maxdur <= 1.0:
            maxdur = 10.0
        self.timeline.set_duration(maxdur)

        # load loops
        g = self.settings.get('_global', {})
        loops = g.get('loops', {})
        self.loop_list = loops if loops else {}
        self.update_loop_dropdown()
        last = g.get('last_used_loop')
        if last and last in self.loop_list:
            chosen = last
        elif self.loop_list:
            chosen = next(iter(self.loop_list.keys()))
        else:
            chosen = None
        if chosen:
            rng = self.loop_list[chosen]
            self.timeline.set_loop(rng[0], rng[1])
            self.loop_select.setCurrentText(chosen)
        else:
            self.timeline.set_loop(0.0, maxdur)

    def on_refresh(self):
        self.sinks = list_pactl_sinks()
        for r in self.track_rows:
            cur = r.sink_combo.currentData()
            r.sink_combo.clear()
            for s in self.sinks:
                r.sink_combo.addItem(s[1], s[0])
            if cur:
                idx = r.sink_combo.findData(cur)
                if idx != -1:
                    r.sink_combo.setCurrentIndex(idx)

    # ---------------------------
    # Loop UI
    # ---------------------------
    def update_loop_dropdown(self):
        self.loop_select.blockSignals(True)
        self.loop_select.clear()
        self.loop_select.addItem("Select loop...", None)
        for name, rng in (self.loop_list.items() if hasattr(self,'loop_list') else []):
            self.loop_select.addItem(name, (name, rng))
        self.loop_select.blockSignals(False)

    def on_save_loop(self):
        name, ok = QInputDialog.getText(self, "Save loop", "Loop name:")
        if not ok or not name:
            return
        start, end = float(self.timeline.loop_start), float(self.timeline.loop_end)
        if not hasattr(self, 'loop_list'):
            self.loop_list = {}
        self.loop_list[name] = [start, end]
        self.current_loop_name = name
        self.update_loop_dropdown()
        idx = self.loop_select.findText(name)
        if idx != -1:
            self.loop_select.setCurrentIndex(idx)
        # store in settings memory
        if '_global' not in self.settings:
            self.settings['_global'] = {}
        self.settings['_global']['last_used_loop'] = name

    def on_delete_loop(self):
        data = self.loop_select.currentData()
        if not data:
            QMessageBox.information(self, "Info", "No loop selected")
            return
        name, rng = data
        if hasattr(self,'loop_list') and name in self.loop_list:
            del self.loop_list[name]
            self.update_loop_dropdown()

    def on_loop_selected(self, idx):
        data = self.loop_select.currentData()
        if not data:
            return
        name, rng = data
        self.current_loop_name = name
        self.timeline.set_loop(rng[0], rng[1])
        if '_global' not in self.settings:
            self.settings['_global'] = {}
        self.settings['_global']['last_used_loop'] = name

    def on_loop_toggled(self, state):
        if state:
            start = self.timeline.loop_start
            for p in self.track_players:
                try: p.seek(start)
                except Exception: pass
            self.timeline.set_position(start)

    def on_loop_changed(self, s, e):
        # manual loop change -> not auto-named
        self.current_loop_name = None

    # ---------------------------
    # Playback controls
    # ---------------------------
    def on_play(self):
        if not self.track_players:
            return
        start = 0.0
        if self.loop_toggle.isChecked():
            start = self.timeline.loop_start
        # apply sinks & volumes & playback rate
        for idx, row in enumerate(self.track_rows):
            try:
                tp = self.track_players[idx]
                sink_data = row.sink_combo.currentData()
                if sink_data is not None:
                    try:
                        didx = int(sink_data)
                    except Exception:
                        didx = sink_data
                    tp.set_device(didx)
                tp.set_playback_rate(self.global_playback_rate)
                tp.set_volume_db(row.vol_slider.value())
            except Exception:
                pass
        # start streams
        for tp in self.track_players:
            try:
                tp.play(start_pos=start)
            except Exception as e:
                print("Error starting track:", e)
        # ensure UI reflects correct position
        self.timeline.set_position(start)

    def on_pause(self):
        for tp in self.track_players:
            try:
                tp.stop()
            except Exception:
                pass

    def on_stop(self):
        for tp in self.track_players:
            try:
                tp.stop()
            except Exception:
                pass
        if hasattr(self,'track_players'):
            for tp in self.track_players:
                tp.position = 0.0
        self.timeline.set_position(0.0)

    # ---------------------------
    # Playback rate
    # ---------------------------
    def _update_rate_label(self):
        self.rate_label.setText(f"{int(round(self.global_playback_rate*100))}%")

    def on_rate_plus(self):
        new = round(self.global_playback_rate + PLAYBACK_RATE_STEP, 3)
        if new > PLAYBACK_RATE_MAX: new = PLAYBACK_RATE_MAX
        self.global_playback_rate = new
        for tp in self.track_players:
            tp.set_playback_rate(self.global_playback_rate)
        self.global_cfg['playback_rate'] = float(self.global_playback_rate)
        self._update_rate_label()

    def on_rate_minus(self):
        new = round(self.global_playback_rate - PLAYBACK_RATE_STEP, 3)
        if new < PLAYBACK_RATE_MIN: new = PLAYBACK_RATE_MIN
        self.global_playback_rate = new
        for tp in self.track_players:
            tp.set_playback_rate(self.global_playback_rate)
        self.global_cfg['playback_rate'] = float(self.global_playback_rate)
        self._update_rate_label()

    # ---------------------------
    # Tick handlers
    # ---------------------------
    def on_browse_tick(self):
        f = QFileDialog.getOpenFileName(self, "Select tick sound (global)", str(Path.home()), "Audio files (*.wav *.ogg *.mp3 *.flac)")[0]
        if not f: return
        self.global_tick = f
        self.tick_label.setText(self.global_tick)
        self.tick_player.set_tick_file(self.global_tick)
        self.global_cfg['tick_file'] = self.global_tick

    def on_tick_vol_changed(self, v):
        self.tick_player.vol = int(v)
        self.global_cfg['tick_volume'] = int(v)

    # ---------------------------
    # Save settings
    # ---------------------------
    def on_save(self):
        if not self.current_folder:
            QMessageBox.information(self, "Info", "No folder opened")
            return
        # save folder-specific
        data = {}
        for idx, row in enumerate(self.track_rows):
            key = Path(row.filepath).name
            sink = row.sink_combo.currentData()
            data[key] = {'sink': sink, 'volume': row.vol_slider.value(), 'mute': False, 'solo': False}
        data['_global'] = {
            'loops': getattr(self,'loop_list',{}),
            'last_used_loop': getattr(self,'current_loop_name', None),
            'bpm': int(self.bpm_spin.value()),
            'tick_file': self.global_tick,
            'tick_volume': int(self.tick_vol_spin.value()),
            'playback_rate': float(self.global_playback_rate)
        }
        try:
            with open(self.current_folder / SETTINGS_NAME, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save folder settings: {e}")
        # save global config
        self.global_cfg['tick_file'] = self.global_tick
        self.global_cfg['tick_volume'] = int(self.tick_vol_spin.value())
        self.global_cfg['playback_rate'] = float(self.global_playback_rate)
        save_global_config(self.global_cfg)
        QMessageBox.information(self, "Saved", "Settings saved (folder + global).")

    # ---------------------------
    # UI tick: update timeline position from first player
    # ---------------------------
    def ui_tick(self):
        now = time.time()
        dt = now - self._last_ui_time
        self._last_ui_time = now
        self.timeline.advance_pulse(dt)
        if not self.track_players:
            return
        # take primary player's position as source of truth
        try:
            pos = self.track_players[0].get_time_pos()
        except Exception:
            pos = 0.0
        self.timeline.set_position(pos)
        # loop handling
        if self.loop_toggle.isChecked():
            start, end = self.timeline.loop_start, self.timeline.loop_end
            if pos >= end - 0.02:
                for p in self.track_players:
                    try:
                        p.seek(start)
                    except Exception:
                        pass
                self.timeline.set_position(start)
        # update duration if available
        try:
            dur = self.track_players[0].get_duration()
            if dur and dur > 0:
                self.timeline.set_duration(dur)
        except Exception:
            pass

    def on_seek_requested(self, seconds):
        for p in self.track_players:
            try:
                p.seek(seconds)
            except Exception:
                pass

    def closeEvent(self, ev):
        try:
            for tp in self.track_players:
                tp.stop()
        except Exception:
            pass
        # save global config automatically on exit
        save_global_config(self.global_cfg)
        super().closeEvent(ev)

# ---------------------------
# Main
# ---------------------------
def main():
    if not SD_AVAILABLE:
        print("Warning: sounddevice not available. Install with `pip install sounddevice`.")
    if not SF_AVAILABLE:
        print("Warning: soundfile not available. Install with `pip install soundfile`.")
    app = QApplication(sys.argv)
    mw = MainWindow()
    mw.show()
    sys.exit(app.exec())

if __name__ == '__main__':
    main()
