#!/usr/bin/env python3
# multitrack_player_v10.py
# Multitrack Player v10 - PyQt6, Python 3.12
# Realtime global pitch shift (rubberband/scaletempo via mpv af), ticks via pygame.mixer,
# loop save/overwrite/delete, per-track volume/mute/solo, JSON per-folder settings.
#
# Usage:
#   python3 multitrack_player_v10.py

import os
import locale

# Ensure C numeric locale for libmpv (prevents Non-C locale segfaults)
locale.setlocale(locale.LC_NUMERIC, "C")
os.environ["LC_NUMERIC"] = "C"

import sys
import json
import time
import math
import subprocess
import threading
from pathlib import Path
from typing import Optional, Dict

from PyQt6.QtWidgets import (
    QApplication, QWidget, QMainWindow, QFileDialog, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QSlider, QScrollArea, QMessageBox, QComboBox,
    QCheckBox, QInputDialog, QSpinBox, QLineEdit
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QRectF, QPointF, QUrl
from PyQt6.QtGui import QPainter, QColor, QPen

# pygame used for low-latency tick playback in a separate thread
try:
    import pygame
    PYGAME_AVAILABLE = True
except Exception:
    PYGAME_AVAILABLE = False

try:
    import mpv
except Exception:
    mpv = None

# Constants
SETTINGS_NAME = "multitrack_config.json"
AUDIO_EXTS = ('.wav', '.mp3', '.flac', '.ogg', '.m4a')
DEFAULT_BPM = 120
DEFAULT_TICK_VOL = 80  # percentage for tick sound
PITCH_ENGINE_OPTIONS = ["rubberband", "scaletempo"]
DEFAULT_PITCH_ENGINE = "rubberband"


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


def slider_to_mpv_volume_linear(slider_value: int) -> float:
    return float(slider_value)


def semitone_to_scale(semitones: int) -> float:
    return 2.0 ** (float(semitones) / 12.0)


# ---------------------------
# TrackPlayer wrapper (mpv)
# ---------------------------
class TrackPlayer:
    def __init__(self, file_path, sink_name=None):
        self.file_path = str(file_path)
        self.sink_name = sink_name
        self.player = None
        self.is_playing = False
        self.create_player()

    def create_player(self, volume=100.0):
        opts = {'no_video': True, 'volume': volume, 'keep-open': True, 'msg-level': 'all=no'}
        try:
            self.player = mpv.MPV(**opts) if mpv else None
        except Exception:
            # fallback to simple creation
            try:
                self.player = mpv.MPV() if mpv else None
                if self.player:
                    try:
                        self.player.volume = volume
                    except Exception:
                        pass
            except Exception:
                self.player = None

    def set_sink(self, sink_name):
        self.sink_name = sink_name
        if not self.player:
            return
        try:
            # PulseAudio device by name
            self.player.set_property('audio-device', f"pulse/{sink_name}")
        except Exception:
            pass

    def set_volume(self, vol_pct: float):
        if self.player:
            try:
                self.player.set_property('volume', float(vol_pct))
            except Exception:
                try:
                    self.player.volume = float(vol_pct)
                except Exception:
                    pass

    def apply_pitch_af(self, semitones: int, engine: str):
        """
        Apply an audio filter (af) chain to perform pitch shift without changing tempo.
        Using 'rubberband' if available (pitch-scale), else a scaletempo/asetrate chain.
        """
        if not self.player:
            return False
        scale = semitone_to_scale(int(semitones))
        if engine == "rubberband":
            af = f"rubberband=pitch-scale={scale},tempo-scale=1.0"
        else:
            # scaletempo approach: asetrate * scale -> scaletempo -> aresample
            # Note: sample rate hardcoded to 48000 in chain; if files differ, mpv/a resample may handle it
            af = f"asetrate=48000*{scale},scaletempo,aresample=48000"
        try:
            # clear previous filters and add new
            try:
                self.player.command('af', 'clear')
            except Exception:
                pass
            try:
                # Use 'af add' — should apply immediately in mpv
                self.player.command('af', 'add', af)
                return True
            except Exception:
                try:
                    self.player.set_property('af', af)
                    return True
                except Exception:
                    return False
        except Exception:
            return False

    def play(self, start_pos: float = 0.0, speed: float = 1.0):
        if not self.player:
            self.create_player()
        try:
            # ensure any AF is applied before play
            # note: apply_pitch_af should have been called already by main window
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

    def get_time_pos(self) -> float:
        if not self.player:
            return 0.0
        try:
            pos = self.player.time_pos
            return float(pos) if pos else 0.0
        except Exception:
            return 0.0

    def seek(self, seconds: float):
        if not self.player:
            return
        try:
            self.player.seek(seconds, reference='absolute')
        except Exception:
            pass

    def get_duration(self) -> float:
        if not self.player:
            return 0.0
        try:
            d = self.player.duration
            return float(d) if d else 0.0
        except Exception:
            return 0.0


# ---------------------------
# Tick player using pygame in background thread (preferred)
# ---------------------------
class TickPlayer:
    def __init__(self, tick_file: Optional[str] = None, tick_volume: int = DEFAULT_TICK_VOL):
        self.tick_file = tick_file
        self.tick_volume = int(tick_volume)
        self._inited = False
        if PYGAME_AVAILABLE:
            try:
                # small buffer for low latency
                pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
                self._inited = True
            except Exception:
                self._inited = False
        # pre-load sound if possible
        self.sound = None
        if self._inited and self.tick_file and Path(self.tick_file).exists():
            try:
                self.sound = pygame.mixer.Sound(self.tick_file)
                self.sound.set_volume(max(0.0, min(1.0, self.tick_volume / 100.0)))
            except Exception:
                self.sound = None

    def set_tick_file(self, tick_file: Optional[str]):
        self.tick_file = tick_file
        if self._inited:
            try:
                if self.tick_file and Path(self.tick_file).exists():
                    self.sound = pygame.mixer.Sound(self.tick_file)
                    self.sound.set_volume(max(0.0, min(1.0, self.tick_volume / 100.0)))
                else:
                    self.sound = None
            except Exception:
                self.sound = None

    def set_tick_volume(self, vol_percent: int):
        self.tick_volume = int(vol_percent)
        if self.sound:
            try:
                self.sound.set_volume(max(0.0, min(1.0, self.tick_volume / 100.0)))
            except Exception:
                pass

    def play_tick(self):
        # Try pygame first (low latency)
        if self._inited and self.sound:
            try:
                self.sound.play()
                return True
            except Exception:
                pass
        # fallback to mpv subprocess if tick file exists
        if self.tick_file and Path(self.tick_file).exists():
            try:
                subprocess.Popen(['mpv', '--no-video', '--really-quiet', f'--volume={self.tick_volume}', self.tick_file])
                return True
            except Exception:
                pass
        # fallback to paplay default sound
        try:
            subprocess.Popen(['paplay', '/usr/share/sounds/alsa/Front_Center.wav'])
            return True
        except Exception:
            return False


# ---------------------------
# Timeline Widget (same as before)
# ---------------------------
class Timeline(QWidget):
    seekRequested = pyqtSignal(float)
    loopChanged = pyqtSignal(float, float)

    def __init__(self, duration=1.0):
        super().__init__()
        self.duration = max(1.0, float(duration))
        self.position = 0.0
        self.loop_start = 0.0
        self.loop_end = self.duration
        self.dragging = None
        self.setMinimumHeight(48)
        self.setMouseTracking(True)

    def set_duration(self, d):
        self.duration = max(1.0, float(d))
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
        w = float(rect.width()); h = float(rect.height())
        p.fillRect(rect, QColor("#3a3a3a"))
        bar_h = 12.0
        bar_y = float(h / 2.0 - bar_h / 2.0)
        bar_rect = QRectF(10.0, bar_y, max(10.0, w - 20.0), bar_h)
        p.setBrush(QColor("#2f2f2f"))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(bar_rect, 4.0, 4.0)

        def x_for(t):
            return 10.0 + (w - 20.0) * (t / self.duration)

        lsx = x_for(self.loop_start)
        lex = x_for(self.loop_end)

        loop_rect = QRectF(lsx, bar_y, max(4.0, lex - lsx), bar_h)
        p.setBrush(QColor(80, 160, 200, 120))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRect(loop_rect)

        posx = x_for(self.position)
        prog_rect = QRectF(10.0, bar_y, max(2.0, posx - 10.0), bar_h)
        p.setBrush(QColor("#66bb6a"))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRect(prog_rect)

        handle_w = 10.0; handle_h = 18.0
        p.setBrush(QColor("#bbbbbb"))
        p.setPen(QPen(QColor("#888888")))
        handle1 = QRectF(lsx - handle_w / 2.0, bar_y - handle_h / 2.0 + bar_h / 2.0, handle_w, handle_h)
        handle2 = QRectF(lex - handle_w / 2.0, bar_y - handle_h / 2.0 + bar_h / 2.0, handle_w, handle_h)
        p.drawRect(handle1); p.drawRect(handle2)

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
        lsx = 10.0 + (w - 20.0) * (self.loop_start / self.duration)
        lex = 10.0 + (w - 20.0) * (self.loop_end / self.duration)
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
    def __init__(self, filename, sinks, assigned_sink=None, settings=None):
        super().__init__()
        self.filename = str(filename)
        self.sinks = sinks
        self.assigned_sink = assigned_sink
        self.settings = settings or {}
        self.player = TrackPlayer(filename, sink_name=assigned_sink)
        self.current_volume_slider_value = 100
        self.muted = False
        self.solo = False
        self._build_ui()
        self._load_settings()

    def _build_ui(self):
        layout = QHBoxLayout(); self.setLayout(layout)
        self.name_label = QLabel(Path(self.filename).name); layout.addWidget(self.name_label, 3)
        self.sink_combo = QComboBox()
        for s in self.sinks: self.sink_combo.addItem(s[1], s[1])
        layout.addWidget(self.sink_combo, 2)
        self.vol_slider = QSlider(Qt.Orientation.Horizontal); self.vol_slider.setRange(0, 120); self.vol_slider.setValue(100)
        layout.addWidget(QLabel("Vol")); layout.addWidget(self.vol_slider, 1)
        self.vol_label = QLabel("100%"); layout.addWidget(self.vol_label)
        self.mute_cb = QCheckBox("Mute"); layout.addWidget(self.mute_cb)
        self.solo_cb = QCheckBox("Solo"); layout.addWidget(self.solo_cb)
        self.sink_combo.currentIndexChanged.connect(self._on_sink_changed)
        self.vol_slider.valueChanged.connect(self._on_volume_changed)
        self.mute_cb.toggled.connect(self._on_mute_toggled)
        self.solo_cb.toggled.connect(self._on_solo_toggled)

    def _load_settings(self):
        name = Path(self.filename).name
        entry = self.settings.get(name, {})
        vol = entry.get('volume', 100)
        self.vol_slider.setValue(int(vol)); self.current_volume_slider_value = int(vol)
        self.muted = bool(entry.get('mute', False)); self.solo = bool(entry.get('solo', False))
        self.mute_cb.setChecked(self.muted); self.solo_cb.setChecked(self.solo)
        sink = entry.get('sink')
        if sink:
            idx2 = self.sink_combo.findData(sink)
            if idx2 != -1: self.sink_combo.setCurrentIndex(idx2)
        try:
            pv = slider_to_mpv_volume_linear(self.current_volume_slider_value)
            self.player.set_volume(pv)
            if self.assigned_sink:
                self.player.set_sink(self.assigned_sink)
        except Exception:
            pass

    def save_settings(self):
        return {
            'sink': self.sink_combo.currentData(),
            'volume': int(self.current_volume_slider_value),
            'mute': bool(self.mute_cb.isChecked()),
            'solo': bool(self.solo_cb.isChecked())
        }

    def _on_sink_changed(self, idx):
        sink = self.sink_combo.currentData(); self.player.set_sink(sink)

    def _on_volume_changed(self, val):
        self.current_volume_slider_value = int(val); self.vol_label.setText(f"{int(val)}%")
        try:
            pv = slider_to_mpv_volume_linear(self.current_volume_slider_value)
            self.player.set_volume(pv)
        except Exception:
            pass

    def _on_mute_toggled(self, state):
        self.muted = bool(state)

    def _on_solo_toggled(self, state):
        self.solo = bool(state)


# ---------------------------
# MainWindow
# ---------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Multitrack Player v10 - PyQt6")
        self.resize(1220, 860)
        self.setStyleSheet("""
            QWidget { background-color: #2f2f2f; color: #e6e6e6; }
            QPushButton { background-color: #444444; border: none; padding: 6px; border-radius: 6px; }
            QPushButton:hover { background-color: #555555; }
            QComboBox, QSpinBox, QLineEdit { background-color: #3a3a3a; padding: 3px; }
        """)

        self.sinks = list_pactl_sinks()
        self.settings: Dict = {}
        self.current_folder: Optional[Path] = None
        self.track_rows = []
        self.global_players = []
        self.global_pitch_semitones = 0
        self.global_pitch_engine = DEFAULT_PITCH_ENGINE
        self.global_playback_rate = 1.0
        self.loop_list: Dict[str, list] = {}
        self.current_loop = None

        # ticks
        self.bpm = DEFAULT_BPM
        self.tick_file = None
        self.tick_volume = DEFAULT_TICK_VOL
        self.countin_enabled = True
        self.tick_count = 4
        self.tick_player = TickPlayer(self.tick_file, tick_volume=self.tick_volume)

        central = QWidget(); self.setCentralWidget(central); v = QVBoxLayout(); central.setLayout(v)

        # top controls
        top = QHBoxLayout()
        self.open_btn = QPushButton("Open Folder"); top.addWidget(self.open_btn)
        self.folder_label = QLabel("No folder selected"); top.addWidget(self.folder_label, 1)
        self.refresh_btn = QPushButton("Refresh Outputs"); top.addWidget(self.refresh_btn)
        self.save_btn = QPushButton("Save Settings"); top.addWidget(self.save_btn)
        v.addLayout(top)

        # timeline and transport
        self.timeline = Timeline(duration=10.0); v.addWidget(self.timeline)
        transport = QHBoxLayout()
        self.play_btn = QPushButton("Play"); transport.addWidget(self.play_btn)
        self.pause_btn = QPushButton("Pause"); transport.addWidget(self.pause_btn)
        self.stop_btn = QPushButton("Stop"); transport.addWidget(self.stop_btn)
        transport.addWidget(QLabel("Pitch engine:"))
        self.pitch_engine_combo = QComboBox()
        for e in PITCH_ENGINE_OPTIONS: self.pitch_engine_combo.addItem(e, e)
        transport.addWidget(self.pitch_engine_combo)
        transport.addWidget(QLabel("Global pitch (semitones):"))
        self.global_pitch_combo = QComboBox()
        for s in range(-12, 13): self.global_pitch_combo.addItem(f"{s:+d}", s)
        transport.addWidget(self.global_pitch_combo)
        transport.addWidget(QLabel("Playback rate:"))
        self.playback_rate_label = QLabel("1.00x"); transport.addWidget(self.playback_rate_label)
        v.addLayout(transport)

        # loop & tick controls
        loop_h = QHBoxLayout()
        self.loop_toggle = QCheckBox("Loop On"); loop_h.addWidget(self.loop_toggle)
        self.loop_save_btn = QPushButton("Save Loop"); loop_h.addWidget(self.loop_save_btn)
        self.loop_delete_btn = QPushButton("Delete Loop"); loop_h.addWidget(self.loop_delete_btn)
        self.loop_select = QComboBox(); loop_h.addWidget(self.loop_select, 1)
        loop_h.addWidget(QLabel("BPM:"))
        self.bpm_spin = QSpinBox(); self.bpm_spin.setRange(30, 300); self.bpm_spin.setValue(self.bpm); loop_h.addWidget(self.bpm_spin)
        loop_h.addWidget(QLabel("Tick count:"))
        self.tick_count_spin = QSpinBox(); self.tick_count_spin.setRange(1, 16); self.tick_count_spin.setValue(self.tick_count); loop_h.addWidget(self.tick_count_spin)
        self.countin_cb = QCheckBox("Count-in"); self.countin_cb.setChecked(True); loop_h.addWidget(self.countin_cb)
        loop_h.addWidget(QLabel("Tick sound:"))
        self.tick_label = QLineEdit(); self.tick_label.setReadOnly(True); loop_h.addWidget(self.tick_label, 2)
        self.tick_browse = QPushButton("Browse"); loop_h.addWidget(self.tick_browse)
        loop_h.addWidget(QLabel("Tick vol:"))
        self.tick_vol_spin = QSpinBox(); self.tick_vol_spin.setRange(0, 200); self.tick_vol_spin.setValue(self.tick_volume); loop_h.addWidget(self.tick_vol_spin)
        v.addLayout(loop_h)

        # tracks area
        self.tracks_area = QScrollArea(); self.tracks_widget = QWidget(); self.tracks_layout = QVBoxLayout(); self.tracks_widget.setLayout(self.tracks_layout)
        self.tracks_area.setWidgetResizable(True); self.tracks_area.setWidget(self.tracks_widget); v.addWidget(self.tracks_area)

        # signals
        self.open_btn.clicked.connect(self.on_open)
        self.refresh_btn.clicked.connect(self.on_refresh)
        self.save_btn.clicked.connect(self.on_save)
        self.play_btn.clicked.connect(self.on_play)
        self.pause_btn.clicked.connect(self.on_pause)
        self.stop_btn.clicked.connect(self.on_stop)
        self.pitch_engine_combo.currentIndexChanged.connect(self.on_pitch_engine_changed)
        self.global_pitch_combo.currentIndexChanged.connect(self.on_global_pitch_changed)
        self.loop_save_btn.clicked.connect(self.on_save_loop)
        self.loop_delete_btn.clicked.connect(self.on_delete_loop)
        self.loop_select.currentIndexChanged.connect(self.on_loop_selected)
        self.timeline.seekRequested.connect(self.on_seek_requested)
        self.timeline.loopChanged.connect(self.on_loop_changed)
        self.loop_toggle.stateChanged.connect(self.on_loop_toggled)
        self.bpm_spin.valueChanged.connect(self.on_bpm_changed)
        self.tick_browse.clicked.connect(self.on_browse_tick)
        self.countin_cb.toggled.connect(self.on_countin_toggled)
        self.tick_vol_spin.valueChanged.connect(self.on_tick_vol_changed)
        self.tick_count_spin.valueChanged.connect(self.on_tick_count_changed)

        # UI timer
        self.ui_timer = QTimer(); self.ui_timer.setInterval(100); self.ui_timer.timeout.connect(self.ui_tick); self.ui_timer.start()

    # ---------------------------
    # File & tracks
    # ---------------------------
    def on_open(self):
        folder = QFileDialog.getExistingDirectory(self, "Select folder with audio files", str(Path.home()))
        if not folder:
            return
        self.current_folder = Path(folder)
        self.folder_label.setText(folder)
        settings_path = self.current_folder / SETTINGS_NAME
        if settings_path.exists():
            try:
                with open(settings_path, 'r') as f: self.settings = json.load(f)
            except Exception:
                self.settings = {}
        else:
            self.settings = {}
        # load globals
        g = self.settings.get('_global', {})
        self.bpm = g.get('bpm', self.bpm); self.bpm_spin.setValue(self.bpm)
        self.tick_file = g.get('tick_file', self.tick_file)
        if self.tick_file:
            self.tick_label.setText(self.tick_file); self.tick_player.set_tick_file(self.tick_file)
        self.tick_volume = g.get('tick_volume', self.tick_volume); self.tick_vol_spin.setValue(self.tick_volume)
        self.tick_player.set_tick_volume(self.tick_volume)
        self.countin_enabled = g.get('countin_enabled', self.countin_enabled); self.countin_cb.setChecked(self.countin_enabled)
        self.tick_count = g.get('tick_count', self.tick_count); self.tick_count_spin.setValue(self.tick_count)
        self.global_pitch_semitones = g.get('global_pitch', self.global_pitch_semitones)
        idx = self.global_pitch_combo.findData(self.global_pitch_semitones)
        if idx != -1: self.global_pitch_combo.setCurrentIndex(idx)
        self.global_pitch_engine = g.get('pitch_engine', self.global_pitch_engine)
        idx2 = self.pitch_engine_combo.findData(self.global_pitch_engine)
        if idx2 != -1: self.pitch_engine_combo.setCurrentIndex(idx2)
        # load tracks
        self.load_tracks(folder)

    def load_tracks(self, folder):
        for r in self.track_rows:
            try: r.player.stop(); r.deleteLater()
            except Exception: pass
        self.track_rows = []; self.global_players = []
        self.sinks = list_pactl_sinks()
        files = [p for p in sorted(Path(folder).iterdir()) if p.suffix.lower() in AUDIO_EXTS]
        for p in files:
            assigned = None
            s = self.settings.get(p.name, {})
            if s:
                assigned = s.get('sink')
            row = TrackRow(str(p), self.sinks, assigned_sink=assigned, settings=self.settings)
            self.tracks_layout.addWidget(row); self.track_rows.append(row); self.global_players.append(row.player)
        self.tracks_layout.addStretch(1)
        g = self.settings.get('_global', {})
        loops = g.get('loops', {})
        if loops:
            self.loop_list = loops; self.update_loop_dropdown()
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

    # ---------------------------
    # Mute/Solo/Volume
    # ---------------------------
    def apply_mute_solo_logic(self):
        solos = [r for r in self.track_rows if r.solo_cb.isChecked()]
        if solos:
            for r in self.track_rows:
                try:
                    if r.solo_cb.isChecked() and not r.mute_cb.isChecked():
                        pv = slider_to_mpv_volume_linear(r.current_volume_slider_value)
                        r.player.set_volume(pv)
                    else:
                        r.player.set_volume(0.0)
                except Exception:
                    pass
        else:
            for r in self.track_rows:
                try:
                    if r.mute_cb.isChecked():
                        r.player.set_volume(0.0)
                    else:
                        pv = slider_to_mpv_volume_linear(r.current_volume_slider_value)
                        r.player.set_volume(pv)
                except Exception:
                    pass

    # ---------------------------
    # Save/load config
    # ---------------------------
    def on_save(self):
        if not self.current_folder:
            QMessageBox.information(self, "Info", "No folder selected"); return
        self.save_config(self.current_folder)

    def save_config(self, folder: Path):
        data = {}
        for row in self.track_rows:
            data[Path(row.filename).name] = row.save_settings()
        data['_global'] = {
            'loops': self.loop_list,
            'bpm': int(self.bpm_spin.value()),
            'tick_file': self.tick_file,
            'tick_volume': int(self.tick_vol_spin.value()),
            'countin_enabled': bool(self.countin_cb.isChecked()),
            'tick_count': int(self.tick_count_spin.value()),
            'pitch_engine': self.global_pitch_engine,
            'global_pitch': int(self.global_pitch_semitones)
        }
        settings_path = Path(folder) / SETTINGS_NAME
        try:
            with open(settings_path, 'w') as f: json.dump(data, f, indent=2)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save settings: {e}")

    def update_loop_dropdown(self):
        self.loop_select.blockSignals(True)
        self.loop_select.clear()
        self.loop_select.addItem("Select loop...", None)
        for name, rng in self.loop_list.items():
            self.loop_select.addItem(name, (name, rng))
        self.loop_select.blockSignals(False)

    # ---------------------------
    # Loop save/overwrite/delete
    # ---------------------------
    def on_save_loop(self):
        name, ok = QInputDialog.getText(self, "Save loop", "Loop name:")
        if not ok or not name:
            return
        start, end = float(self.timeline.loop_start), float(self.timeline.loop_end)
        self.loop_list[name] = [start, end]  # overwrite if exists
        self.update_loop_dropdown()

    def on_delete_loop(self):
        cur = self.loop_select.currentData()
        if not cur:
            QMessageBox.information(self, "Info", "No loop selected to delete"); return
        name, rng = cur
        if name in self.loop_list:
            del self.loop_list[name]
        self.update_loop_dropdown()

    def on_loop_selected(self, idx):
        data = self.loop_select.currentData()
        if not data:
            return
        name, rng = data
        self.timeline.set_loop(rng[0], rng[1])

    def on_loop_toggled(self, state):
        if state:
            if self.global_players:
                start = self.timeline.loop_start
                for p in self.global_players:
                    try: p.seek(start)
                    except Exception: pass
                self.timeline.set_position(start)

    def on_loop_changed(self, s, e):
        pass

    # ---------------------------
    # Playback & Count-in (tick thread)
    # ---------------------------
    def on_play(self):
        if not self.global_players:
            return
        start = 0.0
        if self.loop_toggle.isChecked():
            start = self.timeline.loop_start
        # prepare sinks, volumes, and apply global pitch af (realtime)
        for r in self.track_rows:
            try:
                r.player.set_sink(r.sink_combo.currentData())
                pv = slider_to_mpv_volume_linear(r.current_volume_slider_value)
                r.player.set_volume(pv)
                # apply global pitch realtime (af)
                r.player.apply_pitch_af(self.global_pitch_semitones, self.global_pitch_engine)
            except Exception:
                pass
        if self.countin_cb.isChecked():
            # run ticks in background thread to not block GUI
            t = threading.Thread(target=self._run_countin_and_start, args=(start,), daemon=True)
            t.start()
        else:
            for p in self.global_players:
                try: p.play(start_pos=start, speed=self.global_playback_rate)
                except Exception: pass
            self.apply_mute_solo_logic()

    def _run_countin_and_start(self, start_pos: float):
        bpm = int(self.bpm_spin.value()) or DEFAULT_BPM
        interval = 60.0 / bpm
        ticks = int(self.tick_count_spin.value()) or 4
        # play ticks spaced by interval, then start playback
        for i in range(ticks):
            try:
                self.tick_player.play_tick()
            except Exception:
                pass
            time.sleep(interval)
        # after ticks finish, start all players
        for p in self.global_players:
            try: p.play(start_pos=start_pos, speed=self.global_playback_rate)
            except Exception: pass
        # ensure mute/solo applied
        time.sleep(0.01)
        # apply mute/solo from main thread via QTimer.singleShot
        QTimer.singleShot(0, self.apply_mute_solo_logic)

    def on_pause(self):
        for p in self.global_players:
            try: p.pause()
            except Exception: pass

    def on_stop(self):
        for p in self.global_players:
            try: p.stop()
            except Exception: pass

    def on_pitch_engine_changed(self, idx):
        eng = self.pitch_engine_combo.currentData()
        if eng:
            self.global_pitch_engine = eng
        # re-apply AF to all players
        for r in self.track_rows:
            try:
                r.player.apply_pitch_af(self.global_pitch_semitones, self.global_pitch_engine)
            except Exception:
                pass

    def on_global_pitch_changed(self, idx):
        val = int(self.global_pitch_combo.currentData())
        self.global_pitch_semitones = val
        # apply to all players realtime
        for r in self.track_rows:
            try:
                r.player.apply_pitch_af(self.global_pitch_semitones, self.global_pitch_engine)
            except Exception:
                pass

    def on_bpm_changed(self, val):
        try: self.bpm = int(val)
        except Exception: self.bpm = DEFAULT_BPM

    def on_browse_tick(self):
        f = QFileDialog.getOpenFileName(self, "Select tick sound", str(Path.home()), "Audio files (*.wav *.ogg *.mp3 *.flac)")[0]
        if not f: return
        self.tick_file = f; self.tick_label.setText(f); self.tick_player.set_tick_file(f)

    def on_countin_toggled(self, state):
        self.countin_enabled = bool(state)

    def on_tick_vol_changed(self, val):
        self.tick_volume = int(val); self.tick_player.set_tick_volume(self.tick_volume)

    def on_tick_count_changed(self, val):
        self.tick_count = int(val)

    # ---------------------------
    # UI tick
    # ---------------------------
    def ui_tick(self):
        if not self.global_players: return
        pos = self.global_players[0].get_time_pos()
        self.timeline.set_position(pos)
        if self.loop_toggle.isChecked():
            start, end = self.timeline.loop_start, self.timeline.loop_end
            if pos >= end - 0.02:
                for p in self.global_players:
                    try: p.seek(start)
                    except Exception: pass
        dur = self.global_players[0].get_duration()
        if dur and dur > 0: self.timeline.set_duration(dur)
        self.apply_mute_solo_logic()

    def on_seek_requested(self, seconds):
        for p in self.global_players:
            try: p.seek(seconds)
            except Exception: pass

    # ---------------------------
    # Close & save
    # ---------------------------
    def closeEvent(self, event):
        try:
            if self.current_folder:
                self.save_config(self.current_folder)
        except Exception:
            pass
        for r in self.track_rows:
            try: r.player.stop()
            except Exception: pass
        super().closeEvent(event)


# ---------------------------
# main entry
# ---------------------------
def main():
    if mpv is None:
        print("Warning: python-mpv not installed. UI will run but audio won't play.")
    app = QApplication(sys.argv)
    mw = MainWindow()
    mw.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
