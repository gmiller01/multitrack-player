#!/usr/bin/env python3
# Multitrack Player v4 - Global transport, per-track outputs, dark background
# Requirements:
#   sudo apt install mpv pulseaudio-utils rubberband-cli rubberband-ladspa
#   python3 -m pip install PyQt5 python-mpv
#
# Usage:
#   python3 multitrack_player_v4.py
#
import locale
locale.setlocale(locale.LC_NUMERIC, "C")

import os, sys, json, subprocess, time
from pathlib import Path
from functools import partial
from PyQt5 import QtWidgets, QtCore, QtGui

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

class TrackPlayer:
    """Small wrapper around python-mpv MPV instance for one track"""
    def __init__(self, file_path, sink_name=None):
        self.file_path = str(file_path)
        self.sink_name = sink_name
        self.player = None
        self.is_playing = False
        self.create_player()

    def create_player(self, volume=100, pitch=1.0):
        opts = {}
        try:
            self.player = mpv.MPV() if mpv else None
            if self.player:
                self.player['volume'] = volume
                if self.sink_name:
                    try:
                        self.player.set_property('audio-device', f"pulse/{self.sink_name}")
                    except Exception:
                        pass
        except Exception as e:
            print("mpv init fallback error:", e)
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

class LoopEditor(QtWidgets.QWidget):
    def __init__(self, duration):
        super().__init__()
        self.duration = duration if duration and duration > 0 else 1.0
        layout = QtWidgets.QHBoxLayout()
        self.setLayout(layout)
        self.start_spin = QtWidgets.QDoubleSpinBox(); self.start_spin.setRange(0.0, self.duration); self.start_spin.setSingleStep(0.1)
        self.end_spin = QtWidgets.QDoubleSpinBox(); self.end_spin.setRange(0.0, self.duration); self.end_spin.setSingleStep(0.1); self.end_spin.setValue(self.duration)
        layout.addWidget(QtWidgets.QLabel("Start (s)")); layout.addWidget(self.start_spin)
        layout.addWidget(QtWidgets.QLabel("End (s)")); layout.addWidget(self.end_spin)
        self.slider = QtWidgets.QSlider(QtCore.Qt.Horizontal); self.slider.setRange(0,1000); self.slider.setSingleStep(1)
        layout.addWidget(self.slider, 2)
        self.start_spin.valueChanged.connect(self.on_spin_changed); self.end_spin.valueChanged.connect(self.on_spin_changed); self.slider.sliderMoved.connect(self.on_slider_moved)

    def on_spin_changed(self, _=None):
        start = self.start_spin.value(); end = self.end_spin.value()
        if end < start:
            end = start; self.end_spin.setValue(end)
        mid = int(((start + end) / 2.0) / self.duration * 1000)
        self.slider.blockSignals(True); self.slider.setValue(mid); self.slider.blockSignals(False)

    def on_slider_moved(self, val):
        mid = val / 1000.0 * self.duration
        width = (self.end_spin.value() - self.start_spin.value())
        new_start = max(0.0, mid - width / 2.0)
        new_end = min(self.duration, new_start + width)
        self.start_spin.blockSignals(True); self.end_spin.blockSignals(True)
        self.start_spin.setValue(new_start); self.end_spin.setValue(new_end)
        self.start_spin.blockSignals(False); self.end_spin.blockSignals(False)

    def get_loop(self):
        return float(self.start_spin.value()), float(self.end_spin.value())

    def set_duration(self, duration):
        self.duration = max(1.0, duration)
        self.start_spin.setRange(0.0, self.duration)
        self.end_spin.setRange(0.0, self.duration)
        self.end_spin.setValue(self.duration)

    def set_loop(self, start, end):
        self.start_spin.setValue(start); self.end_spin.setValue(end)

class TrackRow(QtWidgets.QWidget):
    def __init__(self, filename, sinks, assigned_sink=None):
        super().__init__()
        self.filename = filename
        self.sinks = sinks
        self.assigned_sink = assigned_sink
        self.player = TrackPlayer(filename, sink_name=assigned_sink)
        self.init_ui()

    def init_ui(self):
        layout = QtWidgets.QHBoxLayout(); self.setLayout(layout)
        self.name_label = QtWidgets.QLabel(Path(self.filename).name); layout.addWidget(self.name_label,3)
        self.sink_combo = QtWidgets.QComboBox(); 
        for s in self.sinks: self.sink_combo.addItem(s[1], s[1])
        if self.assigned_sink:
            idx = self.sink_combo.findData(self.assigned_sink)
            if idx != -1: self.sink_combo.setCurrentIndex(idx)
        layout.addWidget(self.sink_combo,2)
        self.mute_cb = QtWidgets.QCheckBox("Mute"); layout.addWidget(self.mute_cb)
        self.sink_combo.currentIndexChanged.connect(self.on_sink_changed)

    def on_sink_changed(self, idx):
        sink = self.sink_combo.currentData()
        self.player.set_sink(sink)

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Multitrack Player v4 - Global Transport")
        self.resize(1000, 720)
        # dark gray background
        self.setStyleSheet("QWidget { background-color: #2f2f2f; color: #e6e6e6; } QPushButton { background-color: #444444; } QLineEdit, QComboBox { background-color: #3a3a3a; }")
        self.sinks = list_pactl_sinks()
        self.settings = {}
        self.current_folder = None
        self.track_rows = []
        self.global_players = []
        self.global_pitch = 1.0
        self.loop_list = []
        self.current_loop = None

        w = QtWidgets.QWidget(); self.setCentralWidget(w); v = QtWidgets.QVBoxLayout(); w.setLayout(v)
        top = QtWidgets.QHBoxLayout(); v.addLayout(top)
        self.open_btn = QtWidgets.QPushButton("Open Folder"); top.addWidget(self.open_btn)
        self.folder_label = QtWidgets.QLabel("No folder selected"); top.addWidget(self.folder_label,1)
        self.refresh_btn = QtWidgets.QPushButton("Refresh Outputs"); top.addWidget(self.refresh_btn)
        self.save_btn = QtWidgets.QPushButton("Save Settings"); top.addWidget(self.save_btn)
        transport = QtWidgets.QHBoxLayout(); v.addLayout(transport)
        self.play_btn = QtWidgets.QPushButton("Play"); transport.addWidget(self.play_btn)
        self.pause_btn = QtWidgets.QPushButton("Pause"); transport.addWidget(self.pause_btn)
        self.stop_btn = QtWidgets.QPushButton("Stop"); transport.addWidget(self.stop_btn)
        transport.addWidget(QtWidgets.QLabel("Pitch")); self.pitch_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal); self.pitch_slider.setRange(50,200); self.pitch_slider.setValue(100); transport.addWidget(self.pitch_slider,2); self.pitch_label = QtWidgets.QLabel("1.00x"); transport.addWidget(self.pitch_label)
        loop_h = QtWidgets.QHBoxLayout(); v.addLayout(loop_h)
        loop_h.addWidget(QtWidgets.QLabel("Loop:"))
        self.loop_editor = LoopEditor(duration=10.0); loop_h.addWidget(self.loop_editor,3)
        self.loop_toggle = QtWidgets.QCheckBox("Loop On"); loop_h.addWidget(self.loop_toggle)
        self.loop_save_btn = QtWidgets.QPushButton("Save Loop"); loop_h.addWidget(self.loop_save_btn)
        self.loop_select = QtWidgets.QComboBox(); loop_h.addWidget(self.loop_select,1)
        self.tracks_area = QtWidgets.QScrollArea(); self.tracks_widget = QtWidgets.QWidget(); self.tracks_layout = QtWidgets.QVBoxLayout(); self.tracks_widget.setLayout(self.tracks_layout); self.tracks_area.setWidgetResizable(True); self.tracks_area.setWidget(self.tracks_widget); v.addWidget(self.tracks_area)
        self.open_btn.clicked.connect(self.on_open); self.refresh_btn.clicked.connect(self.on_refresh); self.save_btn.clicked.connect(self.on_save)
        self.play_btn.clicked.connect(self.on_play); self.pause_btn.clicked.connect(self.on_pause); self.stop_btn.clicked.connect(self.on_stop)
        self.pitch_slider.valueChanged.connect(self.on_pitch_changed); self.loop_save_btn.clicked.connect(self.on_save_loop); self.loop_select.currentIndexChanged.connect(self.on_loop_selected)
        self.ui_timer = QtCore.QTimer(); self.ui_timer.setInterval(100); self.ui_timer.timeout.connect(self.ui_tick); self.ui_timer.start()

    def on_open(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Select folder with audio files")
        if not folder:
            return
        self.current_folder = Path(folder); self.folder_label.setText(folder)
        settings_path = self.current_folder / SETTINGS_NAME
        if settings_path.exists():
            try:
                with open(settings_path, 'r') as f: self.settings = json.load(f)
            except Exception:
                self.settings = {}
        else:
            self.settings = {}
        self.load_tracks(folder)

    def load_tracks(self, folder):
        for r in self.track_rows:
            try:
                r.player.stop()
                r.deleteLater()
            except Exception:
                pass
        self.track_rows = []; self.global_players = []
        self.sinks = list_pactl_sinks()
        files = [p for p in sorted(Path(folder).iterdir()) if p.suffix.lower() in AUDIO_EXTS]
        for p in files:
            assigned = None
            s = self.settings.get(p.name, {})
            if s:
                assigned = s.get('sink')
            row = TrackRow(str(p), self.sinks, assigned_sink=assigned)
            self.tracks_layout.addWidget(row); self.track_rows.append(row); self.global_players.append(row.player)
        self.tracks_layout.addStretch(1)
        global_settings = self.settings.get('_global', {})
        if global_settings:
            self.global_pitch = global_settings.get('pitch', 1.0); self.pitch_slider.setValue(int(self.global_pitch * 100))
            loops = global_settings.get('loops', [])
            if loops:
                self.loop_list = loops; self.update_loop_dropdown()
        if self.global_players:
            dur = self.global_players[0].get_duration()
            if dur and dur > 0: self.loop_editor.set_duration(dur)

    def on_refresh(self):
        self.sinks = list_pactl_sinks()
        for row in self.track_rows:
            cur = row.sink_combo.currentData(); row.sink_combo.clear()
            for s in self.sinks: row.sink_combo.addItem(s[1], s[1])
            if cur:
                idx = row.sink_combo.findData(cur)
                if idx != -1: row.sink_combo.setCurrentIndex(idx)

    def on_save(self):
        if not self.current_folder:
            QtWidgets.QMessageBox.information(self, "Info", "No folder selected"); return
        data = {}
        for row in self.track_rows:
            data[row.name_label.text()] = {'sink': row.sink_combo.currentData()}
        data['_global'] = {'pitch': self.global_pitch, 'loops': self.loop_list}
        settings_path = self.current_folder / SETTINGS_NAME
        try:
            with open(settings_path, 'w') as f: json.dump(data, f, indent=2)
            QtWidgets.QMessageBox.information(self, "Saved", f"Settings saved to {settings_path}")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Error", f"Failed to save settings: {e}")

    def on_play(self):
        start = 0.0
        if self.loop_toggle.isChecked():
            start, end = self.loop_editor.get_loop()
        speed = self.global_pitch
        for r in self.track_rows:
            try:
                r.player.set_sink(r.sink_combo.currentData())
            except Exception:
                pass
        # start players quickly
        for p in self.global_players:
            try:
                p.play(start_pos=start, speed=speed)
            except Exception:
                pass

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
        name, ok = QtWidgets.QInputDialog.getText(self, "Save loop", "Loop name:")
        if not ok or not name: return
        start, end = self.loop_editor.get_loop(); entry = {'name': name, 'start': start, 'end': end}; self.loop_list.append(entry); self.update_loop_dropdown()

    def update_loop_dropdown(self):
        self.loop_select.blockSignals(True); self.loop_select.clear(); self.loop_select.addItem("Select loop...", None)
        for l in self.loop_list: self.loop_select.addItem(l['name'], l)
        self.loop_select.blockSignals(False)

    def on_loop_selected(self, idx):
        data = self.loop_select.currentData(); 
        if not data: return
        self.loop_editor.set_loop(data['start'], data['end'])

    def ui_tick(self):
        if not self.global_players: return
        if self.loop_toggle.isChecked():
            start, end = self.loop_editor.get_loop()
            pos = self.global_players[0].get_time_pos()
            if pos >= end - 0.02:
                for p in self.global_players:
                    try: p.seek(start)
                    except Exception: pass
        if self.global_players:
            dur = self.global_players[0].get_duration()
            if dur and dur > 0: self.loop_editor.set_duration(dur)

    def closeEvent(self, event):
        for r in self.track_rows:
            try: r.player.stop()
            except Exception: pass
        super().closeEvent(event)

def main():
    if mpv is None:
        print("Error: python-mpv not installed. Install with: python3 -m pip install python-mpv"); return
    app = QtWidgets.QApplication(sys.argv); mw = MainWindow(); mw.show(); sys.exit(app.exec_())

if __name__ == '__main__':
    main()
