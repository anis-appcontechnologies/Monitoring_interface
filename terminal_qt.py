#!/usr/bin/env python3
"""
Terminal — PySide6 edition

Modern serial terminal dialog matching the AMC Interface app.
Behavior preserved 1:1 from the Tkinter version (terminal.py):
  • Sends raw commands via SerialManager (expect_response=True)
  • Shows sent commands and responses with color tags
  • Timestamps every line, auto-scrolls
  • Clear / Copy buttons
  • Entry cleared immediately after Send/Enter (fast terminal feel)
"""

import logging
import threading
import time

from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QTextEdit, QFrame, QSizePolicy, QApplication,
)

try:
    import qtawesome as qta
    _QTA = True
except ImportError:
    _QTA = False

def _px(n: int) -> int:
    try:
        from PySide6.QtWidgets import QApplication
        s = QApplication.primaryScreen()
        if s is not None:
            return max(1, round(n * s.logicalDotsPerInch() / 96.0))
    except Exception:
        pass
    return n

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(message)s")


def _get_palette():
    try:
        import amc_interface_qt as _amcqt
        return _amcqt.C
    except Exception:
        return {
            "white": "#FFFFFF", "bg": "#F0F0F0", "border": "#D8D8D8",
            "text": "#1A1A1A", "text2": "#3A3A3A", "muted": "#707070",
            "red": "#C0272D", "red_bg": "#F9ECEC", "red_border": "#E8AAAC",
            "blue": "#1976D2", "blue_dark": "#1255A0", "blue_light": "#EBF3FC",
            "input_bg": "#F7F7F7", "faint": "#B0B0B0",
        }


class Terminal(QDialog):
    """Modern serial terminal dialog. Drop-in replacement for the Tkinter Terminal."""

    _sig_log_response = Signal(str, bool)   # message, is_error
    _sig_reset_ui     = Signal()

    def __init__(self, parent, serial_manager):
        super().__init__(parent)
        self.serial_manager = serial_manager

        self.setWindowTitle("AMC Terminal")
        _scr = QApplication.primaryScreen().availableGeometry()
        self.resize(min(_px(640), int(_scr.width() * 0.7)), min(_px(480), int(_scr.height() * 0.7)))
        self.setMinimumSize(_px(540), _px(400))

        self._sig_log_response.connect(self._display_response)
        self._sig_reset_ui.connect(self._reset_ui_state)

        self._build_ui()
        self._apply_style()

    # ── UI ─────────────────────────────────────────────────────────────────────
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        # Header
        title = QLabel("Serial Terminal")
        title.setObjectName("term_title")
        sub = QLabel("Send raw commands to the AMC device")
        sub.setObjectName("term_sub")
        root.addWidget(title)
        root.addWidget(sub)

        # Card: input row
        input_card = QFrame()
        input_card.setObjectName("term_card")
        in_lay = QHBoxLayout(input_card)
        in_lay.setContentsMargins(12, 10, 12, 10)
        in_lay.setSpacing(8)

        cmd_lbl = QLabel("Command")
        cmd_lbl.setObjectName("term_field")
        in_lay.addWidget(cmd_lbl)

        self.cmd_entry = QLineEdit()
        self.cmd_entry.setPlaceholderText("e.g. g speed")
        self.cmd_entry.setToolTip("Enter command (e.g. g speed)")
        self.cmd_entry.returnPressed.connect(self.on_send)
        in_lay.addWidget(self.cmd_entry, 1)

        self.send_btn = QPushButton("Send")
        self.send_btn.setObjectName("term_btn_primary")
        self.send_btn.setToolTip("Send command to device (Enter also works)")
        self.send_btn.setFixedWidth(_px(80))
        self.send_btn.clicked.connect(self.on_send)
        if _QTA:
            self.send_btn.setIcon(qta.icon("fa5s.paper-plane", color="#FFFFFF"))
            self.send_btn.setIconSize(QSize(12, 12))
        in_lay.addWidget(self.send_btn)

        root.addWidget(input_card)

        # Card: log
        log_card = QFrame()
        log_card.setObjectName("term_card")
        log_lay = QVBoxLayout(log_card)
        log_lay.setContentsMargins(12, 10, 12, 10)
        log_lay.setSpacing(6)

        log_hdr = QHBoxLayout()
        log_hdr.setSpacing(6)
        log_title = QLabel("Communication Log")
        log_title.setObjectName("term_field")
        log_hdr.addWidget(log_title, 1)

        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setObjectName("term_btn_outline")
        self.clear_btn.setFixedWidth(_px(72))
        self.clear_btn.clicked.connect(self.clear_log)
        if _QTA:
            self.clear_btn.setIcon(qta.icon("fa5s.trash-alt", color="#6b7280"))
            self.clear_btn.setIconSize(QSize(11, 11))
        log_hdr.addWidget(self.clear_btn)

        self.copy_btn = QPushButton("Copy")
        self.copy_btn.setObjectName("term_btn_outline")
        self.copy_btn.setFixedWidth(_px(72))
        self.copy_btn.clicked.connect(self.copy_log)
        if _QTA:
            self.copy_btn.setIcon(qta.icon("fa5s.copy", color="#6b7280"))
            self.copy_btn.setIconSize(QSize(11, 11))
        log_hdr.addWidget(self.copy_btn)
        log_lay.addLayout(log_hdr)

        self.log_text = QTextEdit()
        self.log_text.setObjectName("term_log")
        self.log_text.setReadOnly(True)
        self.log_text.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        log_lay.addWidget(self.log_text, 1)

        root.addWidget(log_card, 1)

        # Status row
        self.status_label = QLabel("Ready")
        self.status_label.setObjectName("term_status_ok")
        root.addWidget(self.status_label)

        self.cmd_entry.setFocus()

    def _apply_style(self):
        p = _get_palette()
        self.setStyleSheet(f"""
            QDialog {{ background: {p['bg']}; }}
            QLabel {{ background: transparent; color: {p['text']}; }}
            QLabel#term_title {{ font-size: {_px(16)}px; font-weight: 700; color: {p['text']}; }}
            QLabel#term_sub   {{ font-size: {_px(11)}px; color: {p['muted']}; }}
            QLabel#term_field {{ font-size: {_px(11)}px; font-weight: 600; color: {p['text2']}; }}
            QLabel#term_status_ok  {{ font-size: {_px(11)}px; font-weight: 700; color: #2E6B2A; }}
            QLabel#term_status_busy{{ font-size: {_px(11)}px; font-weight: 700; color: {p['blue']}; }}
            QLabel#term_status_err {{ font-size: {_px(11)}px; font-weight: 700; color: {p['red']}; }}
            QFrame#term_card {{
                background: {p['white']}; border: 1px solid {p['border']}; border-radius: {_px(8)}px;
            }}
            QLineEdit {{
                background: {p['white']}; color: {p['text']};
                border: 1px solid {p['border']}; border-radius: {_px(5)}px;
                padding: {_px(4)}px {_px(8)}px; font-size: {_px(12)}px;
                font-family: "Consolas", "Courier New", monospace;
                min-height: {_px(22)}px;
                selection-background-color: {p['red']};
            }}
            QLineEdit:focus {{ border-color: {p['red']}; }}
            QPushButton#term_btn_primary {{
                background: {p['red']}; color: white; border: none;
                border-radius: {_px(5)}px; padding: {_px(6)}px {_px(12)}px;
                font-size: {_px(12)}px; font-weight: 700;
            }}
            QPushButton#term_btn_primary:hover    {{ background: {p.get('red_dark', '#9B1F24')}; }}
            QPushButton#term_btn_primary:disabled {{ background: {p['faint']}; color: #EEEEEE; }}
            QPushButton#term_btn_outline {{
                background: {p['white']}; color: {p['text2']};
                border: 1.5px solid {p['border']}; border-radius: {_px(5)}px;
                font-size: {_px(11)}px; padding: {_px(4)}px {_px(8)}px;
            }}
            QPushButton#term_btn_outline:hover {{
                border-color: {p['red']}; color: {p['red']}; background: {p['red_bg']};
            }}
            QTextEdit#term_log {{
                background: #1A1A1A; color: #E8E8E8;
                border: 1px solid #2A2A2A; border-radius: {_px(6)}px;
                font-family: "Consolas", "Courier New", monospace;
                font-size: {_px(12)}px; padding: {_px(8)}px;
            }}
            QTextEdit#term_log QScrollBar:vertical {{
                background: #1A1A1A; width: {_px(8)}px; border-radius: {_px(4)}px;
            }}
            QTextEdit#term_log QScrollBar::handle:vertical {{
                background: #4A4A4A; border-radius: {_px(4)}px; min-height: {_px(24)}px;
            }}
            QToolTip {{
                background: #1E293B; color: #F1F5F9;
                border: 1px solid #334155; border-radius: {_px(4)}px;
                padding: {_px(4)}px {_px(8)}px; font-size: {_px(11)}px;
            }}
        """)

    # ── Logging ────────────────────────────────────────────────────────────────
    def log(self, message: str, kind: str = "response"):
        """Append a colored, timestamped line to the log."""
        ts = time.strftime("%H:%M:%S")
        color_map = {
            "command":   "#C49AE6",   # purple-ish on dark bg
            "response":  "#86EFAC",   # green
            "error":     "#FCA5A5",   # red
        }
        ts_color = "#94A3B8"
        line_color = color_map.get(kind, "#E8E8E8")
        # Escape HTML
        safe = (message
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;"))
        html = (f'<span style="color:{ts_color};">[{ts}]</span> '
                f'<span style="color:{line_color};">{safe}</span>')
        self.log_text.append(html)
        sb = self.log_text.verticalScrollBar()
        sb.setValue(sb.maximum())

    def clear_log(self):
        self.log_text.clear()
        self._set_status("Log cleared", "busy")
        from PySide6.QtCore import QTimer
        QTimer.singleShot(1500, lambda: self._set_status("Ready", "ok"))

    def copy_log(self):
        QApplication.clipboard().setText(self.log_text.toPlainText())
        self._set_status("Log copied to clipboard", "busy")
        from PySide6.QtCore import QTimer
        QTimer.singleShot(1500, lambda: self._set_status("Ready", "ok"))

    def _set_status(self, text: str, kind: str = "ok"):
        self.status_label.setText(text)
        obj = {"ok": "term_status_ok", "busy": "term_status_busy",
               "err": "term_status_err"}.get(kind, "term_status_ok")
        self.status_label.setObjectName(obj)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    # ── Send flow ──────────────────────────────────────────────────────────────
    def on_send(self):
        cmd = self.cmd_entry.text().strip()
        if not cmd:
            self._set_status("Enter a command first", "err")
            return

        # Echo immediately + clear input
        self.log(f"→ {cmd}", "command")
        self.cmd_entry.clear()
        self.cmd_entry.setFocus()

        self.send_btn.setEnabled(False)
        self.cmd_entry.setEnabled(False)
        self._set_status("Sending...", "busy")

        def worker():
            try:
                if not self.serial_manager.is_open:
                    raise RuntimeError("Serial port is not open.")
                response = self.serial_manager.send(cmd, expect_response=True)
                display_text = response.strip() or "(no response)"
                self._sig_log_response.emit(display_text, False)
                logging.info("Terminal sent: %s → received: %s", cmd, response)
            except Exception as e:
                logging.exception("Terminal command failed")
                self._sig_log_response.emit(f"Error: {e}", True)
            finally:
                self._sig_reset_ui.emit()

        threading.Thread(target=worker, daemon=True).start()

    def _display_response(self, text: str, is_error: bool):
        self.log(text or "(no response)", "error" if is_error else "response")
        self._set_status("Error" if is_error else "Received",
                         "err" if is_error else "ok")

    def _reset_ui_state(self):
        self.send_btn.setEnabled(True)
        self.cmd_entry.setEnabled(True)
        self.cmd_entry.setFocus()
        if self.status_label.text() in ("Sending...", "Received", "Error"):
            from PySide6.QtCore import QTimer
            QTimer.singleShot(800, lambda: self._set_status("Ready", "ok"))


# ── Standalone testing ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    class DummySerial:
        def __init__(self):
            self.is_open = True
        def send(self, command, expect_response=False):
            if expect_response:
                if "mrs" in command.lower():
                    return "+00056"
                if "clrerr" in command.lower():
                    return "OK"
                return "+00042"
            return ""

    app = QApplication(sys.argv)
    app.setFont(QFont("Segoe UI", 10))
    dlg = Terminal(None, DummySerial())
    dlg.show()
    sys.exit(app.exec())
