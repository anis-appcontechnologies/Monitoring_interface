#!/usr/bin/env python3
"""
Load Parameters — PySide6 edition
Reads a parameter file and restores all parameters to firmware,
then re-triggers current and speed PI setup commands.
Logic preserved 1:1 from load_params.py (Tkinter original).
"""

import logging
import threading
import time

from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QFrame, QFileDialog, QApplication,
)

try:
    import qtawesome as qta
    _QTA = True
except ImportError:
    _QTA = False

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(message)s")


def _get_palette():
    try:
        import amc_interface_qt as _amcqt
        return _amcqt.C
    except Exception:
        return {
            "white": "#FFFFFF", "bg": "#F0F0F0", "border": "#D8D8D8",
            "text": "#1A1A1A", "text2": "#3A3A3A", "muted": "#707070",
            "red": "#C0272D", "red_bg": "#F9ECEC", "blue": "#1976D2",
            "input_bg": "#F7F7F7", "faint": "#B0B0B0",
        }


from protocol import dec_encode

PARAM_DEFS = [
    ("mpole",  "s mpole",  "Pole pairs",   "—",       1.0),
    ("mrs",    "s mrs",    "Rs",           "mΩ",   1000.0),
    ("mlsd",   "s mlsd",   "Lsd",          "µH",     1e6),
    ("mlsq",   "s mlsq",   "Lsq",          "µH",     1e6),
    ("mpsif",  "s mpsif",  "PsiF",         "mWb",  1000.0),
    ("miqmx",  "s miqmx",  "IsqMax",       "A",       1.0),
    ("msmax",  "s msmax",  "SpeedMax",     "RPM",     1.0),
    ("msmnl",  "s msmnl",  "NoLoadSpeed",  "RPM",     1.0),
    ("jres",   "s mtheta", "Inertia J",    "kg·m²",   1.0),
    ("fric",   None,       "Friction",     "Nm·s/r",  1.0),
    ("damp",   "s damp",   "Damping Dg",   "—",       1.0),
    ("dyn",    "s dyn",    "Dynamic Dy",   "rad/s",   1.0),
]

REQUIRED = {"mpole", "mrs", "mlsd", "mlsq", "mpsif", "miqmx", "msmax", "msmnl"}


class LoadParameters(QDialog):
    _sig_done   = Signal(dict)
    _sig_error  = Signal(str)
    _sig_status = Signal(str)

    def __init__(self, parent, serial_manager):
        super().__init__(parent)
        self.serial_manager = serial_manager
        self.setWindowTitle("Load Parameters")
        self.resize(520, 480)
        self.setMinimumSize(460, 400)

        self._sig_done.connect(self._on_done)
        self._sig_error.connect(self._on_error)
        self._sig_status.connect(self._on_status)

        self._build_ui()
        self._apply_style()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        title = QLabel("Load Parameters")
        title.setObjectName("lp_title")
        sub = QLabel("Read a parameter file and restore all parameters to firmware")
        sub.setObjectName("lp_sub")
        root.addWidget(title)
        root.addWidget(sub)

        path_card = QFrame()
        path_card.setObjectName("lp_card")
        path_lay = QHBoxLayout(path_card)
        path_lay.setContentsMargins(12, 10, 12, 10)
        path_lay.setSpacing(8)

        path_lbl = QLabel("File Path")
        path_lbl.setObjectName("lp_field")
        path_lbl.setFixedWidth(64)
        path_lay.addWidget(path_lbl)

        self.path_entry = QLineEdit()
        self.path_entry.setPlaceholderText("Select a saved .txt parameter file…")
        path_lay.addWidget(self.path_entry, 1)

        browse_btn = QPushButton("Browse")
        browse_btn.setObjectName("lp_btn_outline")
        browse_btn.setFixedWidth(80)
        browse_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        browse_btn.clicked.connect(self._browse)
        path_lay.addWidget(browse_btn)

        root.addWidget(path_card)

        self.load_btn = QPushButton("Load Parameters")
        self.load_btn.setObjectName("lp_btn_primary")
        self.load_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.load_btn.clicked.connect(self._on_load)
        if _QTA:
            self.load_btn.setIcon(qta.icon("fa5s.folder-open", color="#FFFFFF"))
            self.load_btn.setIconSize(QSize(13, 13))
        root.addWidget(self.load_btn)

        self.status_lbl = QLabel("")
        self.status_lbl.setObjectName("lp_status_ok")
        root.addWidget(self.status_lbl)

        res_card = QFrame()
        res_card.setObjectName("lp_card")
        res_outer = QVBoxLayout(res_card)
        res_outer.setContentsMargins(12, 10, 12, 10)
        res_outer.setSpacing(6)

        res_hdr = QLabel("Parameters sent to firmware")
        res_hdr.setObjectName("lp_field")
        res_outer.addWidget(res_hdr)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setObjectName("lp_sep")
        res_outer.addWidget(sep)

        grid_widget = QFrame()
        grid_widget.setObjectName("lp_grid")
        from PySide6.QtWidgets import QGridLayout
        grid = QGridLayout(grid_widget)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(4)
        grid.setContentsMargins(0, 0, 0, 0)

        self._result_labels = {}
        for idx, (key, _, disp, unit, _scale) in enumerate(PARAM_DEFS):
            row = idx // 2
            col = (idx % 2) * 2
            lbl = QLabel(f"{disp}:  —")
            lbl.setObjectName("lp_result")
            grid.addWidget(lbl, row, col)
            self._result_labels[key] = lbl

        res_outer.addWidget(grid_widget)
        root.addWidget(res_card, 1)

    def _apply_style(self):
        p = _get_palette()
        self.setStyleSheet(f"""
            QDialog {{ background: {p['bg']}; }}
            QLabel  {{ background: transparent; color: {p['text']}; }}
            QLabel#lp_title  {{ font-size: 15px; font-weight: 700; color: {p['text']}; }}
            QLabel#lp_sub    {{ font-size: 11px; color: {p['muted']}; }}
            QLabel#lp_field  {{ font-size: 11px; font-weight: 600; color: {p['text2']}; }}
            QLabel#lp_result {{ font-size: 11px; color: {p['text2']}; font-family: Consolas, monospace; }}
            QLabel#lp_status_ok  {{ font-size: 11px; font-weight: 600; color: {p['blue']}; }}
            QLabel#lp_status_err {{ font-size: 11px; font-weight: 600; color: {p['red']}; }}
            QFrame#lp_card   {{ background: {p['white']}; border: 1px solid {p['border']}; border-radius: 8px; }}
            QFrame#lp_sep    {{ background: {p['border']}; max-height: 1px; border: none; }}
            QFrame#lp_grid   {{ background: transparent; }}
            QLineEdit {{
                background: {p['white']}; color: {p['text']};
                border: 1px solid {p['border']}; border-radius: 5px;
                padding: 4px 8px; font-size: 11px; min-height: 22px;
            }}
            QLineEdit:focus {{ border-color: {p['red']}; }}
            QPushButton#lp_btn_primary {{
                background: {p['red']}; color: white; border: none;
                border-radius: 5px; padding: 7px 16px;
                font-size: 12px; font-weight: 700;
                border-bottom: 2px solid {p.get('red_dark', '#9B1F24')};
            }}
            QPushButton#lp_btn_primary:hover    {{ background: {p.get('red_dark', '#9B1F24')}; }}
            QPushButton#lp_btn_primary:disabled {{ background: {p['faint']}; color: #EEEEEE; border-bottom: none; }}
            QPushButton#lp_btn_outline {{
                background: {p['white']}; color: {p['text2']};
                border: 1.5px solid {p['border']}; border-radius: 5px;
                font-size: 11px; padding: 4px 8px;
            }}
            QPushButton#lp_btn_outline:hover {{ border-color: {p['red']}; color: {p['red']}; }}
            QToolTip {{
                background: #1E293B; color: #F1F5F9;
                border: 1px solid #334155; border-radius: 4px;
                padding: 4px 8px; font-size: 11px;
            }}
        """)

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Parameter File", "",
            "Text Files (*.txt);;All Files (*)")
        if path:
            self.path_entry.setText(path)

    def _on_load(self):
        if not self.serial_manager.is_open:
            self._on_error("Not connected to serial port.")
            return
        file_path = self.path_entry.text().strip()
        if not file_path:
            self._on_error("Please select or enter a file path.")
            return

        self.load_btn.setEnabled(False)
        self.load_btn.setText("Loading…")
        self._on_status("Reading parameter file…")

        def worker():
            try:
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                except UnicodeDecodeError:
                    with open(file_path, 'r', encoding='cp1252') as f:
                        content = f.read()

                params = {}
                for line in content.splitlines():
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    if '=' not in line:
                        continue
                    key, _, val_str = line.partition('=')
                    key = key.strip().lower()
                    try:
                        params[key] = float(val_str.strip())
                    except ValueError:
                        pass

                missing = REQUIRED - set(params.keys())
                if missing:
                    raise ValueError(f"Missing required parameter(s): {', '.join(sorted(missing))}")

                self._sig_status.emit("Sending motor parameters…")
                for key, cmd, _disp, _unit, _scale in PARAM_DEFS:
                    if cmd is None or key not in params:
                        continue
                    val = params[key]
                    encoded = dec_encode(val)
                    self.serial_manager.send(f"{cmd} {encoded}", expect_response=True)
                    logging.debug("Sent: %s %s  (%s=%s)", cmd, encoded, key, val)

                self._sig_status.emit("Re-tuning current controller…")
                self.serial_manager.send("s suidq", expect_response=True)
                time.sleep(0.1)

                if "jres" in params:
                    self._sig_status.emit("Writing inertia J…")
                    j_val = params["jres"]
                    encoded_j = dec_encode(j_val)
                    self.serial_manager.send(f"s mtheta {encoded_j}", expect_response=True)
                    logging.debug("Sent: s mtheta %s  (jres=%s)", encoded_j, j_val)
                    time.sleep(0.05)

                if "damp" in params and "dyn" in params:
                    self._sig_status.emit("Re-applying speed controller…")
                    self.serial_manager.send("s susp", expect_response=True)
                    time.sleep(0.1)
                    self.serial_manager.send("s apspd", expect_response=True)
                    time.sleep(0.1)

                logging.info("Parameters loaded from %s", file_path)
                self._sig_done.emit(params)

            except FileNotFoundError:
                self._sig_error.emit("File not found.")
            except Exception as e:
                logging.exception("Load failed")
                self._sig_error.emit(f"Load failed: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def _on_done(self, params: dict):
        for key, _cmd, disp, unit, scale in PARAM_DEFS:
            lbl = self._result_labels.get(key)
            if not lbl or key not in params:
                continue
            val = params[key]
            display_val = val * scale
            if key == "mpole":
                text = f"{disp}:  {int(round(val))}"
            elif abs(display_val) < 0.001 and display_val != 0.0:
                text = f"{disp}:  {display_val:.4e} {unit}"
            else:
                text = f"{disp}:  {display_val:.4g} {unit}"
            lbl.setText(text)
        self.load_btn.setEnabled(True)
        self.load_btn.setText("Load Parameters")
        self._on_status("Loaded successfully.")

    def _on_error(self, msg: str):
        self.load_btn.setEnabled(True)
        self.load_btn.setText("Load Parameters")
        self.status_lbl.setObjectName("lp_status_err")
        self.status_lbl.style().unpolish(self.status_lbl)
        self.status_lbl.style().polish(self.status_lbl)
        self.status_lbl.setText(msg)

    def _on_status(self, msg: str):
        self.status_lbl.setObjectName("lp_status_ok")
        self.status_lbl.style().unpolish(self.status_lbl)
        self.status_lbl.style().polish(self.status_lbl)
        self.status_lbl.setText(msg)


if __name__ == "__main__":
    import sys

    class DummySerial:
        is_open = True
        def send(self, cmd, expect_response=False):
            return "+00042"

    app = QApplication(sys.argv)
    app.setFont(QFont("Segoe UI", 10))
    dlg = LoadParameters(None, DummySerial())
    dlg.show()
    sys.exit(app.exec())
