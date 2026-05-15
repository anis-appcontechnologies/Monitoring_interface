#!/usr/bin/env python3
"""
Save Parameters — PySide6 edition
Reads all drive parameters from firmware and saves them to a file.
Logic preserved 1:1 from save_params.py (Tkinter original).
"""

import logging
import threading

from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QFrame, QSizePolicy, QFileDialog, QApplication, QCheckBox,
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


def _px(n: int) -> int:
    try:
        from PySide6.QtWidgets import QApplication
        s = QApplication.primaryScreen()
        if s is not None:
            return max(1, round(n * s.logicalDotsPerInch() / 96.0))
    except Exception:
        pass
    return n

from protocol import dec_decode

PARAM_DEFS = [
    ("mpole",  "g mpole",  "Pole pairs",   "—",       1.0),
    ("mrs",    "g mrs",    "Rs",           "mΩ",   1000.0),
    ("mlsd",   "g mlsd",   "Lsd",          "µH",     1e6),
    ("mlsq",   "g mlsq",   "Lsq",          "µH",     1e6),
    ("mpsif",  "g mpsif",  "PsiF",         "mWb",  1000.0),
    ("miqmx",  "g miqmx",  "IsqMax",       "A",       1.0),
    ("msmax",  "g msmax",  "SpeedMax",     "RPM",     1.0),
    ("msmnl",  "g msmnl",  "NoLoadSpeed",  "RPM",     1.0),
    ("jres",   "g jres",   "Inertia J",    "kg·m²",   1.0),
    ("fric",   "g fric",   "Friction",     "Nm·s/r",  1.0),
    ("damp",   "g damp",   "Damping Dg",   "—",       1.0),
    ("dyn",    "g dyn",    "Dynamic Dy",   "rad/s",   1.0),
]


class SaveParameters(QDialog):
    _sig_done    = Signal(dict)   # results dict
    _sig_error   = Signal(str)
    _sig_status  = Signal(str)

    def __init__(self, parent, serial_manager):
        super().__init__(parent)
        self.serial_manager = serial_manager
        self.setWindowTitle("Save Parameters")
        _scr = QApplication.primaryScreen().availableGeometry()
        self.resize(min(_px(520), int(_scr.width() * 0.6)), min(_px(480), int(_scr.height() * 0.7)))
        self.setMinimumSize(_px(460), _px(400))

        self._sig_done.connect(self._on_done)
        self._sig_error.connect(self._on_error)
        self._sig_status.connect(self._on_status)

        self._build_ui()
        self._apply_style()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        # Title
        title = QLabel("Save Parameters")
        title.setObjectName("sp_title")
        sub = QLabel("Read all drive parameters from firmware and save to a file")
        sub.setObjectName("sp_sub")
        root.addWidget(title)
        root.addWidget(sub)

        # File path card
        path_card = QFrame()
        path_card.setObjectName("sp_card")
        path_lay = QHBoxLayout(path_card)
        path_lay.setContentsMargins(12, 10, 12, 10)
        path_lay.setSpacing(8)

        path_lbl = QLabel("File Path")
        path_lbl.setObjectName("sp_field")
        path_lbl.setFixedWidth(_px(64))
        path_lay.addWidget(path_lbl)

        self.path_entry = QLineEdit()
        self.path_entry.setPlaceholderText("Select or type a .txt file path…")
        path_lay.addWidget(self.path_entry, 1)

        browse_btn = QPushButton("Browse")
        browse_btn.setObjectName("sp_btn_outline")
        browse_btn.setFixedWidth(_px(80))
        browse_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        browse_btn.clicked.connect(self._browse)
        path_lay.addWidget(browse_btn)

        root.addWidget(path_card)

        # Save button
        self.save_btn = QPushButton("Save Parameters")
        self.save_btn.setObjectName("sp_btn_primary")
        self.save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.save_btn.clicked.connect(self._on_save)
        if _QTA:
            self.save_btn.setIcon(qta.icon("fa5s.save", color="#FFFFFF"))
            self.save_btn.setIconSize(QSize(13, 13))
        root.addWidget(self.save_btn)

        self._chk_save_to_ctrl = QCheckBox("Also save to controller memory (s smpar)")
        self._chk_save_to_ctrl.setChecked(True)
        root.addWidget(self._chk_save_to_ctrl)

        # Status
        self.status_lbl = QLabel("")
        self.status_lbl.setObjectName("sp_status_ok")
        root.addWidget(self.status_lbl)

        # Results card
        res_card = QFrame()
        res_card.setObjectName("sp_card")
        res_outer = QVBoxLayout(res_card)
        res_outer.setContentsMargins(12, 10, 12, 10)
        res_outer.setSpacing(6)

        res_hdr = QLabel("Parameters read from firmware")
        res_hdr.setObjectName("sp_field")
        res_outer.addWidget(res_hdr)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setObjectName("sp_sep")
        res_outer.addWidget(sep)

        grid_widget = QFrame()
        grid_widget.setObjectName("sp_grid")
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
            lbl.setObjectName("sp_result")
            grid.addWidget(lbl, row, col)
            self._result_labels[key] = lbl

        res_outer.addWidget(grid_widget)
        root.addWidget(res_card, 1)

    def _apply_style(self):
        p = _get_palette()
        self.setStyleSheet(f"""
            QDialog {{ background: {p['bg']}; }}
            QLabel  {{ background: transparent; color: {p['text']}; }}
            QLabel#sp_title  {{ font-size: {_px(15)}px; font-weight: 700; color: {p['text']}; }}
            QLabel#sp_sub    {{ font-size: {_px(11)}px; color: {p['muted']}; }}
            QLabel#sp_field  {{ font-size: {_px(11)}px; font-weight: 600; color: {p['text2']}; }}
            QLabel#sp_result {{ font-size: {_px(11)}px; color: {p['text2']}; font-family: Consolas, monospace; }}
            QLabel#sp_status_ok  {{ font-size: {_px(11)}px; font-weight: 600; color: {p['blue']}; }}
            QLabel#sp_status_err {{ font-size: {_px(11)}px; font-weight: 600; color: {p['red']}; }}
            QFrame#sp_card   {{ background: {p['white']}; border: 1px solid {p['border']}; border-radius: {_px(8)}px; }}
            QFrame#sp_sep    {{ background: {p['border']}; max-height: 1px; border: none; }}
            QFrame#sp_grid   {{ background: transparent; }}
            QLineEdit {{
                background: {p['white']}; color: {p['text']};
                border: 1px solid {p['border']}; border-radius: {_px(5)}px;
                padding: {_px(4)}px {_px(8)}px; font-size: {_px(11)}px; min-height: {_px(22)}px;
            }}
            QLineEdit:focus {{ border-color: {p['red']}; }}
            QPushButton#sp_btn_primary {{
                background: {p['red']}; color: white; border: none;
                border-radius: {_px(5)}px; padding: {_px(7)}px {_px(16)}px;
                font-size: {_px(12)}px; font-weight: 700;
                border-bottom: 2px solid {p.get('red_dark', '#9B1F24')};
            }}
            QPushButton#sp_btn_primary:hover    {{ background: {p.get('red_dark', '#9B1F24')}; }}
            QPushButton#sp_btn_primary:disabled {{ background: {p['faint']}; color: #EEEEEE; border-bottom: none; }}
            QPushButton#sp_btn_outline {{
                background: {p['white']}; color: {p['text2']};
                border: 1.5px solid {p['border']}; border-radius: {_px(5)}px;
                font-size: {_px(11)}px; padding: {_px(4)}px {_px(8)}px;
            }}
            QPushButton#sp_btn_outline:hover {{ border-color: {p['red']}; color: {p['red']}; }}
            QToolTip {{
                background: #1E293B; color: #F1F5F9;
                border: 1px solid #334155; border-radius: {_px(4)}px;
                padding: {_px(4)}px {_px(8)}px; font-size: {_px(11)}px;
            }}
        """)

    def _browse(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Select or Create File", "",
            "Text Files (*.txt);;All Files (*)")
        if path:
            self.path_entry.setText(path)

    def _on_save(self):
        if not self.serial_manager.is_open:
            self._on_error("Not connected to serial port.")
            return
        file_path = self.path_entry.text().strip()
        if not file_path:
            self._on_error("Please select or enter a file path.")
            return

        self.save_btn.setEnabled(False)
        self.save_btn.setText("Saving…")
        self._on_status("Reading parameters from firmware…")
        save_to_ctrl = self._chk_save_to_ctrl.isChecked()

        def worker():
            try:
                results = {}
                for key, cmd, disp, unit, scale in PARAM_DEFS:
                    raw = self.serial_manager.send(cmd, expect_response=True)
                    val = dec_decode(raw)
                    results[key] = (val, disp, unit, scale)

                lines = ["# AMC Drive Parameters\n"]
                for key, (val, disp, unit, _scale) in results.items():
                    lines.append(f"{key} = {val:.10g}\n")

                with open(file_path, 'w', encoding='utf-8') as f:
                    f.writelines(lines)

                logging.info("Parameters saved to %s", file_path)

                if save_to_ctrl:
                    try:
                        self.serial_manager.send("s smpar", expect_response=False)
                        logging.info("s smpar sent — parameters written to controller NV memory")
                    except Exception as _se:
                        logging.warning("s smpar failed: %s", _se)

                self._sig_done.emit(results)
            except Exception as e:
                logging.exception("Save failed")
                self._sig_error.emit(f"Save failed: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def _on_done(self, results: dict):
        for key, (val, disp, unit, scale) in results.items():
            lbl = self._result_labels.get(key)
            if not lbl:
                continue
            display_val = val * scale
            if key == "mpole":
                text = f"{disp}:  {int(round(val))}"
            elif abs(display_val) < 0.001 and display_val != 0.0:
                text = f"{disp}:  {display_val:.4e} {unit}"
            else:
                text = f"{disp}:  {display_val:.4g} {unit}"
            lbl.setText(text)
        self.save_btn.setEnabled(True)
        self.save_btn.setText("Save Parameters")
        self._on_status("Saved successfully.")

    def _on_error(self, msg: str):
        self.save_btn.setEnabled(True)
        self.save_btn.setText("Save Parameters")
        self.status_lbl.setObjectName("sp_status_err")
        self.status_lbl.style().unpolish(self.status_lbl)
        self.status_lbl.style().polish(self.status_lbl)
        self.status_lbl.setText(msg)

    def _on_status(self, msg: str):
        self.status_lbl.setObjectName("sp_status_ok")
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
    dlg = SaveParameters(None, DummySerial())
    dlg.show()
    sys.exit(app.exec())
