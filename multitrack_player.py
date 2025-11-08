#!/usr/bin/env python3
# multitrack_player_v16.py
# Multitrack Player v16 - PyQt6 + sounddevice (fixed routing & pulsing)
# - LC_NUMERIC fix
# - per-track device switching -> stream rebuild preserving position
# - green progress pulsing ONLY inside loop and stronger
# - console routing/debug messages printed as [AudioRouting]

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
            try:
                devs = sd.query_devices()
                sinks = []
                for i, d in enumerate(devs):
                    if d['max_output_channels'] > 0:
                        sinks.append((str(i), d['name']))
                return sinks
            except Exception:
                return []
        return []

def get_audio_duration(path: Path) -> float:
    if MUTAGEN_AVAILABLE:
        try:
            f = MutagenFile(str(path))
            if f and hasattr(f.info, 'length'):
                return float(f.info.length)
        except Exception:
            pass
    try:
        out = subprocess.check_output([
            'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1', str(path)
        ], stderr=subprocess.DEVNULL, text=True)
        return float(out.strip())
    except Exception:
        return 0.0

def decode_with_ffmpeg_to_wav(src_path: Path, dest_path: Path):
    cmd = [
        "ffmpeg", "-y", "-v", "error", "-i", str(src_path),
        "-ar", "48000", "-ac", "2", "-f", "wav", str(dest_path)
    ]
    subprocess.check_call(cmd)

def open_soundfile_with_fallback(path: Path):
    if not SF_AVAILABLE:
        raise RuntimeError("soundfile (pysoundfile) is required.")
    try:
        sf_obj = sf.SoundFile(str(path))
        return sf_obj, None
    except Exception:
        tmp = tempfile.NamedTemporaryFile(prefix="mtp_dec_", suffix=".wav", delete=False)
        tmp.close()
        try:
            decode_with_ffmpeg_to_wav(path, Path(tmp.name))
            sf_obj = sf.SoundFile(tmp.name)
            return sf_obj, tmp.name
        except Exception as e:
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
       Supports simple playback_rate by resampling blocks (linear interp).
       Handles device switching by rebuilding stream preserving position."""
    def __init__(self, path: Path, device: Optional[object] = None):
        self.path = Path(path)
        self.device = device  # can be int index or string name
        self.sf = None
        self._temp_wav = None
        self.stream = None
        self.lock = threading.Lock()
        self.playing = False
        self.position = 0.0
        self.duration = 0.0
        self.playback_rate = 1.0
        self.blocksize = 4096
        self.channels = 2
        self.samplerate = 48000
        self.stop_flag = threading.Event()
        self.volume = 1.0
        self._open_file()

    def _open_file(self):
        if not SF_AVAILABLE:
            raise RuntimeError("soundfile not installed")
        try:
            sf_obj, tmp = open_soundfile_with_fallback(self.path)
            self.sf = sf_obj
            self._temp_wav = tmp
            self.channels = self.sf.channels
            self.samplerate = int(self.sf.samplerate)
            try:
                self.duration = float(len(self.sf) / self.sf.samplerate)
            except Exception:
                self.duration = get_audio_duration(self.path)
        except Exception as e:
            raise RuntimeError(f"Unable to open audio file {self.path}: {e}")

    def _device_repr(self, device):
        if device is None:
            return "default"
        return str(device)

    def _create_stream(self):
        """Create OutputStream with current device, samplerate, channels and callback."""
        if not SD_AVAILABLE:
            raise RuntimeError("sounddevice is required")
        # If there is an existing stream, close it first
        if self.stream:
            try:
                self.stream.stop(); self.stream.close()
            except Exception:
                pass
            self.stream = None
        dev_arg = None
        if self.device is not None:
            # device might be pactl index string or sounddevice index int
            try:
                # if string is numeric -> int index to sd
                if isinstance(self.device, str) and self.device.isdigit():
                    dev_arg = int(self.device)
                else:
                    dev_arg = self.device
            except Exception:
                dev_arg = self.device
        try:
            self.stream = sd.OutputStream(
                samplerate=self.samplerate,
                blocksize=self.blocksize,
                device=dev_arg,
                channels=self.channels,
                dtype='float32',
                callback=self._callback,
                finished_callback=self._stream_finished
            )
            return True
        except Exception as e:
            # fallback: try without device argument
            try:
                print(f"[AudioRouting] Warning: failed to open device {self._device_repr(self.device)} -> {e}. Falling back to default device.")
                self.stream = sd.OutputStream(
                    samplerate=self.samplerate,
                    blocksize=self.blocksize,
                    channels=self.channels,
                    dtype='float32',
                    callback=self._callback,
                    finished_callback=self._stream_finished
                )
                return True
            except Exception as e2:
                print(f"[AudioRouting] Failed to create stream: {e2}")
                self.stream = None
                return False

    def set_device(self, device: Optional[object]):
        """Set device and rebuild stream preserving playback position & state."""
        old = self.device
        self.device = device
        print(f"[AudioRouting] Setting device for {self.path.name}: {self._device_repr(old)} -> {self._device_repr(device)}")
        was_playing = self.playing
        pos = self.get_time_pos()
        # rebuild: stop stream and re-open file at current position
        try:
            self.stop()
            # reopen file handle (ensure seekable)
            try:
                # reopen underlying soundfile to reset state
                if self.sf:
                    try:
                        self.sf.close()
                    except Exception:
                        pass
                self._open_file()
            except Exception as e:
                print(f"[AudioRouting] Warning: failed to reopen file: {e}")
            # set position then create stream
            try:
                self.sf.seek(int(pos * self.sf.samplerate))
                self.position = pos
            except Exception:
                # fallback: set position variable, callback will read from current file pos
                self.position = pos
            created = self._create_stream()
            if created and was_playing:
                # start streaming at preserved position
                try:
                    self.play(start_pos=pos)
                except Exception as e:
                    print(f"[AudioRouting] Error restarting playback after device change: {e}")
            print(f"[AudioRouting] Device set for {self.path.name} -> {self._device_repr(device)}")
        except Exception as e:
            print(f"[AudioRouting] Exception in set_device: {e}")

    def set_playback_rate(self, rate: float):
        with self.lock:
            self.playback_rate = float(rate)

    def set_volume_db(self, vol_pct: float):
        with self.lock:
            self.volume = max(0.0, float(vol_pct) / 100.0)

    def play(self, start_pos: float = 0.0):
        if not SD_AVAILABLE or not SF_AVAILABLE:
            raise RuntimeError("sounddevice and soundfile required")
        with self.lock:
            self.stop_flag.clear()
            self.playing = True
            # ensure file handle open and positioned
            try:
                self.sf.seek(int(start_pos * self.sf.samplerate))
            except Exception:
                pass
            self.position = start_pos
            # create stream if not exists
            if not self.stream:
                created = self._create_stream()
                if not created:
                    print(f"[AudioRouting] Failed to create stream for {self.path.name}, cannot play.")
                    self.playing = False
                    return
            try:
                # start stream (if not started)
                if not self.stream.active:
                    self.stream.start()
            except Exception as e:
                print(f"[AudioRouting] Error starting stream for {self.path.name}: {e}")
                self.playing = False

    def _stream_finished(self):
        self.playing = False

    def _callback(self, outdata, frames, time_info, status):
        if self.stop_flag.is_set():
            outdata[:] = np.zeros((frames, self.channels), dtype='float32')
            raise sd.CallbackStop()
        with self.lock:
            rate = float(self.playback_rate)
            vol = float(self.volume)
        # compute required source frames
        src_frames_needed = int(np.ceil(frames / max(1e-6, rate))) + 4
        try:
            src = self.sf.read(frames=src_frames_needed, dtype='float32', always_2d=True)
            if src.size == 0:
                outdata[:] = np.zeros((frames, self.channels), dtype='float32')
                raise sd.CallbackStop()
            src_len = src.shape[0]
            if rate == 1.0:
                if src_len >= frames:
                    out = src[:frames]
                else:
                    out = np.zeros((frames, src.shape[1]), dtype='float32')
                    out[:src_len] = src
            else:
                # map output positions to source positions
                src_pos = (np.arange(frames) / rate)
                # ensure interpolation range
                xs = np.arange(src_len)
                out = np.zeros((frames, src.shape[1]), dtype='float32')
                for ch in range(src.shape[1]):
                    ys = src[:, ch]
                    xp = np.concatenate([xs, [xs[-1] + 1]])
                    fp = np.concatenate([ys, [0.0]])
                    out[:, ch] = np.interp(src_pos, xp, fp).astype('float32')
            out *= vol
            # update logical position (in seconds) considering playback_rate
            self.position += frames / float(self.sf.samplerate) * rate
            # match output channels
            if out.shape[1] < outdata.shape[1]:
                pad = np.zeros((frames, outdata.shape[1] - out.shape[1]), dtype='float32')
                out = np.concatenate([out, pad], axis=1)
            elif out.shape[1] > outdata.shape[1]:
                out = out[:, :outdata.shape[1]]
            outdata[:] = out
        except sd.CallbackStop:
            raise
        except Exception as e:
            print(f"[AudioRouting] Stream callback error for {self.path.name}: {e}")
            outdata[:] = np.zeros((frames, outdata.shape[1]), dtype='float32')
            raise sd.CallbackStop()

    def seek(self, seconds: float):
        with self.lock:
            try:
                pos_frames = int(seconds * self.sf.samplerate)
                self.sf.seek(pos_frames)
                self.position = seconds
            except Exception:
                self.position = seconds

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
            self.stream = None

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
# Timeline widget (pulse only inside loop, stronger)
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
        # default pulse speed -> 2s period
        self.pulse_speed = 2 * pi / 2.0
        self.setMinimumHeight(64)
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
        base_alpha = 90
        pulse_alpha = int(base_alpha + 120 * (0.5 + 0.5 * sin(self.pulse_phase)))  # stronger amplitude
        loop_alpha = base_alpha
        # Only pulse loop background if position is inside loop
        if self.loop_start <= self.position <= self.loop_end:
            loop_alpha = pulse_alpha
        p.setBrush(QColor(80, 160, 200, loop_alpha))
        p.drawRect(QRectF(lsx, bar_y, max(4.0, lex - lsx), bar_h))

        # progress bar - pulse only if in loop AND looping enabled (so user sees active loop)
        posx = x_for(self.position)
        prog_rect = QRectF(10.0, bar_y, max(2.0, posx - 10.0), bar_h)
        prog_base_alpha = 200
        prog_pulse = 0
        # check also that loop has positive width
        if (self.loop_end - self.loop_start) > 0.001 and (self.loop_start <= self.position <= self.loop_end):
            prog_pulse = int(100 * (0.5 + 0.5 * sin(self.pulse_phase)))  # quite visible
        prog_alpha = min(255, prog_base_alpha + prog_pulse)
        col = QColor(102, 187, 106)
        col.setAlpha(prog_alpha)
        p.setBrush(col)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRect(prog_rect)

        # handles (left and right)
        handle_w = 10.0; handle_h = 18.0
        p.setBrush(QColor("#bbbbbb"))
        p.setPen(QPen(QColor("#888888")))
        p.drawRect(QRectF(lsx - handle_w/2.0, bar_y - handle_h/2.0 + bar_h/2.0, handle_w, handle_h))
        p.drawRect(QRectF(lex - handle_w/2.0, bar_y - handle_h/2.0 + bar_h/2.0, handle_w, handle_h))

        # play cursor
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
        if not self.dragging:
            return
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
        self.player = None
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
            self.sink_combo.addItem(s[1], s[0])
        self.sink_combo.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        layout.addWidget(self.sink_combo, 0)

        self.sink_combo.currentIndexChanged.connect(self._on_sink_changed)
        self.vol_slider.valueChanged.connect(self._on_volume_changed)

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
        try:
            self.player.set_volume_db(self.vol_slider.value())
        except Exception:
            pass

    def _on_sink_changed(self, idx):
        data = self.sink_combo.currentData()
        if self.player:
            try:
                # interpret numeric string as int index for sounddevice if possible
                try:
                    didx = int(data)
                except Exception:
                    didx = data
                # set device (will rebuild stream and resume)
                self.player.set_device(didx)
            except Exception as e:
                print(f"[AudioRouting] Failed setting device: {e}")

    def _on_volume_changed(self, val):
        self.vol_label.setText(f"{int(val)}%")
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
        self.setWindowTitle("Multitrack Player v16 - sounddevice")
        self.resize(1220, 860)
        self.setStyleSheet("QWidget { background-color: #2f2f2f; color: #e6e6e6 }")

        # load global config
        self.global_cfg = load_global_config()
        self.global_tick = self.global_cfg.get('tick_file')
        self.global_playback_rate = float(self.global_cfg.get('playback_rate', 1.0))
        self.global_bpm = int(self.global_cfg.get('bpm', DEFAULT_BPM))

        self.sinks = list_pactl_sinks()
        if not self.sinks and SD_AVAILABLE:
            try:
                devs = sd.query_devices()
                self.sinks = [(str(i), d['name']) for i,d in enumerate(devs) if d['max_output_channels']>0]
            except Exception:
                self.sinks = []

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

        # connect signals
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
            try:
                devs = sd.query_devices()
                self.sinks = [(str(i), d['name']) for i,d in enumerate(devs) if d['max_output_channels']>0]
            except Exception:
                self.sinks = []

        files = [p for p in sorted(Path(folder).iterdir()) if p.suffix.lower() in AUDIO_EXTS]
        durations = []
        for pth in files:
            row = TrackRow(str(pth), self.sinks, settings=self.settings)
            self.tracks_layout.addWidget(row)
            self.track_rows.append(row)
            # assigned sink from settings
            assigned = None
            ent = self.settings.get(Path(pth).name, {})
            if ent:
                assigned = ent.get('sink')
            # interpret assigned: numeric -> index, else string
            devidx = None
            if assigned is not None:
                try:
                    devidx = int(assigned)
                except Exception:
                    devidx = assigned
            try:
                tp = TrackPlayerSD(pth, device=devidx)
                tp.set_playback_rate(self.global_playback_rate)
                row.set_player(tp)
                self.track_players.append(tp)
                durations.append(tp.get_duration())
                if assigned is not None:
                    idx = row.sink_combo.findData(assigned)
                    if idx != -1:
                        row.sink_combo.setCurrentIndex(idx)
            except Exception as e:
                print(f"[AudioRouting] Failed to init TrackPlayerSD for {pth.name}: {e}")
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
            except Exception as e:
                print(f"[AudioRouting] Error applying settings to track {idx}: {e}")
        # start streams
        for tp in self.track_players:
            try:
                tp.play(start_pos=start)
            except Exception as e:
                print(f"[AudioRouting] Error starting track: {e}")
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
        # update duration
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
