#!/usr/bin/env python3
"""
Multitrack Player v3 prototype
- PyQt5 GUI
- python-mpv (libmpv) backend
- rubberband-based pitch shifting (tempo preserved) when available
- per-track output selection (Pulse/PipeWire sinks via pactl)
- per-track loop (start/end) with slider and save/load named loops
- settings saved to multitrack_settings.json in the selected folder
"""

import os, sys, json, subprocess, time
from pathlib import Path
from functools import partial
from PyQt5 import QtWidgets, QtCore

# try import python-mpv
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
    def __init__(self, file_path, sink_name=None, volume=100, pitch=1.0):
        self.file_path = str(file_path)
        self.sink_name = sink_name
        self.volume = volume
        self.pitch = pitch
        self.player = None
        self.is_playing = False
        self.create_player()

    def create_player(self):
        opts = {'no_video': True, 'volume': self.volume, 'keep-open': True, 'msg-level': 'all=no'}
        if self.sink_name:
            opts['audio_device'] = f"pulse/{self.sink_name}"
        try:
            self.player = mpv.MPV(**opts) if mpv else None
        except Exception:
            self.player = None
        if self.player and abs(self.pitch - 1.0) > 0.0001:
            self.apply_pitch_filter(self.pitch)

    def apply_pitch_filter(self, pitch):
        if not self.player:
            return False
        af_str = f"rubberband=pitch-scale={pitch}"
        try:
            # try to add audio filter via command
            try:
                self.player.command('af', 'add', af_str)
                return True
            except Exception:
                pass
            # fallback: set_property (may not work on some builds)
            try:
                self.player.set_property('af', af_str)
                return True
            except Exception:
                return False
        except Exception:
            return False

    def set_sink(self, sink_name):
        self.sink_name = sink_name
        if not self.player:
            return
        try:
            self.player.set_property('audio-device', f"pulse/{sink_name}")
        except Exception:
            pass

    def set_volume(self, vol):
        self.volume = int(vol)
        if self.player:
            try:
                self.player.volume = self.volume
            except Exception:
                pass

    def set_pitch(self, pitch):
        self.pitch = float(pitch)
        ok = False
        if self.player:
            ok = self.apply_pitch_filter(self.pitch)
        return ok

    def play(self, start_pos=None):
        if not self.player:
            self.create_player()
        try:
            if start_pos is not None:
                self.player.play(self.file_path)
                time.sleep(0.02)
                self.player.seek(start_pos, reference='absolute')
            else:
                self.player.play(self.file_path)
            self.player.volume = self.volume
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

    def set_loop(self, start, end):
        self.start_spin.setRange(0.0, self.duration); self.end_spin.setRange(0.0, self.duration)
        self.start_spin.setValue(start); self.end_spin.setValue(end)


class TrackWidget(QtWidgets.QWidget):
    def __init__(self, filepath, sinks, settings):
        super().__init__()
        self.filepath = Path(filepath)
        self.sinks = sinks
        self.settings = settings or {}
        self.player = None
        self.duration = 0.0
        self.loop_list = []
        self.init_ui(); self.load_settings(); self.create_player()

    def init_ui(self):
        layout = QtWidgets.QVBoxLayout(); self.setLayout(layout)
        top = QtWidgets.QHBoxLayout(); layout.addLayout(top)
        self.label = QtWidgets.QLabel(self.filepath.name); top.addWidget(self.label,2)
        self.sink_combo = QtWidgets.QComboBox(); 
        for s in self.sinks: self.sink_combo.addItem(s[1], s[1])
        top.addWidget(self.sink_combo,1)
        self.play_btn = QtWidgets.QPushButton("Play"); self.stop_btn = QtWidgets.QPushButton("Stop"); top.addWidget(self.play_btn); top.addWidget(self.stop_btn)
        vol_layout = QtWidgets.QHBoxLayout(); vol_layout.addWidget(QtWidgets.QLabel("Vol")); self.vol_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal); self.vol_slider.setRange(0,200); self.vol_slider.setValue(100); vol_layout.addWidget(self.vol_slider,1); self.vol_label = QtWidgets.QLabel("100%"); vol_layout.addWidget(self.vol_label); layout.addLayout(vol_layout)
        pitch_layout = QtWidgets.QHBoxLayout(); pitch_layout.addWidget(QtWidgets.QLabel("Pitch")); self.pitch_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal); self.pitch_slider.setRange(50,200); self.pitch_slider.setValue(100); pitch_layout.addWidget(self.pitch_slider,1); self.pitch_label = QtWidgets.QLabel("1.00x"); pitch_layout.addWidget(self.pitch_label); layout.addLayout(pitch_layout)
        loop_top = QtWidgets.QHBoxLayout(); self.loop_checkbox = QtWidgets.QCheckBox("Loop"); loop_top.addWidget(self.loop_checkbox); self.loop_editor = LoopEditor(duration=1.0); loop_top.addWidget(self.loop_editor,3); self.loop_save_btn = QtWidgets.QPushButton("Save Loop"); loop_top.addWidget(self.loop_save_btn); self.loop_select = QtWidgets.QComboBox(); loop_top.addWidget(self.loop_select,1); layout.addLayout(loop_top)
        self.play_btn.clicked.connect(self.on_play); self.stop_btn.clicked.connect(self.on_stop); self.vol_slider.valueChanged.connect(self.on_volume_changed); self.pitch_slider.valueChanged.connect(self.on_pitch_changed); self.loop_save_btn.clicked.connect(self.on_save_loop); self.loop_select.currentIndexChanged.connect(self.on_loop_selected)
        self.loop_timer = QtCore.QTimer(); self.loop_timer.setInterval(100); self.loop_timer.timeout.connect(self.check_loop); self.loop_timer.start()

    def create_player(self):
        init_sink = None
        if self.sink_combo.count() > 0:
            init_sink = self.sink_combo.currentData()
        self.player = TrackPlayer(self.filepath, sink_name=init_sink, volume=self.vol_slider.value(), pitch=1.0)
        try:
            dur = self.player.get_duration()
            self.duration = dur if dur and dur > 0 else self.duration
        except Exception:
            pass
        self.loop_editor.duration = max(1.0, self.duration)
        self.loop_editor.set_loop(0.0, self.loop_editor.duration)

    def load_settings(self):
        s = self.settings.get(self.filepath.name, {})
        if s:
            sink_name = s.get('sink')
            if sink_name:
                idx = self.sink_combo.findData(sink_name)
                if idx != -1:
                    self.sink_combo.setCurrentIndex(idx)
            vol = s.get('volume', 100); self.vol_slider.setValue(int(vol))
            pitch = s.get('pitch', 1.0)
            try: self.pitch_slider.setValue(int(float(pitch) * 100))
            except Exception: pass
            loops = s.get('loops', [])
            if loops:
                self.loop_list = loops; self.update_loop_dropdown()

    def save_settings(self, settings_dict):
        settings_dict[self.filepath.name] = {'sink': self.sink_combo.currentData(), 'volume': self.vol_slider.value(), 'pitch': self.pitch_slider.value() / 100.0, 'loops': self.loop_list}

    def on_play(self):
        sink = self.sink_combo.currentData(); self.player.set_sink(sink); self.player.set_volume(self.vol_slider.value())
        pitch = self.pitch_slider.value() / 100.0
        ok = self.player.set_pitch(pitch)
        if not ok:
            try:
                self.player.player.speed = pitch
            except Exception:
                pass
        if self.loop_checkbox.isChecked():
            start, end = self.loop_editor.get_loop(); self.player.play(start_pos=start)
        else:
            self.player.play()
        self.player.is_playing = True

    def on_stop(self):
        self.player.stop(); self.player.is_playing = False

    def on_volume_changed(self, val):
        self.vol_label.setText(f"{val}%"); self.player.set_volume(val)

    def on_pitch_changed(self, val):
        speed = val / 100.0; self.pitch_label.setText(f"{speed:.2f}x"); ok = self.player.set_pitch(speed)
        if not ok:
            try:
                if self.player.player:
                    self.player.player.speed = speed
            except Exception:
                pass

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

    def check_loop(self):
        if not self.player or not self.player.is_playing: return
        if not self.loop_checkbox.isChecked(): return
        pos = self.player.get_time_pos(); start, end = self.loop_editor.get_loop()
        if pos >= end - 0.02:
            try: self.player.seek(start)
            except Exception: pass


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Multitrack Player v3 (Prototype)")
        self.resize(1000, 700)
        self.sinks = list_pactl_sinks(); self.settings = {}; self.current_folder = None; self.track_widgets = []
        w = QtWidgets.QWidget(); self.setCentralWidget(w); v = QtWidgets.QVBoxLayout(); w.setLayout(v)
        top = QtWidgets.QHBoxLayout(); v.addLayout(top)
        self.open_btn = QtWidgets.QPushButton("Open Folder"); top.addWidget(self.open_btn)
        self.folder_label = QtWidgets.QLabel("No folder selected"); top.addWidget(self.folder_label,1)
        self.refresh_btn = QtWidgets.QPushButton("Refresh Outputs"); top.addWidget(self.refresh_btn)
        self.save_btn = QtWidgets.QPushButton("Save Settings"); top.addWidget(self.save_btn)
        self.tracks_area = QtWidgets.QScrollArea(); self.tracks_widget = QtWidgets.QWidget(); self.tracks_layout = QtWidgets.QVBoxLayout(); self.tracks_widget.setLayout(self.tracks_layout); self.tracks_area.setWidgetResizable(True); self.tracks_area.setWidget(self.tracks_widget); v.addWidget(self.tracks_area)
        self.open_btn.clicked.connect(self.on_open); self.refresh_btn.clicked.connect(self.on_refresh); self.save_btn.clicked.connect(self.on_save)

    def on_open(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Select folder with audio files")
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
        for tw in self.track_widgets:
            try: tw.on_stop(); tw.deleteLater()
            except Exception: pass
        self.track_widgets = []
        self.sinks = list_pactl_sinks()
        for p in sorted(Path(folder).iterdir()):
            if p.suffix.lower() in AUDIO_EXTS:
                tw = TrackWidget(str(p), self.sinks, self.settings); self.tracks_layout.addWidget(tw); self.track_widgets.append(tw)
        self.tracks_layout.addStretch(1)

    def on_refresh(self):
        self.sinks = list_pactl_sinks()
        for tw in self.track_widgets:
            cur = tw.sink_combo.currentData(); tw.sink_combo.clear()
            for s in self.sinks: tw.sink_combo.addItem(s[1], s[1])
            if cur:
                idx = tw.sink_combo.findData(cur)
                if idx != -1: tw.sink_combo.setCurrentIndex(idx)

    def on_save(self):
        if not self.current_folder:
            QtWidgets.QMessageBox.information(self, "Info", "No folder selected"); return
        data = {}
        for tw in self.track_widgets: tw.save_settings(data)
        settings_path = self.current_folder / SETTINGS_NAME
        try:
            with open(settings_path, 'w') as f: json.dump(data, f, indent=2)
            QtWidgets.QMessageBox.information(self, "Saved", f"Settings saved to {settings_path}")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Error", f"Failed to save settings: {e}")

    def closeEvent(self, event):
        for tw in self.track_widgets:
            try: tw.on_stop()
            except Exception: pass
        super().closeEvent(event)


def main():
    if mpv is None:
        print("Error: python-mpv not installed. Install with: python3 -m pip install python-mpv")
        return
    app = QtWidgets.QApplication(sys.argv)
    mw = MainWindow(); mw.show(); sys.exit(app.exec_())

if __name__ == '__main__':
    main()
