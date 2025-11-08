#!/usr/bin/env python3
# multitrack_player_v17.py
# Multitrack Player v17 - sounddevice + pactl routing fallback
# - stores project settings in <folder>/multitrack_config.json
# - global config in ~/.config/multitrack_player/config.json
# - Test button, default project folder, tick enabled saved per-project
# - os.environ["LC_NUMERIC"] = "C"

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
    QCheckBox, QInputDialog, QSpinBox, QLineEdit, QSizePolicy, QDialog, QFormLayout
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QRectF, QPointF
from PyQt6.QtGui import QPainter, QColor, QPen, QFontMetrics

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
    """Return list of sink names via pactl (Pulse/PipeWire)."""
    try:
        out = subprocess.check_output(['pactl', 'list', 'short', 'sinks'], text=True)
        sinks = []
        for line in out.strip().splitlines():
            parts = line.split('\t')
            if len(parts) >= 2:
                sinks.append(parts[1])
        return sinks
    except Exception:
        return []

def query_sd_output_devices() -> List[Dict]:
    out = []
    if not SD_AVAILABLE:
        return out
    try:
        devs = sd.query_devices()
        for i, d in enumerate(devs):
            if d.get('max_output_channels', 0) > 0:
                out.append({'index': i, 'name': d.get('name', f"device-{i}")})
    except Exception:
        pass
    return out

def build_output_device_list() -> List[str]:
    names = ["default"]
    for s in list_pactl_sinks():
        if s not in names:
            names.append(s)
    for d in query_sd_output_devices():
        if d['name'] not in names:
            names.append(d['name'])
    return names

def find_sd_device_index_by_name(name: str) -> Optional[int]:
    if not SD_AVAILABLE or name is None:
        return None
    try:
        if isinstance(name, (int, float)) or (isinstance(name, str) and name.isdigit()):
            return int(name)
    except Exception:
        pass
    try:
        devs = sd.query_devices()
    except Exception:
        return None
    for i, d in enumerate(devs):
        if d.get('name') == name and d.get('max_output_channels', 0) > 0:
            return i
    lname = (name or "").lower()
    for i, d in enumerate(devs):
        if d.get('max_output_channels', 0) > 0 and lname in d.get('name', '').lower():
            return i
    for i, d in enumerate(devs):
        if d.get('max_output_channels', 0) > 0:
            return i
    return None

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
            subprocess.check_call([
                "ffmpeg", "-y", "-v", "error", "-i", str(path),
                "-ar", "48000", "-ac", "2", "-f", "wav", tmp.name
            ])
            sf_obj = sf.SoundFile(tmp.name)
            return sf_obj, tmp.name
        except Exception as e:
            try:
                os.unlink(tmp.name)
            except Exception:
                pass
            raise e

# ---------- helper: pactl move sink input ----------
def pactl_move_sink_input_to_sink(sink_input_idx: int, sink_name: str) -> bool:
    try:
        subprocess.check_call(['pactl', 'move-sink-input', str(sink_input_idx), sink_name])
        return True
    except Exception as e:
        return False

def pactl_find_sink_inputs_for_process_and_media(pid: int, media_substr: str = "") -> List[int]:
    """Return list of sink_input indexes that belong to this PID or mention media_substr"""
    try:
        out = subprocess.check_output(['pactl', 'list', 'sink-inputs'], text=True, stderr=subprocess.DEVNULL)
        lines = out.splitlines()
        idx = None
        sinks = []
        current = {}
        for ln in lines:
            ln = ln.rstrip()
            if ln.startswith('Sink Input #'):
                if current.get('index') is not None:
                    # evaluate previous block
                    proplist = current.get('properties','')
                    apid = ''
                    for l in proplist.splitlines():
                        if 'application.process.id' in l:
                            apid = l.split('=')[-1].strip().strip('"')
                    media_name = ''
                    for l in proplist.splitlines():
                        if 'media.name' in l or 'application.name' in l:
                            media_name = l.split('=')[-1].strip().strip('"')
                    if apid and str(pid) == apid:
                        sinks.append(current['index'])
                    elif media_substr and media_substr in media_name:
                        sinks.append(current['index'])
                # start new
                idx = int(ln.split('#')[-1])
                current = {'index': idx, 'properties': ''}
            else:
                if 'Properties:' in ln:
                    # subsequent lines until blank are properties
                    pass
                if current is not None:
                    current['properties'] += ln + '\n'
        # final block
        if current.get('index') is not None:
            proplist = current.get('properties','')
            apid = ''
            for l in proplist.splitlines():
                if 'application.process.id' in l:
                    apid = l.split('=')[-1].strip().strip('"')
            media_name = ''
            for l in proplist.splitlines():
                if 'media.name' in l or 'application.name' in l:
                    media_name = l.split('=')[-1].strip().strip('"')
            if apid and str(pid) == apid:
                sinks.append(current['index'])
            elif media_substr and media_substr in media_name:
                sinks.append(current['index'])
        return sinks
    except Exception:
        return []

# ---------------------------
# TrackPlayer using sounddevice
# ---------------------------
class TrackPlayerSD:
    def __init__(self, path: Path, device_name: Optional[str] = None):
        self.path = Path(path)
        self.device_name = device_name  # stored as string (pactl name or sd device name)
        self.device_index = None
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
        self.moved_sink_inputs = set()
        self._open_file()

    def _open_file(self):
        if not SF_AVAILABLE:
            raise RuntimeError("soundfile not installed")
        sf_obj = None; tmp = None
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

    def _resolve_device_index(self):
        if not SD_AVAILABLE:
            self.device_index = None
            return None
        if self.device_name is None or self.device_name == "default":
            self.device_index = None
            return None
        # try numeric
        try:
            if isinstance(self.device_name, (int, float)) or (isinstance(self.device_name, str) and self.device_name.isdigit()):
                self.device_index = int(self.device_name)
                return self.device_index
        except Exception:
            pass
        idx = find_sd_device_index_by_name(self.device_name)
        self.device_index = idx
        return idx

    def _create_stream(self):
        if not SD_AVAILABLE:
            return False
        if self.stream:
            try:
                self.stream.stop(); self.stream.close()
            except Exception:
                pass
            self.stream = None
        dev_idx = self._resolve_device_index()
        try:
            self.stream = sd.OutputStream(
                samplerate=self.samplerate,
                blocksize=self.blocksize,
                device=dev_idx,
                channels=self.channels,
                dtype='float32',
                callback=self._callback,
                finished_callback=self._stream_finished
            )
            return True
        except Exception as e:
            try:
                print(f"[AudioRouting] Warning: failed to open device {self.device_name} -> {e}. Falling back to default device.")
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
                print(f"[AudioRouting] Failed to create stream for {self.path.name}: {e2}")
                self.stream = None
                return False

    def set_device_by_name(self, device_name: Optional[str]):
        old = self.device_name
        self.device_name = device_name
        print(f"[AudioRouting] Setting device for {self.path.name}: {old or 'default'} -> {self.device_name or 'default'}")
        was_playing = self.playing
        pos = self.get_time_pos()
        try:
            self.stop()
            try:
                if self.sf:
                    try:
                        self.sf.close()
                    except Exception:
                        pass
                self._open_file()
            except Exception as e:
                print(f"[AudioRouting] Warning reopening file: {e}")
            try:
                self.sf.seek(int(pos * self.sf.samplerate))
                self.position = pos
            except Exception:
                self.position = pos
            created = self._create_stream()
            if created and was_playing:
                try:
                    self.play(start_pos=pos)
                except Exception as e:
                    print(f"[AudioRouting] Error restarting playback after device change: {e}")
            print(f"[AudioRouting] Device set for {self.path.name} -> {self.device_name} (resolved index={self.device_index})")
        except Exception as e:
            print(f"[AudioRouting] Exception in set_device_by_name: {e}")

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
            try:
                self.sf.seek(int(start_pos * self.sf.samplerate))
            except Exception:
                pass
            self.position = start_pos
            if not self.stream:
                created = self._create_stream()
                if not created:
                    print(f"[AudioRouting] Could not create stream for {self.path.name}")
                    self.playing = False
                    return
            try:
                if not self.stream.active:
                    self.stream.start()
            except Exception as e:
                print(f"[AudioRouting] Error starting stream for {self.path.name}: {e}")
                self.playing = False
                return
            # after stream started, try to move sink_input to sink_name using pactl (best effort)
            threading.Thread(target=self._try_move_sink_input_after_start, daemon=True).start()

    def _try_move_sink_input_after_start(self):
        # Wait a bit for Pulse to create sink_input for this process/stream
        time.sleep(0.12)
        if not self.device_name or self.device_name == "default":
            return
        pid = os.getpid()
        # try to find sink_input indexes that match our pid or file name
        sink_inputs = pactl_find_sink_inputs_for_process_and_media(pid, media_substr=self.path.name)
        if not sink_inputs:
            # try heuristic with short wait
            time.sleep(0.12)
            sink_inputs = pactl_find_sink_inputs_for_process_and_media(pid, media_substr=self.path.name)
        sinks = list_pactl_sinks()
        # try to match sink by exact name or substring
        target_sink = None
        for s in sinks:
            if s == self.device_name or self.device_name in s:
                target_sink = s; break
        if not target_sink:
            # try description match via pactl (not available here reliably) => use device_name as sink index/name anyway
            target_sink = self.device_name
        moved_any = False
        for si in sink_inputs:
            try:
                ok = pactl_move_sink_input_to_sink(si, target_sink)
                if ok:
                    print(f"[AudioRouting] Moved sink_input {si} -> {target_sink}")
                    moved_any = True
            except Exception as e:
                print(f"[AudioRouting] Failed to move sink_input {si} -> {target_sink}: {e}")
        if not moved_any and sink_inputs:
            print(f"[AudioRouting] Could not move sink_inputs {sink_inputs} -> {target_sink}")

    def _stream_finished(self):
        self.playing = False

    def _callback(self, outdata, frames, time_info, status):
        if self.stop_flag.is_set():
            outdata[:] = np.zeros((frames, self.channels), dtype='float32')
            raise sd.CallbackStop()
        with self.lock:
            rate = float(self.playback_rate)
            vol = float(self.volume)
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
                src_pos = (np.arange(frames) / rate)
                xs = np.arange(src_len)
                out = np.zeros((frames, src.shape[1]), dtype='float32')
                for ch in range(src.shape[1]):
                    ys = src[:, ch]
                    xp = np.concatenate([xs, [xs[-1] + 1]])
                    fp = np.concatenate([ys, [0.0]])
                    out[:, ch] = np.interp(src_pos, xp, fp).astype('float32')
            out *= vol
            self.position += frames / float(self.sf.samplerate) * rate
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
# Timeline widget (pulse only inside loop & only when loop enabled)
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
        pulse_alpha = int(base_alpha + 120 * (0.5 + 0.5 * sin(self.pulse_phase)))
        loop_alpha = base_alpha
        # -- IMPORTANT: pulsing only when loop playback is ON is handled by MainWindow
        if getattr(self, 'loop_pulse_active', False):
            loop_alpha = pulse_alpha
        p.setBrush(QColor(80, 160, 200, loop_alpha))
        p.drawRect(QRectF(lsx, bar_y, max(4.0, lex - lsx), bar_h))

        posx = x_for(self.position)
        prog_rect = QRectF(10.0, bar_y, max(2.0, posx - 10.0), bar_h)
        prog_base_alpha = 200
        prog_pulse = 0
        if getattr(self, 'prog_pulse_active', False):
            prog_pulse = int(100 * (0.5 + 0.5 * sin(self.pulse_phase)))
        prog_alpha = min(255, prog_base_alpha + prog_pulse)
        col = QColor(102, 187, 106)
        col.setAlpha(prog_alpha)
        p.setBrush(col)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRect(prog_rect)

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
# TrackRow UI (with Test button)
# ---------------------------
class TrackRow(QWidget):
    def __init__(self, filepath: str, device_names: List[str], settings: dict):
        super().__init__()
        self.filepath = filepath
        self.device_names = device_names
        self.settings = settings or {}
        self.player: Optional[TrackPlayerSD] = None
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
        for name in self.device_names:
            self.sink_combo.addItem(name, name)
        self.sink_combo.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        layout.addWidget(self.sink_combo, 0)
        self.test_btn = QPushButton("Test"); self.test_btn.setFixedWidth(56)
        layout.addWidget(self.test_btn)

        self.sink_combo.currentIndexChanged.connect(self._on_sink_changed)
        self.vol_slider.valueChanged.connect(self._on_volume_changed)
        self.test_btn.clicked.connect(self._on_test_clicked)

    def _load_settings(self):
        n = Path(self.filepath).name
        e = self.settings.get(n, {})
        vol = e.get('volume', 100)
        self.vol_slider.setValue(int(vol)); self.vol_label.setText(f"{int(vol)}%")
        dev = e.get('sink')
        if dev is not None:
            idx = self.sink_combo.findData(dev)
            if idx != -1:
                self.sink_combo.setCurrentIndex(idx)

    def set_player(self, player: TrackPlayerSD):
        self.player = player
        try:
            self.player.set_volume_db(self.vol_slider.value())
            sel = self.sink_combo.currentData()
            if sel is not None:
                self.player.set_device_by_name(sel)
        except Exception:
            pass

    def _on_sink_changed(self, idx):
        data = self.sink_combo.currentData()
        if self.player:
            try:
                self.player.set_device_by_name(data)
            except Exception as e:
                print(f"[AudioRouting] Failed setting device: {e}")

    def _on_volume_changed(self, val):
        self.vol_label.setText(f"{int(val)}%")
        if self.player:
            try:
                self.player.set_volume_db(val)
            except Exception:
                pass

    def _on_test_clicked(self):
        dev_name = self.sink_combo.currentData()
        if not SD_AVAILABLE:
            QMessageBox.information(self, "Info", "sounddevice is not available for Test tone.")
            return
        idx = find_sd_device_index_by_name(dev_name) if dev_name and dev_name != "default" else None
        duration = 0.35; sr = 44100
        t = np.linspace(0, duration, int(sr * duration), endpoint=False)
        freq = 880.0
        sine = 0.2 * np.sin(2 * np.pi * freq * t).astype('float32')
        out = np.column_stack([sine, sine])
        try:
            sd.play(out, samplerate=sr, device=idx)
            threading.Timer(duration + 0.05, lambda: sd.stop()).start()
            # best-effort move to sink via pactl
            if dev_name and dev_name != "default":
                def mover():
                    time.sleep(0.12)
                    pid = os.getpid()
                    sink_inputs = pactl_find_sink_inputs_for_process_and_media(pid, media_substr=Path(self.filepath).name)
                    sinks = list_pactl_sinks()
                    target = None
                    for s in sinks:
                        if s == dev_name or dev_name in s:
                            target = s; break
                    if not target:
                        target = dev_name
                    for si in sink_inputs:
                        pactl_move_sink_input_to_sink(si, target)
                threading.Thread(target=mover, daemon=True).start()
        except Exception as e:
            QMessageBox.warning(self, "Test tone failed", f"Could not play test tone on device {dev_name}: {e}")

# ---------------------------
# Global Settings dialog
# ---------------------------
class GlobalSettingsDialog(QDialog):
    def __init__(self, parent, global_cfg):
        super().__init__(parent)
        self.setWindowTitle("Global Settings")
        self.global_cfg = global_cfg
        self._build_ui()

    def _build_ui(self):
        layout = QFormLayout(); self.setLayout(layout)
        self.default_folder_edit = QLineEdit(self.global_cfg.get('default_project_folder', str(Path.home())))
        btn_folder = QPushButton("Browse")
        def browse():
            d = QFileDialog.getExistingDirectory(self, "Select default project folder", self.default_folder_edit.text())
            if d:
                self.default_folder_edit.setText(d)
        btn_folder.clicked.connect(browse)
        row = QHBoxLayout(); row.addWidget(self.default_folder_edit); row.addWidget(btn_folder)
        layout.addRow("Default project folder:", row)

        self.tick_file_edit = QLineEdit(self.global_cfg.get('tick_file', ''))
        btn_tick = QPushButton("Browse")
        def browse_tick():
            f = QFileDialog.getOpenFileName(self, "Select tick sound file", str(Path.home()), "Audio files (*.wav *.ogg *.mp3 *.flac)")[0]
            if f:
                self.tick_file_edit.setText(f)
        btn_tick.clicked.connect(browse_tick)
        row2 = QHBoxLayout(); row2.addWidget(self.tick_file_edit); row2.addWidget(btn_tick)
        layout.addRow("Tick sound file (global):", row2)

        btns = QHBoxLayout()
        ok = QPushButton("OK"); cancel = QPushButton("Cancel")
        ok.clicked.connect(self.accept); cancel.clicked.connect(self.reject)
        btns.addWidget(ok); btns.addWidget(cancel)
        layout.addRow(btns)

    def accept(self):
        self.global_cfg['default_project_folder'] = self.default_folder_edit.text()
        self.global_cfg['tick_file'] = self.tick_file_edit.text()
        save_global_config(self.global_cfg)
        super().accept()

# ---------------------------
# Main Window
# ---------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Multitrack Player v17")
        self.resize(1200, 800)
        self.setStyleSheet("QWidget { background-color: #2f2f2f; color: #e6e6e6 }")

        self.global_cfg = load_global_config()
        # ensure defaults
        if 'default_project_folder' not in self.global_cfg:
            self.global_cfg['default_project_folder'] = str(Path.home())
        if 'tick_file' not in self.global_cfg:
            self.global_cfg['tick_file'] = ""

        self.device_names = build_output_device_list()

        self.settings = {}
        self.current_folder = None
        self.track_rows = []
        self.track_players = []

        self.timeline = Timeline(duration=10.0)
        self.tick_player = TickPlayer(self.global_cfg.get('tick_file'), vol=int(self.global_cfg.get('tick_volume', DEFAULT_TICK_VOL)))

        self._build_ui()

        self.ui_timer = QTimer(); self.ui_timer.setInterval(80)
        self._last_ui_time = time.time()
        self.ui_timer.timeout.connect(self.ui_tick); self.ui_timer.start()

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

        # timeline + controls
        v.addWidget(self.timeline)
        transport = QHBoxLayout()
        self.play_btn = QPushButton("Play"); transport.addWidget(self.play_btn)
        self.pause_btn = QPushButton("Pause"); transport.addWidget(self.pause_btn)
        self.stop_btn = QPushButton("Stop"); transport.addWidget(self.stop_btn)
        transport.addWidget(QLabel("Playback rate:"))
        self.rate_minus = QPushButton("–"); self.rate_plus = QPushButton("+")
        self.rate_minus.setFixedWidth(28); self.rate_plus.setFixedWidth(28)
        transport.addWidget(self.rate_minus)
        self.rate_label = QLabel("100%"); transport.addWidget(self.rate_label)
        transport.addWidget(self.rate_plus)
        v.addLayout(transport)

        loop_h = QHBoxLayout()
        self.loop_toggle = QCheckBox("Loop On"); loop_h.addWidget(self.loop_toggle)
        self.loop_save = QPushButton("Save Loop"); loop_h.addWidget(self.loop_save)
        self.loop_delete = QPushButton("Delete Loop"); loop_h.addWidget(self.loop_delete)
        self.loop_select = QComboBox(); loop_h.addWidget(self.loop_select, 1)
        loop_h.addWidget(QLabel("BPM:"))
        self.bpm_spin = QSpinBox(); self.bpm_spin.setRange(30, 300); self.bpm_spin.setValue(DEFAULT_BPM); loop_h.addWidget(self.bpm_spin)
        loop_h.addWidget(QLabel("Tick Enabled:"))
        self.tick_enabled_cb = QCheckBox("Tick"); loop_h.addWidget(self.tick_enabled_cb)
        loop_h.addWidget(QLabel("Tick sound (global):"))
        self.tick_label = QLineEdit(); self.tick_label.setReadOnly(True); self.tick_label.setText(self.global_cfg.get('tick_file','')); loop_h.addWidget(self.tick_label, 2)
        self.tick_browse = QPushButton("Browse"); loop_h.addWidget(self.tick_browse)
        loop_h.addWidget(QLabel("Tick vol:"))
        self.tick_vol_spin = QSpinBox(); self.tick_vol_spin.setRange(0, 200); self.tick_vol_spin.setValue(int(self.global_cfg.get('tick_volume', DEFAULT_TICK_VOL))); loop_h.addWidget(self.tick_vol_spin)
        v.addLayout(loop_h)

        self.tracks_area = QScrollArea(); self.tracks_widget = QWidget(); self.tracks_layout = QVBoxLayout(); self.tracks_widget.setLayout(self.tracks_layout)
        self.tracks_area.setWidgetResizable(True); self.tracks_area.setWidget(self.tracks_widget); v.addWidget(self.tracks_area)

        # connections
        self.open_btn.clicked.connect(self.on_open)
        self.default_btn.clicked.connect(self.on_set_default_folder)
        self.settings_btn.clicked.connect(self.on_global_settings)
        self.refresh_btn.clicked.connect(self.on_refresh_outputs)
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
        self.bpm_spin.valueChanged.connect(lambda v: setattr(self, 'project_bpm', int(v)))
        self.tick_browse.clicked.connect(self.on_browse_tick)
        self.tick_vol_spin.valueChanged.connect(self.on_tick_vol_changed)

    # ---------------------------
    # Folder & load
    # ---------------------------
    def on_set_default_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Select default project folder", self.global_cfg.get('default_project_folder', str(Path.home())))
        if d:
            self.global_cfg['default_project_folder'] = d
            save_global_config(self.global_cfg)
            QMessageBox.information(self, "Saved", f"Default project folder set to:\n{d}")

    def on_global_settings(self):
        dlg = GlobalSettingsDialog(self, self.global_cfg)
        dlg.exec()

    def on_open(self):
        start = self.global_cfg.get('default_project_folder', str(Path.home()))
        folder = QFileDialog.getExistingDirectory(self, "Select folder", start)
        if not folder:
            return
        self.current_folder = Path(folder)
        self.folder_label.setText(str(self.current_folder))
        # load project config
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
        # project bpm and playback rate
        self.project_bpm = g.get('bpm', DEFAULT_BPM)
        self.project_playback_rate = g.get('playback_rate', 1.0)
        self.bpm_spin.setValue(self.project_bpm)
        self.rate_label.setText(f"{int(round(self.project_playback_rate*100))}%")
        self.tick_enabled_cb.setChecked(g.get('tick_enabled', True))
        # tick file from global
        self.tick_label.setText(self.global_cfg.get('tick_file',''))
        self.tick_player.set_tick_file(self.global_cfg.get('tick_file',''))
        # build device list and load tracks
        self.device_names = build_output_device_list()
        self.load_tracks(folder)
        # autosize window based on content
        self._adjust_window_size_for_devices()

    def load_tracks(self, folder):
        # cleanup
        for p in self.track_players:
            try: p.stop()
            except Exception: pass
        for r in self.track_rows:
            try: r.deleteLater()
            except Exception: pass
        self.track_rows = []; self.track_players = []

        self.device_names = build_output_device_list()

        files = [p for p in sorted(Path(folder).iterdir()) if p.suffix.lower() in AUDIO_EXTS]
        durations = []
        for pth in files:
            row = TrackRow(str(pth), self.device_names, settings=self.settings)
            self.tracks_layout.addWidget(row)
            self.track_rows.append(row)
            assigned = None
            ent = self.settings.get(Path(pth).name, {})
            if ent:
                assigned = ent.get('sink')
            try:
                tp = TrackPlayerSD(pth, device_name=assigned)
                tp.set_playback_rate(self.project_playback_rate if hasattr(self,'project_playback_rate') else 1.0)
                row.set_player(tp)
                self.track_players.append(tp)
                durations.append(tp.get_duration())
                if assigned:
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

    def _adjust_window_size_for_devices(self):
        # compute longest device name width in pixels and adjust window width accordingly
        fm = QFontMetrics(self.font())
        max_name = 0
        for name in self.device_names:
            w = fm.horizontalAdvance(name)
            if w > max_name: max_name = w
        # base widths: file name label column + vol slider + small buttons
        target_width = 400 + max_name + 200
        target_width = min(max(target_width, 900), 2200)
        self.resize(int(target_width), self.height())

    def on_refresh_outputs(self):
        self.device_names = build_output_device_list()
        for r in self.track_rows:
            cur = r.sink_combo.currentData()
            r.sink_combo.blockSignals(True)
            r.sink_combo.clear()
            for name in self.device_names:
                r.sink_combo.addItem(name, name)
            if cur:
                idx = r.sink_combo.findData(cur)
                if idx != -1:
                    r.sink_combo.setCurrentIndex(idx)
            r.sink_combo.blockSignals(False)

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
        # apply per-track settings
        for idx, row in enumerate(self.track_rows):
            try:
                tp = self.track_players[idx]
                dev_name = row.sink_combo.currentData()
                if dev_name is not None:
                    tp.set_device_by_name(dev_name)
                tp.set_playback_rate(self.project_playback_rate if hasattr(self,'project_playback_rate') else 1.0)
                tp.set_volume_db(row.vol_slider.value())
            except Exception as e:
                print(f"[AudioRouting] Error applying settings to track {idx}: {e}")
        # start playback
        for tp in self.track_players:
            try:
                tp.play(start_pos=start)
            except Exception as e:
                print(f"[AudioRouting] Error starting track: {e}")
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
        for tp in self.track_players:
            tp.position = 0.0
        self.timeline.set_position(0.0)

    # ---------------------------
    # Playback rate
    # ---------------------------
    def _update_rate_label(self):
        self.rate_label.setText(f"{int(round(self.project_playback_rate*100))}%")

    def on_rate_plus(self):
        new = round(getattr(self,'project_playback_rate',1.0) + PLAYBACK_RATE_STEP, 3)
        if new > PLAYBACK_RATE_MAX: new = PLAYBACK_RATE_MAX
        self.project_playback_rate = new
        for tp in self.track_players:
            tp.set_playback_rate(self.project_playback_rate)
        self._update_rate_label()

    def on_rate_minus(self):
        new = round(getattr(self,'project_playback_rate',1.0) - PLAYBACK_RATE_STEP, 3)
        if new < PLAYBACK_RATE_MIN: new = PLAYBACK_RATE_MIN
        self.project_playback_rate = new
        for tp in self.track_players:
            tp.set_playback_rate(self.project_playback_rate)
        self._update_rate_label()

    # ---------------------------
    # Tick handling
    # ---------------------------
    def on_browse_tick(self):
        f = QFileDialog.getOpenFileName(self, "Select tick sound (global)", str(Path.home()), "Audio files (*.wav *.ogg *.mp3 *.flac)")[0]
        if not f: return
        self.global_cfg['tick_file'] = f
        self.tick_label.setText(f)
        self.tick_player.set_tick_file(f)
        save_global_config(self.global_cfg)

    def on_tick_vol_changed(self, v):
        self.tick_player.vol = int(v)
        self.global_cfg['tick_volume'] = int(v)
        save_global_config(self.global_cfg)

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
            'tick_enabled': bool(self.tick_enabled_cb.isChecked()),
            'playback_rate': float(getattr(self,'project_playback_rate',1.0))
        }
        try:
            with open(self.current_folder / SETTINGS_NAME, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save folder settings: {e}")
        save_global_config(self.global_cfg)
        QMessageBox.information(self, "Saved", "Settings saved (project + global).")

    # ---------------------------
    # UI tick
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
        # loop handling & pulsing activation
        loop_on = self.loop_toggle.isChecked()
        inside_loop = (self.timeline.loop_start <= pos <= self.timeline.loop_end)
        self.timeline.loop_pulse_active = loop_on and inside_loop
        self.timeline.prog_pulse_active = loop_on and inside_loop
        if loop_on and inside_loop:
            # if reached loop end, seek to start
            if pos >= self.timeline.loop_end - 0.02:
                for p in self.track_players:
                    try:
                        p.seek(self.timeline.loop_start)
                    except Exception:
                        pass
                self.timeline.set_position(self.timeline.loop_start)
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
