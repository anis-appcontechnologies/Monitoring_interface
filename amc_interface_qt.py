#!/usr/bin/env python3
"""
Appcon GUI + Serial Interface  — PySide6 Edition
UI rebuilt to match target design: numbered section cards, header bar,
sliders for references, commands table with Send panel, dark activity log.
All serial/threading/queue/state-machine logic preserved unchanged.
Author: DAGBAGI Mohamed  (UI rebuild: Appcon Technologies)

FILE MAP (search by line number to jump to any section)
────────────────────────────────────────────────────────
  L 20   Imports & top-level helpers (dec_encode/dec_decode moved to protocol.py)
  L 784  SerialManager         — serial port open/close/send, thread lock
  L 857  QueuedCommandManager  — queued write commands with ACK tracking
  L 935  _FlowArrow            — animated data-flow arrow widget
  L 1067 _ModernModal          — styled modal dialog (error / warn / info)
  L 1232 _LogFileManager       — rotating log file writer
  L 1312 AMCMainWindow         — main application window
         L 1546  _port_watch_tick      USB plug/unplug detection (deferred disconnect)
         L 1660  _build_serial_card    Section 1: COM port + Connect/Disconnect UI
         L 1758  _build_status_card    Section 2: mode pill, fault indicator, get-vars
         L 2258  _build_menu           Menu bar (File / Identification / Terminal / Monitoring)
         L 2339  _set_connected_ui     UI state on connect (enables controls, logs port)
         L 2378  _set_disconnected_ui  UI state on disconnect
         L 2566  _toggle_theme         Dark / light mode switch
         L 2988  _on_connect           Opens serial port in background thread, reads fpwm
         L 3025  _on_disconnect        Stops all loops, closes port
         L 3324  _start/stop loops     Four background polling loops (status/fault/get/cmd)
         L 3513  _append_log           HTML-safe activity log renderer
         L 3694  closeEvent            Quit confirmation modal + cleanup
"""

import math
import threading
import time
import queue
import logging
import sys
import os
import datetime
import atexit
import shutil
from html import escape
from enum import Enum

# ── Decimal encode/decode ─────────────────────────────────────────────────────
def dec_encode(value: float) -> str:
    sign = '-' if value < 0 else '+'
    absval = abs(value)
    if absval > 999999999.0:
        absval = 999999999.0
    int_part = int(absval)
    int_digits = 1 if int_part == 0 else len(str(int_part))
    frac_digits = 0 if int_digits >= 9 else 8 - int_digits
    formatted = f"{absval:.{frac_digits}f}"
    result = sign + formatted
    return result.ljust(10)[:10]


def dec_decode(s: str) -> float:
    s = s.strip()
    if not s:
        raise ValueError("Empty decimal string")
    return float(s)


# ── Optional pyserial ─────────────────────────────────────────────────────────
try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = None
    list_ports = None

APP_PROJECT = "ESC1"
APP_VERSION = "60420"

logging.basicConfig(level=logging.DEBUG,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logging.getLogger("matplotlib").setLevel(logging.WARNING)
logging.getLogger("matplotlib.font_manager").setLevel(logging.WARNING)

# ── PySide6 imports ───────────────────────────────────────────────────────────
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QDialog, QFrame, QLabel, QPushButton,
    QLineEdit, QComboBox, QButtonGroup, QTextEdit, QSlider,
    QScrollArea, QSizePolicy, QGridLayout, QHBoxLayout,
    QVBoxLayout, QMenuBar, QMenu, QFileDialog,
    QStatusBar, QSpacerItem, QSplitter, QSplitterHandle,
)
from PySide6.QtCore import (
    Qt, QTimer, Signal, QSize, QPoint, QRect,
    QPropertyAnimation, QEasingCurve, QSettings,
)
from PySide6.QtGui import (
    QFont, QColor, QIcon, QPixmap, QPainter, QPen, QBrush,
    QAction, QPolygon, QKeySequence, QShortcut,
)
from PySide6.QtWidgets import QGraphicsOpacityEffect, QGraphicsDropShadowEffect

try:
    import qtawesome as qta
except ImportError as _qta_err:
    raise RuntimeError(
        "QtAwesome is required for consistent icons across all PCs. "
        "Install with: pip install QtAwesome"
    ) from _qta_err

# ═══════════════════════════════════════════════════════════════════════════════
#  DESIGN TOKENS
# ═══════════════════════════════════════════════════════════════════════════════

C = {
    # ── Appcon brand palette ──────────────────────────────────────────────────
    "bg":           "#F0F0F0",   # light neutral page background
    "card":         "#FFFFFF",   # white cards
    "border":       "#D8D8D8",   # subtle grey borders
    # primary: Appcon red
    "blue":         "#C0272D",   # primary action (was blue — now Appcon red)
    "blue_dark":    "#9B1F24",   # hover / pressed
    "blue_light":   "#F9ECEC",   # subtle red tint backgrounds
    "blue_text":    "#7A1217",   # dark red text
    # status colours — kept neutral / functional
    "red":          "#C0272D",   # Appcon red for errors / disconnected
    "red_bg":       "#F9ECEC",
    "red_border":   "#E8AAAC",
    "green":        "#3D8B37",   # success / connected
    "green_dark":   "#2E6B2A",
    "green_bg":     "#EDF7EC",
    "green_border": "#A8D5A5",
    "orange":       "#C07820",   # warning
    "orange_bg":    "#FDF3E3",
    "orange_border":"#E8C87A",
    # typography
    "text":         "#1A1A1A",   # near-black — strong readability
    "text2":        "#3A3A3A",
    "muted":        "#707070",
    "faint":        "#A8A8A8",
    # surfaces
    "input_bg":     "#F7F7F7",
    "sep":          "#D8D8D8",
    "log_bg":       "#1A1A1A",   # near-black log panel — matches text colour
    "log_text":     "#E8E8E8",
    "white":        "#FFFFFF",
    "gray_mid":     "#A8A8A8",
}

C_LIGHT = dict(C)

C_DARK = {
    "bg":           "#090D12",   # very dark page — maximum contrast base
    "card":         "#141A22",   # clearly lighter than bg
    "border":       "#252D38",   # visible but not harsh
    "blue":         "#E05A5F",
    "blue_dark":    "#C0272D",
    "blue_light":   "#2D1215",
    "blue_text":    "#F08080",
    "red":          "#E05A5F",
    "red_bg":       "#2D1215",
    "red_border":   "#7A2A2D",
    "green":        "#3FB950",
    "green_dark":   "#56D364",
    "green_bg":     "#0B2510",
    "green_border": "#1E6025",
    "orange":       "#E3A44A",
    "orange_bg":    "#281B04",
    "orange_border":"#7A5010",
    "text":         "#F0F6FC",   # brighter white text
    "text2":        "#D0D8E4",
    "muted":        "#7D8EA0",
    "faint":        "#2D3744",
    "input_bg":     "#1A2030",   # clearly darker than card
    "sep":          "#252D38",
    "log_bg":       "#05080C",   # deepest surface
    "log_text":     "#B8C8D8",
    "white":        "#141A22",   # card color
    "gray_mid":     "#3D4A58",
}

_THEME = "light"

# ── Optional sub-modules (imported AFTER C/C_DARK so _get_palette() works) ───
try:
    from electrical_params_qt import ElectricalParametersIdentification
except ImportError:
    ElectricalParametersIdentification = None
    logging.warning("electrical_params_qt not found — Electrical Parameters dialog unavailable")

try:
    from save_params_qt import SaveParameters
except ImportError:
    SaveParameters = None
    logging.warning("save_params_qt not found — Save Parameters dialog unavailable")

try:
    from load_params_qt import LoadParameters
except ImportError:
    LoadParameters = None
    logging.warning("load_params_qt not found — Load Parameters dialog unavailable")

try:
    from psif_param import PMFluxIdentification
except ImportError:
    PMFluxIdentification = None

try:
    from inertia_param_qt import InertiaIdentification
except ImportError:
    InertiaIdentification = None
    logging.warning("inertia_param_qt not found — Mechanical Parameters dialog unavailable")

try:
    from terminal_qt import Terminal
except ImportError:
    Terminal = None
    logging.warning("terminal_qt not found — Terminal dialog unavailable")

try:
    from scope_qt import ScopeWindow, _apply_mono, MONO_FONT_FAMILY
    _SCOPE_QT = True
except ImportError:
    _SCOPE_QT = False
    ScopeWindow = None
    MONO_FONT_FAMILY = "Consolas, monospace"
    def _apply_mono(w): pass

# DPI scale factor — set once in main() after QApplication starts.
# All fixed pixel sizes in widget code are multiplied by _S so the layout
# looks identical regardless of the OS display scale (100%, 125%, 150%, 200%).
_S: float = 1.0

def _px(n: int) -> int:
    """Scale a pixel value by the current DPI factor."""
    return max(1, round(n * _S))

def _make_arrow_png(color_hex: str, path: str):
    """Paint a 10×10 downward chevron and save as PNG."""
    pix = QPixmap(10, 10)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(color_hex))
    pen.setWidth(2)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.drawLine(1, 3, 5, 7)
    p.drawLine(5, 7, 9, 3)
    p.end()
    pix.save(path, "PNG")

def _make_pencil_icon(color_hex: str, size: int) -> QPixmap:
    """Draw a pencil icon using QPainter — no external dependency."""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    col = QColor(color_hex)
    pen = QPen(col, max(1, size // 10))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    s = size
    # Pencil body: diagonal rectangle from top-right to bottom-left
    body = [QPoint(int(s*0.65), int(s*0.05)), QPoint(int(s*0.95), int(s*0.35)),
            QPoint(int(s*0.35), int(s*0.95)), QPoint(int(s*0.05), int(s*0.65))]
    p.setBrush(QBrush(col))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawPolygon(QPolygon(body))
    # Tip triangle
    tip = [QPoint(int(s*0.05), int(s*0.65)), QPoint(int(s*0.35), int(s*0.95)),
           QPoint(int(s*0.05), int(s*0.95))]
    tip_col = QColor(color_hex)
    tip_col.setAlpha(140)
    p.setBrush(QBrush(tip_col))
    p.drawPolygon(QPolygon(tip))
    p.end()
    return pix


def _make_eye_icon(color_hex: str, size: int) -> QPixmap:
    """Draw an eye icon using QPainter — no external dependency."""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    col = QColor(color_hex)
    pen = QPen(col, max(1, size // 9))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    cx, cy = size // 2, size // 2
    ew, eh = int(size * 0.88), int(size * 0.52)
    # Eye outline arc (top and bottom arcs meeting at corners)
    from PySide6.QtCore import QRectF
    p.drawArc(QRectF(cx - ew//2, cy - eh//2, ew, eh), 0, 180 * 16)
    p.drawArc(QRectF(cx - ew//2, cy - eh//2, ew, eh), 180 * 16, 180 * 16)
    # Pupil
    pr = max(2, size // 6)
    p.setBrush(QBrush(col))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(QPoint(cx, cy), pr, pr)
    p.end()
    return pix


import tempfile as _tempfile, os as _os
_ARROW_DIR   = _tempfile.mkdtemp(prefix="amc_arrows_")
atexit.register(shutil.rmtree, _ARROW_DIR, True)
_ARROW_MUTED = _os.path.join(_ARROW_DIR, "arrow_muted.png").replace("\\", "/")
_ARROW_BLUE  = _os.path.join(_ARROW_DIR, "arrow_blue.png").replace("\\", "/")

def _build_qss(palette):
    p = palette
    # Scale all font sizes by DPI factor so text is legible on any screen.
    def fs(n): return _px(n)
    return f"""
QMainWindow {{ background: {p['bg']}; }}
QWidget#central {{ background: {p['bg']}; }}
QWidget#scroll_inner {{ background: {p['bg']}; }}
QScrollArea {{ background: transparent; border: none; }}

QFrame#header_bar {{
    background: {p['white']};
    border: none;
    border-bottom: 2px solid qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {p['blue']}, stop:0.6 {p['blue']}, stop:1 transparent);
}}

QFrame#card {{
    background: {p['white']};
    border: 1px solid {p['border']};
    border-radius: 8px;
    border-bottom: 2px solid {p['border']};
}}


QLabel#modes_active_pill {{
    background: {p['blue_light']};
    color: {p['blue']};
    border: 1.5px solid {p['blue']};
    border-radius: 12px;
    padding: 4px 14px;
    font-size: {fs(12)}px; font-weight: 700;
    letter-spacing: 0.5px;
}}
QLabel#modes_active_pill[mode="Stop"] {{
    background: {p['red_bg']};
    color: {p['red']};
    border: 1.5px solid {p['red_border']};
}}
QLabel#modes_active_pill[mode="active"] {{
    background: {p['orange_bg']};
    color: {p['orange']};
    border: 1.5px solid {p['orange_border']};
}}

QLabel {{ background: transparent; color: {p['text']}; }}
QLabel#sec_title  {{ font-size: {fs(15)}px; font-weight: 700; color: {p['text']}; }}
QLabel#sec_sub    {{ font-size: {fs(10)}px; color: {p['muted']}; }}
QLabel#field_lbl  {{ font-size: {fs(11)}px; font-weight: 500; color: {p['text2']}; }}
QLabel#unit_lbl   {{ font-size: {fs(11)}px; color: {p['muted']}; }}
QLabel#range_lbl  {{ font-size: {fs(9)}px; color: {p['faint']}; }}

QLabel#conn_pill_dis {{
    background: {p['red_bg']}; color: {p['red']};
    border: 2px solid {p['red_border']};
    border-radius: 10px; padding: 7px 10px;
    font-size: {fs(13)}px; font-weight: 700;
    qproperty-alignment: AlignCenter;
    min-width: 120px;
}}
QLabel#conn_pill_ok {{
    background: {p['green_bg']}; color: {p['green_dark']};
    border: 2px solid {p['green_border']};
    border-radius: 10px; padding: 7px 10px;
    font-size: {fs(13)}px; font-weight: 700;
    qproperty-alignment: AlignCenter;
    min-width: 120px;
}}
QLabel#fault_ok {{
    background: {p['green_bg']}; color: {p['green_dark']};
    border: 2px solid {p['green_border']};
    border-radius: 10px; padding: 7px 10px;
    font-size: {fs(13)}px; font-weight: 700;
    qproperty-alignment: AlignCenter;
}}
QLabel#fault_err {{
    background: {p['red_bg']}; color: {p['red']};
    border: 2px solid {p['red_border']};
    border-radius: 10px; padding: 7px 10px;
    font-size: {fs(13)}px; font-weight: 700;
    qproperty-alignment: AlignCenter;
}}
QLabel#fault_unknown {{
    background: {p['bg']}; color: {p['muted']};
    border: 1.5px solid {p['border']};
    border-radius: 10px; padding: 7px 10px;
    font-size: {fs(13)}px; font-weight: 600;
    font-style: italic;
    qproperty-alignment: AlignCenter;
}}
QLabel#tbl_hdr {{
    font-size: {fs(11)}px; font-weight: 600;
    color: {p['muted']}; border-bottom: 1px solid {p['border']};
    padding-bottom: 4px;
}}
QLabel#conn_status_pill {{
    background: {p['red_bg']}; color: {p['red']};
    border: 1.5px solid {p['red_border']};
    border-radius: 10px; padding: 4px 12px;
    font-size: {fs(12)}px; font-weight: 700;
}}
QLabel#conn_status_pill_ok {{
    background: {p['green_bg']}; color: {p['green_dark']};
    border: 1.5px solid {p['green_border']};
    border-radius: 10px; padding: 4px 12px;
    font-size: {fs(12)}px; font-weight: 700;
}}

QPushButton {{
    border-radius: 5px; font-size: {fs(11)}px; padding: 4px 10px;
}}
QPushButton#btn_primary {{
    background: {p['blue']}; color: white;
    border: none; font-weight: 700;
    font-size: {fs(12)}px; padding: 6px 16px;
    border-bottom: 2px solid {p['blue_dark']};
}}
QPushButton#btn_primary:hover   {{ background: {p['blue_dark']}; }}
QPushButton#btn_primary:pressed {{ background: {p['blue_dark']}; border-bottom: 1px solid {p['blue_dark']}; padding-top: 7px; }}
QPushButton#btn_primary:disabled {{ background: {p['faint']}; color: #888; border-bottom: none; }}

QPushButton#btn_outline {{
    background: {p['white']}; color: {p['text2']};
    border: 1.5px solid {p['border']};
}}
QPushButton#btn_outline:hover {{
    border-color: {p['blue']}; color: {p['blue']};
    background: {p['blue_light']};
}}
QPushButton#btn_outline:pressed {{
    background: {p['blue_light']}; border-color: {p['blue_dark']};
}}
QPushButton#btn_outline:disabled {{ color: {p['faint']}; background: {p['bg']}; border-color: {p['border']}; }}

QPushButton#btn_danger_outline {{
    background: {p['white']}; color: {p['red']};
    border: 1.5px solid {p['red_border']};
}}
QPushButton#btn_danger_outline:hover {{ background: {p['red_bg']}; }}
QPushButton#btn_danger_outline:disabled {{ color: {p['faint']}; border-color: {p['border']}; background: {p['bg']}; }}

QPushButton#btn_clear_fault {{
    background: {p['red_bg']}; color: {p['red']};
    border: 1.5px solid {p['red_border']};
    border-radius: 8px;
    font-size: {fs(11)}px; font-weight: 700;
    padding: 4px 10px;
}}
QPushButton#btn_clear_fault:hover {{
    background: {p['red']}; color: white;
    border-color: {p['red']};
}}
QPushButton#btn_clear_fault:pressed {{
    background: {p['blue_dark']}; color: white;
}}
QPushButton#btn_clear_fault:disabled {{
    background: {p['bg']}; color: {p['faint']};
    border-color: {p['border']};
}}

QPushButton#btn_theme {{
    background: {p['white']}; color: {p['text2']};
    border: 1.5px solid {p['border']}; border-radius: 5px;
    font-size: {fs(13)}px; padding: 3px 8px;
}}
QPushButton#btn_theme:hover {{ border-color: {p['blue']}; color: {p['blue']}; }}

QPushButton#send_row_btn {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 {p['green']}, stop:1 {p['green_dark']});
    color: white; border: none;
    font-size: {fs(11)}px; font-weight: 600;
    border-radius: 5px; padding: 4px 10px;
    border-bottom: 2px solid {p['green_dark']};
}}
QPushButton#send_row_btn {{ outline: none; }}
QPushButton#send_row_btn:hover {{ background: {p['green']}; border-bottom: 2px solid {p['green_dark']}; }}
QPushButton#send_row_btn:pressed {{ border-bottom: 1px solid {p['green_dark']}; padding-top: 5px; }}
QPushButton#send_row_btn:disabled {{ background: {p['faint']}; color: #888; border-bottom: none; }}

QPushButton#radio_btn, QPushButton#radio_btn_stop, QPushButton#radio_btn_active {{
    background: {p['bg']}; color: {p['muted']};
    border: 1px solid {p['border']}; border-radius: 6px;
    font-size: {fs(12)}px; font-weight: 500; padding: 5px 8px;
    outline: none;
}}
QPushButton#radio_btn:hover {{
    border-color: {p['blue']}; color: {p['blue']};
    background: {p['blue_light']};
}}
QPushButton#radio_btn_stop:hover {{
    border-color: {p['red_border']}; color: {p['red']};
    background: {p['red_bg']};
}}
QPushButton#radio_btn_stop_active:hover {{
    border-color: {p['text2']}; color: {p['text']};
    background: {p['bg']};
}}
QPushButton#radio_btn_active:hover {{
    border-color: {p['blue']}; color: {p['blue']};
    background: {p['blue_light']};
}}
QLabel#group_label {{
    font-size: {fs(12)}px; font-weight: 700; color: {p['text']};
    padding-right: 4px;
}}
QFrame#mode_group_ctrl, QFrame#mode_group_sens {{
    background: {p['red_bg']};
    border: 1px solid {p['red_border']};
    border-left: 3px solid {p['red']};
    border-radius: 6px;
}}
QPushButton#radio_btn:checked {{
    background: {p['blue']}; color: white;
    border: 2px solid {p['blue_dark']}; font-weight: 700; font-size: {fs(12)}px;
}}
QPushButton#radio_btn_stop:checked {{
    background: {p['red']}; color: white;
    border: 2px solid {p['red_border']}; font-weight: 700; font-size: {fs(12)}px;
}}
QPushButton#radio_btn_stop_active, QPushButton#radio_btn_stop_active:checked {{
    background: {p['bg']}; color: {p['muted']};
    border: 1px solid {p['border']}; font-weight: 500; font-size: {fs(12)}px;
}}
QPushButton#radio_btn_active:checked {{
    background: {p['blue']}; color: white;
    border: 2px solid {p['blue_dark']}; font-weight: 700; font-size: {fs(12)}px;
}}
QPushButton#radio_btn:disabled, QPushButton#radio_btn_stop:disabled,
QPushButton#radio_btn_stop_active:disabled, QPushButton#radio_btn_active:disabled {{
    background: {p['bg']}; color: {p['faint']};
    border: 1px solid {p['border']}; font-weight: 400;
}}
QPushButton#radio_btn:checked:disabled, QPushButton#radio_btn_stop:checked:disabled,
QPushButton#radio_btn_stop_active:checked:disabled, QPushButton#radio_btn_active:checked:disabled {{
    background: {p['bg']}; color: {p['faint']};
    border: 1.5px solid {p['border']};
}}

QLineEdit {{
    background: {p['white']}; color: {p['text']};
    border: 2px solid {p['border']};
    border-radius: 5px; padding: 2px 6px; font-size: {fs(11)}px;
    min-height: 20px;
    selection-background-color: {p['blue']};
}}
QLineEdit:hover {{ border-color: {p['blue']}; }}
QLineEdit:focus  {{
    border: 2px solid {p['blue']};
    background: {p['white']};
}}
QLineEdit:disabled {{ background: {p['bg']}; color: {p['faint']}; border-color: {p['border']}; }}
QLineEdit[readOnly="true"] {{
    background: {p['input_bg']};
    color: {p['text2']};
    border: 1px solid {p['border']};
    font-style: italic;
}}
QLineEdit[readOnly="true"]:hover {{ border-color: {p['border']}; }}

QComboBox {{
    background: {p['input_bg']}; color: {p['text']};
    border: 1px solid {p['border']};
    border-radius: 5px; padding: 2px 6px; font-size: {fs(11)}px;
    min-height: 20px;
}}
QComboBox:focus  {{ border-color: {p['blue']}; }}
QComboBox:hover  {{ border-color: {p['blue']}; }}
QComboBox:disabled {{ background: {p['bg']}; color: {p['faint']}; border-color: {p['border']}; }}
QComboBox::drop-down {{ border: none; width: 0px; }}
QComboBox::down-arrow {{ image: none; width: 0px; height: 0px; }}
QComboBox#port_combo {{
    padding-right: 24px;
}}
QComboBox#port_combo::drop-down {{
    border: none; width: 22px;
    subcontrol-origin: padding; subcontrol-position: right center;
}}
QComboBox#port_combo::down-arrow {{
    image: url({_ARROW_MUTED}); width: 10px; height: 10px;
}}
QComboBox#baud_combo {{
    padding-right: 20px; padding-left: 4px;
}}
QComboBox#baud_combo::drop-down {{
    border: none; width: 18px;
    subcontrol-origin: padding; subcontrol-position: right center;
}}
QComboBox#baud_combo::down-arrow {{
    image: url({_ARROW_MUTED}); width: 8px; height: 8px;
}}
QLabel#baud_bps_lbl {{
    font-size: {fs(10)}px; color: {p['muted']}; background: transparent;
}}
QComboBox#var_combo {{ padding-right: 22px; min-width: 60px; }}
QComboBox#var_combo::drop-down {{ border: none; width: 20px; subcontrol-origin: padding; subcontrol-position: right center; }}
QComboBox#var_combo::down-arrow {{ image: url({_ARROW_BLUE}); width: 10px; height: 10px; }}
QComboBox#var_combo QAbstractItemView {{ max-height: 115px; }}
QComboBox QAbstractItemView {{
    background: {p['white']}; border: 1px solid {p['border']};
    border-radius: 6px;
    selection-background-color: {p['blue']}; selection-color: white;
    padding: 4px; font-size: {fs(12)}px;
}}

QTextEdit#log_area {{
    background: {p['log_bg']}; color: {p['log_text']};
    border: 1px solid {p['border']}; border-radius: 8px;
    font-family: "Cascadia Code", "JetBrains Mono", "Fira Code", "Consolas", monospace;
    font-size: {fs(12)}px; padding: 12px 16px;
    line-height: 1.7;
}}
QTextEdit#log_area QScrollBar:vertical {{
    background: {p['log_bg']}; width: 6px; border-radius: 3px;
}}
QTextEdit#log_area QScrollBar::handle:vertical {{
    background: #4A4A4A; border-radius: 3px; min-height: 24px;
}}

QScrollBar:vertical {{
    background: {p['bg']}; width: 6px; border-radius: 3px;
}}
QScrollBar::handle:vertical {{
    background: {p['border']}; border-radius: 3px; min-height: 24px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}

QStatusBar {{
    background: {p['white']}; color: {p['muted']};
    border-top: 1px solid {p['border']}; font-size: {fs(11)}px;
}}

QMenuBar {{
    background: {p['white']}; color: {p['text']};
    border-bottom: 1px solid {p['border']}; font-size: {fs(12)}px;
}}
QMenuBar::item:selected {{ background: {p['blue_light']}; color: {p['blue']}; }}
QMenu {{
    background: {p['white']}; color: {p['text']};
    border: 1px solid {p['border']}; border-radius: 8px; padding: 4px;
}}
QMenu::item {{ padding: 8px 20px; border-radius: 4px; font-size: {fs(12)}px; }}
QMenu::item:selected {{ background: {p['blue_light']}; color: {p['blue']}; }}

QToolTip {{
    background: #1E293B; color: #F1F5F9;
    border: 1px solid #334155; border-radius: 4px;
    padding: 5px 8px; font-size: {fs(11)}px;
}}

QFrame#cmd_write_zone {{
    background: transparent; border: none;
    border-left: 3px solid {p['blue']}; border-radius: 0px;
}}
QFrame#cmd_read_zone {{
    background: transparent; border: none;
    border-left: 3px solid {p['green']}; border-radius: 0px;
}}
QLabel#cmd_zone_hdr_write {{
    font-size: {fs(11)}px; font-weight: 800; color: {p['blue']};
    letter-spacing: 1px; padding: 0px;
}}
QLabel#cmd_zone_hdr_read {{
    font-size: {fs(11)}px; font-weight: 800; color: {p['green_dark']};
    letter-spacing: 1px; padding: 0px;
}}
QLineEdit#read_resp_entry {{
    background: {p['bg']};
    color: {p['text2']};
    border: 1px solid {p['border']};
    border-left: 3px solid {p['green_border']};
    border-radius: 4px;
    font-size: {fs(11)}px;
    font-family: "Consolas", "Cascadia Code", monospace;
    font-style: normal;
    padding: 2px 6px;
}}
QLabel#cmd_zone_hdr {{
    font-size: {fs(11)}px; font-weight: 700; color: {p['muted']};
    letter-spacing: 0.5px; padding: 0px;
}}
QLabel#cmd_arrow {{
    font-size: {fs(18)}px; color: {p['blue']}; background: transparent;
}}
QLabel#cmd_flow_arrow {{
    font-size: {fs(14)}px; font-weight: 700; color: {p['muted']};
    background: transparent; letter-spacing: 2px;
    qproperty-alignment: AlignCenter;
}}
QLabel#cmd_flow_dot {{
    font-size: {fs(16)}px; color: {p['muted']};
    background: transparent;
    qproperty-alignment: AlignCenter;
}}
QWidget#modes_body[disconnected="true"] QLabel {{
    color: {p['faint']};
}}
QWidget#ref_row_locked QLabel {{ color: {p['faint']}; }}
QWidget#ref_row_locked QLineEdit {{
    background: {p['input_bg']}; color: {p['faint']}; border-color: {p['border']};
}}

/* ── Header bar labels ── */
QLabel#header_title {{
    font-size: {fs(16)}px; font-weight: 800; color: {p['text']};
    background: transparent;
}}
QLabel#header_sub {{
    font-size: {fs(10)}px; color: {p['muted']};
    background: transparent;
}}
QLabel#header_logo_text {{
    color: {p['red']}; font-size: {fs(16)}px; font-weight: 800;
    background: transparent;
}}

/* ── Generic separators ── */
QFrame#v_sep {{
    background: {p['border']}; border: none;
}}
QFrame#cmd_write_sep {{
    background: {p['blue_light']}; border: none; max-height: 1px;
}}
QFrame#cmd_read_sep {{
    background: {p['green_border']}; border: none; max-height: 1px;
}}
QFrame#cmd_row_sep {{
    background: {p['bg']}; border: none; max-height: 1px;
}}

/* ── Command zone column headers ── */
QLabel#cmd_col_hdr {{
    font-size: {fs(10)}px; font-weight: 600;
    color: {p['muted']}; background: transparent;
}}

/* ── Disconnected notice in commands card ── */
QLabel#cmd_notice {{
    color: {p['muted']}; font-size: {fs(11)}px;
    font-style: italic; padding: 4px 0px; background: transparent;
}}

/* ── Combined view button ── */
QPushButton#btn_combined_view {{
    background: {p['input_bg']}; color: {p['text']};
    border: 1px solid {p['border']}; border-radius: 6px;
    font-size: {fs(10)}px; font-weight: 600;
    padding: 3px 10px; min-height: 26px;
}}
QPushButton#btn_combined_view:hover {{
    background: {p['blue_light']}; border-color: {p['blue']};
    color: {p['blue']};
}}
QPushButton#btn_combined_view:checked {{
    background: {p['blue']}; color: #FFFFFF; border-color: {p['blue_dark']};
}}
QPushButton#btn_combined_view:checked:hover {{
    background: {p['blue_dark']};
}}

/* ── Splitter handle ── */
QSplitter::handle {{
    background: {p['border']}; width: 6px;
}}
QSplitter::handle:hover {{
    background: {p['muted']};
}}

/* ── Status bar label ── */
QLabel#sb_disconnected {{
    color: {p['red']}; font-weight: 600; font-size: {fs(11)}px;
}}
QLabel#sb_connected {{
    color: {p['green_dark']}; font-weight: 600; font-size: {fs(12)}px;
}}
QLabel#sb_quality {{
    color: {p['muted']}; font-size: {fs(10)}px;
    font-family: "Consolas", monospace;
    padding-right: 8px;
}}

/* ── Locked ref entry (dashed border) ── */
QLineEdit[locked="true"] {{
    background: {p['input_bg']}; color: {p['faint']};
    border: 1px dashed {p['border']}; border-radius: 6px;
}}
"""

QSS = _build_qss(C)

# ═══════════════════════════════════════════════════════════════════════════════
#  LOGIC LAYER  (unchanged)
# ═══════════════════════════════════════════════════════════════════════════════

class SerialManager:
    def __init__(self):
        self._ser = None
        self._lock = threading.Lock()
        self.scope_active = threading.Event()   # set while scope owns the port

    @property
    def is_open(self):
        return self._ser is not None and getattr(self._ser, "is_open", False)

    def connect(self, port: str, baud: int, timeout=1):
        if serial is None:
            raise RuntimeError("pyserial is not installed.")
        with self._lock:
            if self._ser and getattr(self._ser, "is_open", False):
                try:
                    self._ser.close()
                except Exception:
                    pass
            self._ser = serial.Serial(port, baud, timeout=timeout)
            # Flush any stale bytes that arrived during MCU boot/reset
            self._ser.reset_input_buffer()
            logging.info("Connected to %s @ %s baud", port, baud)

    def disconnect(self):
        with self._lock:
            try:
                if self._ser and getattr(self._ser, "is_open", False):
                    self._ser.close()
            except Exception as e:
                logging.warning("Error closing serial: %s", e)
            finally:
                self._ser = None

    def send(self, cmd: str, expect_response=True) -> str:
        if not self.is_open:
            raise RuntimeError("Serial port not open.")
        cmd_parts = cmd.strip().split()
        if cmd_parts and cmd_parts[0] in ("s", "g"):
            parts = cmd_parts
            name = parts[1].ljust(6) if len(parts) > 1 else ""
            rest = " " + " ".join(parts[2:]) if len(parts) > 2 else ""
            cmd = f"#{parts[0]} {name}{rest};"
        if not cmd.endswith("\n"):
            cmd += "\n"
        try:
            with self._lock:
                self._ser.write(cmd.encode("ascii"))
                logging.debug("Sent: %s", cmd.strip())
                if expect_response and cmd_parts and cmd_parts[0] == "g":
                    prompt = self._ser.readline().decode("ascii", errors="ignore").strip()
                    if prompt != "->":
                        raise ValueError(f"Invalid prompt: expected '->', got '{prompt}'")
                    resp = self._ser.readline().decode("ascii", errors="ignore")
                    resp = resp.strip("\r\n")
                    logging.debug("Received value: '%s'", resp)
                    return resp
            return ""
        except (Exception,) as _e:
            if serial and isinstance(_e, (serial.SerialException,
                                          getattr(serial, "SerialTimeoutException", type(None)))):
                raise ConnectionError(f"Cable disconnected: {_e}") from _e
            raise


class CommandState(Enum):
    IDLE = 0
    QUEUED = 1
    EXECUTING = 2
    COMPLETED = 3
    TIMEOUT = 4


class QueuedCommandManager:
    def __init__(self, serial_manager: SerialManager, response_q: queue.Queue):
        self.serial = serial_manager
        self.response_q = response_q
        self.state = CommandState.IDLE
        self.current_command = None
        self.lock = threading.RLock()
        self.timeout_seconds = 35.0
        self.send_time = None
        self.completion_callbacks = []

    def register_completion_callback(self, callback):
        with self.lock:
            self.completion_callbacks.append(callback)

    def send_queued_command(self, command_name: str, timeout_s: float = 35.0) -> bool:
        with self.lock:
            if self.state != CommandState.IDLE:
                return False
            try:
                self.serial.send(f"s {command_name}", expect_response=False)
                self.current_command = command_name
                self.state = CommandState.QUEUED
                self.timeout_seconds = timeout_s
                self.send_time = time.time()
                return True
            except Exception as e:
                logging.error("Failed to send command: %s", e)
                self.state = CommandState.IDLE
                return False

    def poll_queue_status(self):
        with self.lock:
            if self.state == CommandState.IDLE:
                return False
            try:
                queue_size_str = self.serial.send("g qusize", expect_response=True)
                queue_size = int(round(float(queue_size_str)))
                elapsed = time.time() - self.send_time
                if queue_size == 0:
                    self.state = CommandState.COMPLETED
                    cmd_name = self.current_command
                    self.current_command = None
                    self.response_q.put(("cmd_completed", CommandState.COMPLETED, cmd_name))
                    return False
                if elapsed > self.timeout_seconds:
                    self.state = CommandState.TIMEOUT
                    cmd_name = self.current_command
                    self.current_command = None
                    self.response_q.put(("cmd_completed", CommandState.TIMEOUT, cmd_name))
                    return False
                return True
            except Exception as e:
                logging.error("Queue status polling error: %s", e)
                self.state = CommandState.IDLE
                return False

    def is_ready_for_command(self):
        with self.lock:
            return self.state == CommandState.IDLE

    def reset_to_idle(self):
        with self.lock:
            self.state = CommandState.IDLE
            self.current_command = None
            self.send_time = None


# ═══════════════════════════════════════════════════════════════════════════════
#  CUSTOM WIDGETS
# ═══════════════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════════════
#  FLOW ARROW — animated data-packet indicator
# ═══════════════════════════════════════════════════════════════════════════════

class _FlowArrow(QWidget):
    """Draws a right-pointing arrow with animated 'data packet' dots that
    travel left → right when triggered, giving a data-flow feel."""

    _PACKET_COUNT = 3      # simultaneous packets
    _PACKET_W     = 6      # dot width px
    _PACKET_H     = 4      # dot height px
    _DURATION_MS  = 480    # one full traversal
    _STEP_MS      = 16     # ~60 fps

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(_px(34), _px(18))
        self._packets: list[float] = []  # each entry is progress 0.0–1.0
        self._active   = False
        self._steps    = 0
        self._total_steps = max(1, self._DURATION_MS // self._STEP_MS)
        self._timer = QTimer(self)
        self._timer.setInterval(self._STEP_MS)
        self._timer.timeout.connect(self._tick)
        self._render_static()

    def _render_static(self):
        """Pre-render the idle arrow pixmap (reused every paint when idle)."""
        self._static_px = None  # will be drawn in paintEvent

    def trigger(self):
        """Start a data-flow burst. Safe to call while already running."""
        self._packets = [i / self._PACKET_COUNT for i in range(self._PACKET_COUNT)]
        self._active = True
        self._steps  = 0
        if not self._timer.isActive():
            self._timer.start()

    def _tick(self):
        advance = 1.0 / self._total_steps
        self._packets = [p + advance for p in self._packets]
        self._packets = [p for p in self._packets if p <= 1.05]
        self._steps += 1
        if not self._packets:
            self._active = False
            self._timer.stop()
        self.update()

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        cx, cy = w // 2, h // 2

        # ── arrow body ────────────────────────────────────────────────────────
        idle_col = QColor(C["muted"])
        if self._active:
            idle_col = QColor(C["blue"])
            idle_col.setAlpha(180)

        body_y = cy - 1
        p.setPen(QPen(idle_col, 1.5))
        p.drawLine(4, body_y, w - 10, body_y)

        # arrowhead
        p.setBrush(QBrush(idle_col))
        p.setPen(Qt.PenStyle.NoPen)
        tip_x = w - 5
        pts = [
            QPoint(tip_x,     body_y),
            QPoint(tip_x - 7, body_y - 4),
            QPoint(tip_x - 7, body_y + 3),
        ]
        p.drawPolygon(QPolygon(pts))

        # ── animated packets ───────────────────────────────────────────────────
        if self._active:
            arrow_start = 4
            arrow_len   = w - 14
            pkt_w = _px(self._PACKET_W)
            pkt_h = _px(self._PACKET_H)
            base_col = QColor(C["blue"])
            for prog in self._packets:
                # clamp draw range so packets don't spill past arrowhead
                x = int(arrow_start + prog * arrow_len - pkt_w // 2)
                if x > w - 12:
                    continue
                # alpha: fade in from left tail, fade out near arrowhead
                alpha = int(255 * min(prog * 4, 1.0) * min((1.0 - prog) * 3, 1.0))
                col = QColor(base_col)
                col.setAlpha(max(0, min(255, alpha)))
                p.setBrush(QBrush(col))
                p.setPen(Qt.PenStyle.NoPen)
                p.drawRoundedRect(x, cy - pkt_h // 2, pkt_w, pkt_h, 2, 2)

        p.end()


# ═══════════════════════════════════════════════════════════════════════════════
#  GRIP SPLITTER — shows 3 dots on the handle so engineers know it's draggable
# ═══════════════════════════════════════════════════════════════════════════════

class _GripSplitter(QSplitter):
    def createHandle(self):
        return _GripHandle(self.orientation(), self)


class _GripHandle(QSplitterHandle):
    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        dot_color = QColor("#A0A0A0")
        painter.setBrush(QBrush(dot_color))
        painter.setPen(Qt.PenStyle.NoPen)
        cx = self.width() // 2
        cy = self.height() // 2
        r = 2
        gap = 6
        for dy in (-gap, 0, gap):
            painter.drawEllipse(cx - r, cy + dy - r, r * 2, r * 2)
        painter.end()


# Modal button role constants — used as return values from _ModernModal.warn()
MODAL_CONFIRMED = "danger"
MODAL_CANCELLED = "secondary"

# ═══════════════════════════════════════════════════════════════════════════════
#  MODERN MODAL DIALOG
# ═══════════════════════════════════════════════════════════════════════════════

class _ModernModal(QDialog):
    """Styled modal matching the sub-dialog aesthetic.
    level: 'error' | 'warn' | 'info' | 'success'
    buttons: tuple of (label, role) where role in ('primary', 'secondary')
    Returns the clicked role string from exec()."""

    _ICONS = {
        "error":   ("fa5s.exclamation-circle", "#C0272D"),
        "warn":    ("fa5s.exclamation-triangle", "#C07820"),
        "info":    ("fa5s.info-circle", "#1976D2"),
        "success": ("fa5s.check-circle", "#3D8B37"),
    }

    def __init__(self, parent, title, body_html, level="error",
                 buttons=(("OK", "primary"),)):
        super().__init__(parent)
        self._clicked_role = "primary"
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setModal(True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)

        card = QFrame()
        card.setObjectName("modern_modal_card")
        card.setStyleSheet(
            "QFrame#modern_modal_card {"
            f"  background: {C['white']};"
            "  border-radius: 10px;"
            f"  border: 1px solid {C['border']};"
            "}"
        )
        card_lay = QVBoxLayout(card)
        card_lay.setContentsMargins(24, 20, 24, 20)
        card_lay.setSpacing(12)

        # Icon
        icon_name, icon_color = self._ICONS.get(level, self._ICONS["info"])
        icon_lbl = QLabel()
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pix = qta.icon(icon_name, color=icon_color).pixmap(32, 32)
        icon_lbl.setPixmap(pix)
        card_lay.addWidget(icon_lbl)

        # Title
        title_lbl = QLabel(title)
        title_lbl.setObjectName("modal_title")
        title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_lbl.setStyleSheet(
            f"font-size: {_px(14)}px; font-weight: 700; color: {C['text']}; background: transparent;")
        card_lay.addWidget(title_lbl)

        # Body
        body_lbl = QLabel()
        body_lbl.setObjectName("modal_body")
        body_lbl.setText(body_html)
        body_lbl.setWordWrap(True)
        body_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body_lbl.setStyleSheet(
            f"font-size: {_px(12)}px; color: {C['text2']}; background: transparent;")
        card_lay.addWidget(body_lbl)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch()
        for label, role in buttons:
            b = QPushButton(label)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            if role == "primary":
                b.setStyleSheet(
                    f"QPushButton {{ background: {C['blue']}; color: white; border: none; "
                    f"border-radius: 5px; padding: 7px 20px; font-size: {_px(12)}px; font-weight: 700; }}"
                    f"QPushButton:hover {{ background: {C['blue_dark']}; }}")
            else:
                b.setStyleSheet(
                    f"QPushButton {{ background: {C['white']}; color: {C['text2']}; "
                    f"border: 1.5px solid {C['border']}; border-radius: 5px; "
                    f"padding: 7px 20px; font-size: {_px(12)}px; }}"
                    f"QPushButton:hover {{ border-color: {C['blue']}; color: {C['blue']}; }}")
            r = role
            b.clicked.connect(lambda checked=False, _r=r: self._on_btn(_r))
            btn_row.addWidget(b)
        card_lay.addLayout(btn_row)

        outer.addWidget(card)
        self.setMinimumWidth(340)

    def _on_btn(self, role):
        self._clicked_role = role
        self.accept()

    @classmethod
    def error(cls, parent, title, msg):
        dlg = cls(parent, title, msg, level="error", buttons=(("OK", "primary"),))
        dlg.exec()

    @classmethod
    def info(cls, parent, title, html):
        dlg = cls(parent, title, html, level="info", buttons=(("OK", "primary"),))
        dlg.exec()

    @classmethod
    def warn(cls, parent, title, msg, buttons):
        dlg = cls(parent, title, msg, level="warn", buttons=buttons)
        dlg.exec()
        return dlg._clicked_role


# ═══════════════════════════════════════════════════════════════════════════════
#  UI BUILDER HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _card() -> QFrame:
    f = QFrame()
    f.setObjectName("card")
    f.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    return f


def _lbl(text, obj="", parent=None) -> QLabel:
    l = QLabel(text, parent)
    if obj:
        l.setObjectName(obj)
    return l


def _btn(text, obj, parent=None) -> QPushButton:
    b = QPushButton(text, parent)
    b.setObjectName(obj)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


class _DownCombo(QComboBox):
    """QComboBox that always opens its popup below the widget, never above.
    Caps popup to 4 visible rows — scrollbar appears for longer lists."""
    _MAX_ROWS = 4
    _ROW_HEIGHT_PX = 26

    def showPopup(self):
        super().showPopup()
        popup = self.findChild(QFrame)
        if popup:
            pos = self.mapToGlobal(self.rect().bottomLeft())
            popup.move(pos)
            max_h = self._ROW_HEIGHT_PX * self._MAX_ROWS + 10
            if popup.height() > max_h:
                popup.resize(popup.width(), max_h)
            # Make sure the internal list-view has a scrollbar
            from PySide6.QtWidgets import QAbstractScrollArea
            lv = popup.findChild(QAbstractScrollArea)
            if lv:
                lv.setVerticalScrollBarPolicy(
                    Qt.ScrollBarPolicy.ScrollBarAlwaysOn)


def _combo(items, parent=None) -> QComboBox:
    c = _DownCombo(parent)
    c.addItems(items)
    c.setMaxVisibleItems(_DownCombo._MAX_ROWS)
    return c


class _LogFileManager:
    """Writes activity log to a dated file; rolls to _part2, _part3... past 5 MB."""
    _MAX_BYTES = 5 * 1024 * 1024  # 5 MB

    def __init__(self, base_dir: str):
        self._log_dir = os.path.join(base_dir, "logs")
        self._base_name = datetime.datetime.now().strftime("amc_%Y-%m-%d_%H-%M-%S")
        self._part = 1
        self._file = None
        try:
            os.makedirs(self._log_dir, exist_ok=True)
            self._open()
        except Exception:
            pass

    def _open(self):
        suffix = "" if self._part == 1 else f"_part{self._part}"
        path = os.path.join(self._log_dir, f"{self._base_name}{suffix}.log")
        self._file = open(path, "a", encoding="utf-8")
        self._current_path = path

    def write(self, line: str):
        if self._file is None:
            return
        try:
            self._file.write(line + "\n")
            self._file.flush()
            if os.path.getsize(self._current_path) >= self._MAX_BYTES:
                self._file.close()
                self._part += 1
                self._open()
        except Exception:
            pass

    def current_path(self) -> str:
        return getattr(self, "_current_path", "")

    def close(self):
        try:
            if self._file:
                self._file.flush()
                self._file.close()
                self._file = None
        except Exception:
            pass


def _section_row(title: str, subtitle: str) -> QWidget:
    """Returns a widget: [title / subtitle]."""
    w = QWidget()
    w.setStyleSheet("background: transparent;")
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)
    if subtitle:
        txt = QWidget()
        txt.setStyleSheet("background: transparent;")
        tl = QVBoxLayout(txt)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(1)
        tl.addWidget(_lbl(title, "sec_title"))
        s = _lbl(subtitle, "sec_sub")
        s.setWordWrap(True)
        tl.addWidget(s)
        lay.addWidget(txt, 1)
    else:
        lay.addWidget(_lbl(title, "sec_title"), 1)
    return w


def _field_label(text) -> QLabel:
    l = QLabel(text)
    l.setObjectName("field_lbl")
    return l


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN WINDOW
# ═══════════════════════════════════════════════════════════════════════════════

class AMCMainWindow(QMainWindow):

    _sig_update_status  = Signal(str)
    _sig_update_fault   = Signal(str)
    _sig_update_get     = Signal(int, str)
    _sig_update_write   = Signal(int, str)
    _sig_sync_modes     = Signal(str, str)
    _sig_error          = Signal(str)
    _sig_connect        = Signal(str, str)   # port, baud — triggers _set_connected_ui on main thread
    _sig_disconnect     = Signal()
    _sig_log            = Signal(str, str)
    _sig_cmd_completed  = Signal(object, str)
    _sig_quality        = Signal(str)

    VAR_MAP = {
        "Speed": "speed",   "SpeedMax": "spmax",
        "Acceleration": "accel", "Deceleration": "decel",
        "Isq": "isq",   "Isqmax": "iqmax",  "Ismax": "imax",
        "Isd": "isd",   "Idc": "dccur",     "Idcmax": "idcmx",
        "Usd": "usd",   "Usq": "usq",
        "Dg": "damp",   "Dy": "dyn",
    }
    UNIT_MAP = {
        "Speed": "RPM",   "SpeedMax": "RPM",
        "Acceleration": "RPM/s", "Deceleration": "RPM/s",
        "Isq": "A",  "Isqmax": "A", "Ismax": "A",
        "Isd": "A",  "Idc": "A",   "Idcmax": "A",
        "Usd": "V",  "Usq": "V",
        "Dg": "—",   "Dy": "rad/s",
    }

    # Slider ranges for each reference entry [min, max, default]
    _REF_RANGES = {
        "UsdRef":  (-500,  500,  0),
        "UsqRef":  (-500,  500,  0),
        "IsdRef":  (-100,  100,  0),
        "IsqRef":  (-100,  100,  0),
        "speed":   (-5000, 5000, 0),
        "accel":   (-10000,10000,0),
    }

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Appcon — AMC Interface")

        _base = sys._MEIPASS if getattr(sys, 'frozen', False) else os.path.abspath(".")
        for _icon_name in ("LogoAmcComm2.ico", "Logo-Appcon.ico", "LogoAmcComm2.png"):
            _icon_path = os.path.join(_base, _icon_name)
            if os.path.exists(_icon_path):
                _qicon = QIcon(_icon_path)
                self.setWindowIcon(_qicon)
                QApplication.instance().setWindowIcon(_qicon)
                break

        # Generate arrow PNGs now that QApplication exists, then rebuild QSS
        _make_arrow_png(C["muted"], _ARROW_MUTED)
        _make_arrow_png(C["blue"],  _ARROW_BLUE)
        self.setStyleSheet(_build_qss(C))

        # Logic layer
        self.serial      = SerialManager()
        self.response_q  = queue.Queue()
        self.cmd_manager = QueuedCommandManager(self.serial, self.response_q)

        self.loop_running = self.fault_loop_running = False
        self.get_loop_running = self.status_loop_running = False
        self.cmd_loop_running = False
        self.loop_thread = self.fault_loop_thread = None
        self.get_loop_thread = self.status_loop_thread = None
        self.cmd_loop_thread = None
        self._fault_stop  = threading.Event()
        self._status_stop = threading.Event()
        self._get_stop    = threading.Event()
        self._cmd_stop    = threading.Event()

        self.old_values               = {}
        self.last_valid_get_responses = {i: "N/A" for i in range(3)}
        self.last_valid_status        = "Unknown"
        self.last_valid_fault         = "No Fault"
        self.previous_control_mode    = "Stop"
        self.current_control_mode     = "Stop"
        self._mode_lock_until         = 0.0
        self._flow_widget             = None  # compat

        # Slider ↔ entry sync guard
        self._slider_updating = False
        self.fpwm = 16000.0   # updated from firmware on each connect

        # Cable-drop announcement guard — reset on each fresh connection
        self._cable_drop_announced = False
        self._user_disconnected    = False  # set True on manual disconnect, suppresses auto-connect
        self._has_connected_once   = False  # auto-connect only after user has connected manually first
        self._last_fault_state     = None   # tracks previous fault to suppress duplicate log entries
        self._known_ports          = set()  # tracks port list for new-device detection
        _s = QSettings("Appcon Technologies", "AMC Interface")
        self._last_port = _s.value("last_port", None)

        # Signal → slot
        self._sig_update_status.connect(self._update_status_display)
        self._sig_update_fault.connect(self._update_fault_indicator)
        self._sig_update_get.connect(self._update_get_response)
        self._sig_update_write.connect(self._update_write_entry)
        self._sig_sync_modes.connect(self._sync_mode_radiobuttons)
        self._sig_error.connect(self._on_error_signal)
        self._sig_connect.connect(self._set_connected_ui)
        self._sig_disconnect.connect(self._set_disconnected_ui)
        self._sig_log.connect(self._append_log)
        self._sig_cmd_completed.connect(self._on_cmd_completed)
        self._sig_quality.connect(lambda q: self._sb_quality.setText(q))

        self._build_ui()
        self._set_hand_cursors()
        self.setMinimumSize(900, 560)
        QTimer.singleShot(50, self._fit_to_screen)
        QShortcut(QKeySequence("Ctrl+Shift+D"), self).activated.connect(self._toggle_theme)
        QShortcut(QKeySequence("Ctrl+Shift+M"), self).activated.connect(self._toggle_combined_view)

        # Restore combined view preference from last session
        if _s.value("combined_view", False, type=bool):
            QTimer.singleShot(300, self._enter_combined_view)

        # Port-watch: scans every 2s, auto-connects when remembered port appears
        self._port_watch_timer = QTimer(self)
        self._port_watch_timer.timeout.connect(self._port_watch_tick)
        self._port_watch_timer.start(2000)

        self._queue_timer = QTimer(self)
        self._queue_timer.timeout.connect(self._drain_response_queue)
        self._queue_timer.start(80)

        self._log_file = _LogFileManager(os.path.dirname(os.path.abspath(__file__)))
        self._set_disconnected_ui()
        self._log_signal("SYS", f"AMC Interface v2.1.0 initialized — Appcon Technologies")
        self._log_signal("SYS", "Scanning available serial ports...")
        ports = self._list_serial_ports()
        self._known_ports = set(p.strip().split(" — ")[0].strip() for p in ports)
        if ports:
            self._log_signal("OK", f"Found {len(ports)} port(s): {', '.join(p.split(' — ')[0] for p in ports)}")
        self._log_signal("WARN", "Select a COM port and Baud Rate, then click Connect.")

    # ──────────────────────────────────────────────────────────────────────────
    #  TOP-LEVEL UI CONSTRUCTION
    # ──────────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        central.setObjectName("central")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._build_menu()
        root.addWidget(self._build_header_bar())

        # ── Left column cards ──────────────────────────────────────────────────
        serial_card  = self._build_serial_card()
        status_card  = self._build_status_card()
        modes_card   = self._build_modes_card()
        commands_card = self._build_commands_card()

        top_row = QHBoxLayout()
        top_row.setSpacing(6)
        top_row.addWidget(serial_card, 5)
        status_card.setMinimumWidth(340)
        top_row.addWidget(status_card, 5)

        left_col = QWidget()
        left_col.setObjectName("scroll_inner")
        left_lay = QVBoxLayout(left_col)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(5)
        left_lay.addLayout(top_row)
        left_lay.addWidget(modes_card)
        left_lay.addWidget(commands_card, 1)  # commands card grows to fill space

        # ── Right sidebar — Activity Log (collapsible via splitter) ──────────
        self._log_panel = self._build_log_card()

        left_col.setMinimumWidth(520)
        self._log_panel.setMinimumWidth(300)

        # Inner splitter: interface (left) | log panel (right)
        self._splitter = _GripSplitter(Qt.Orientation.Horizontal)
        self._splitter.setObjectName("scroll_inner")
        self._splitter.setContentsMargins(10, 4, 10, 4)
        self._splitter.setHandleWidth(8)
        self._splitter.addWidget(left_col)
        self._splitter.addWidget(self._log_panel)
        self._splitter.setCollapsible(0, False)
        self._splitter.setCollapsible(1, False)
        self._splitter.setSizes([680, 420])

        # Outer splitter: inner (left) | scope embed pane (right, hidden by default)
        self._outer_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._outer_splitter.setHandleWidth(6)
        self._outer_splitter.addWidget(self._splitter)
        # Placeholder for embedded scope body — filled by _toggle_combined_view
        self._scope_embed_pane = QWidget()
        self._scope_embed_pane.setMinimumWidth(400)
        self._scope_embed_pane.setVisible(False)
        _sep_lay = QVBoxLayout(self._scope_embed_pane)
        _sep_lay.setContentsMargins(0, 0, 0, 0)
        self._outer_splitter.addWidget(self._scope_embed_pane)
        self._outer_splitter.setCollapsible(0, False)
        self._outer_splitter.setCollapsible(1, False)
        self._combined_view_active = False

        root.addWidget(self._outer_splitter, 1)

        # Status bar
        sb = self.statusBar()
        self._sb_label = QLabel("  ● Disconnected")
        self._sb_label.setObjectName("sb_disconnected")
        sb.addWidget(self._sb_label)
        self._sb_quality = QLabel("")
        self._sb_quality.setObjectName("sb_quality")
        self._sb_quality.setToolTip(
            "Connection quality\n● Good (<50 ms)\n◑ Slow (50–200 ms)\n◌ Degraded (>200 ms)")
        sb.addPermanentWidget(self._sb_quality)
        tip = QLabel("Tip: Change entries and press Enter or click Send  ")
        tip.setObjectName("header_sub")
        sb.addPermanentWidget(tip)

    def _fit_to_screen(self):
        w, h = 1100, 660
        self.resize(w, h)
        self._splitter.setSizes([w - 420, 420])
        screen = self.screen().availableGeometry()
        self.move(
            screen.x() + (screen.width() - w) // 2,
            screen.y() + (screen.height() - h) // 2,
        )

    def _port_watch_tick(self):
        """Runs every 2 s. Detects USB plug/unplug and keeps the port combo in sync.
        Never auto-connects — the user must press Connect themselves."""
        current_ports = set(p.strip().split(" — ")[0].strip() for p in self._list_serial_ports())
        new_ports     = current_ports - self._known_ports
        removed_ports = self._known_ports - current_ports

        if new_ports:
            # Refresh combobox so new port appears immediately
            self._refresh_ports()
            self._pulse_refresh_btn()
            for port in sorted(new_ports):
                self._show_toast(f"Device connected: {port}. Press Connect to use it.", "info")
                self._log_signal("SYS", f"USB device appeared: {port}")

        if removed_ports:
            # If we're connected to a port that just vanished, trigger graceful disconnect
            if self.serial.is_open:
                connected_port = getattr(self, '_last_port', None)
                if connected_port and connected_port in removed_ports:
                    self._show_toast(f"Device {connected_port} unplugged. Disconnecting.", "warn")
                    self._log_signal("WARN", f"USB device removed: {connected_port}")
                    QTimer.singleShot(0, self._on_disconnect)
            # Refresh combobox to remove the gone port
            self._refresh_ports()
            for port in sorted(removed_ports):
                self._log_signal("SYS", f"USB device removed: {port}")

        self._known_ports = current_ports

    def _pulse_refresh_btn(self):
        """Flash the refresh button blue→normal 3 times to signal a new port was detected."""
        btn = getattr(self, "_refresh_btn", None)
        if btn is None:
            return
        flash_on  = (f"background: {C['blue']}; color: white; border: 1.5px solid {C['blue_dark']}; "
                     f"border-radius: 6px;")
        flash_off = ""
        for i in range(3):
            QTimer.singleShot(i * 300,       lambda: btn.setStyleSheet(flash_on))
            QTimer.singleShot(i * 300 + 150, lambda: btn.setStyleSheet(flash_off))

    # ── HEADER BAR ────────────────────────────────────────────────────────────

    def _build_header_bar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("header_bar")
        bar.setFixedHeight(_px(64))
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(16, 4, 16, 4)
        lay.setSpacing(12)

        _base = sys._MEIPASS if getattr(sys, 'frozen', False) else os.path.abspath(".")
        logo_path = os.path.join(_base, "LogoAmcComm2.PNG")
        if os.path.exists(logo_path):
            pix = QPixmap(logo_path).scaledToHeight(58, Qt.TransformationMode.SmoothTransformation)
            logo_lbl = QLabel()
            logo_lbl.setPixmap(pix)
            lay.addWidget(logo_lbl)
        else:
            fb = QLabel("APPCON")
            fb.setObjectName("header_logo_text")
            lay.addWidget(fb)

        title_block = QWidget()
        title_block.setStyleSheet("background: transparent;")
        tb_lay = QVBoxLayout(title_block)
        tb_lay.setContentsMargins(0, 0, 0, 0)
        tb_lay.setSpacing(1)
        t = QLabel("AMC Interface")
        t.setObjectName("header_title")
        s = QLabel("Monitor and control your AMC controller")
        s.setObjectName("header_sub")
        tb_lay.addWidget(t)
        tb_lay.addWidget(s)
        lay.addWidget(title_block, 1)

        # Connection status pill (top-right)
        self._conn_pill = QLabel("  ⊘  Disconnected  ●")
        self._conn_pill.setObjectName("conn_status_pill")
        lay.addWidget(self._conn_pill)

        self._log_toggle_btn = QPushButton()
        self._log_toggle_btn.setIcon(qta.icon("fa5s.eye-slash", color=C["muted"]))
        self._log_toggle_btn.setIconSize(QSize(_px(14), _px(14)))
        self._log_toggle_btn.setObjectName("btn_theme")
        self._log_toggle_btn.setFixedSize(_px(28), _px(28))
        self._log_toggle_btn.setToolTip("Hide Activity Log")
        self._log_toggle_btn.clicked.connect(self._toggle_log_panel)
        lay.addWidget(self._log_toggle_btn)

        self._theme_btn = QPushButton()
        self._theme_btn.setIcon(qta.icon("fa5s.moon", color=C["muted"]))
        self._theme_btn.setIconSize(QSize(_px(14), _px(14)))
        self._theme_btn.setObjectName("btn_theme")
        self._theme_btn.setFixedSize(_px(28), _px(28))
        self._theme_btn.setToolTip("Toggle dark / light mode  [Ctrl+Shift+D]")
        self._theme_btn.clicked.connect(self._toggle_theme)
        lay.addWidget(self._theme_btn)

        # Combined view toggle — embeds scope panel beside main interface
        self._combined_btn = QPushButton("⊞  Combined View")
        self._combined_btn.setObjectName("btn_combined_view")
        self._combined_btn.setToolTip(
            "Show interface + oscilloscope side-by-side  [Ctrl+Shift+M]")
        self._combined_btn.setCheckable(True)
        self._combined_btn.setChecked(False)
        self._combined_btn.clicked.connect(self._toggle_combined_view)
        lay.addWidget(self._combined_btn)

        return bar

    # ── SECTION 1: SERIAL CONNECTION ─────────────────────────────────────────

    def _build_serial_card(self) -> QFrame:
        card = _card()
        lay = QVBoxLayout(card)
        lay.setContentsMargins(12, 5, 12, 5)
        lay.setSpacing(5)

        lay.addWidget(_section_row("Serial Connection",
                                   "Configure and manage serial port connection"))

        # Row 1: port combo | baud | refresh | reset
        top_row = QHBoxLayout()
        top_row.setSpacing(8)

        ports = self._list_serial_ports()
        self.port_combobox = _combo(ports)
        self.port_combobox.setObjectName("port_combo")
        self.port_combobox.setPlaceholderText("No device found")
        self.port_combobox.setToolTip("Select AMC controller port")
        self.port_combobox.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        # Dropdown list expands to fit longest item name — never clips
        self.port_combobox.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContentsOnFirstShow)
        self.port_combobox.view().setMinimumWidth(320)
        last = getattr(self, "_last_port", None)
        if last:
            for i, p in enumerate(ports):
                if p.startswith(last):
                    self.port_combobox.setCurrentIndex(i)
                    break
        elif ports:
            self.port_combobox.setCurrentIndex(0)
        top_row.addWidget(self.port_combobox, 2)

        baud_vals = [
            "9600","19200","38400","57600","115200","230400",
            "460800","921600","1000000","1500000","2000000",
        ]
        self.baud_entry = _combo(baud_vals)
        self.baud_entry.setEditable(True)
        self.baud_entry.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.baud_entry.setObjectName("baud_combo")
        self.baud_entry.setCurrentText("1000000")
        self.baud_entry.setToolTip("Baud rate — select or type (up to 2 000 000 bps)")
        self.baud_entry.setFixedWidth(_px(105))
        self.baud_entry.get  = self.baud_entry.currentText
        self.baud_entry.text = self.baud_entry.currentText

        bps_lbl = QLabel("bps")
        bps_lbl.setObjectName("baud_bps_lbl")

        top_row.addWidget(self.baud_entry)
        top_row.addSpacing(6)
        top_row.addWidget(bps_lbl)
        top_row.addSpacing(8)

        self._refresh_btn = _btn("", "btn_outline")
        self._refresh_btn.setIcon(qta.icon("fa5s.sync-alt", color=C["muted"]))
        self._refresh_btn.setIconSize(QSize(_px(14), _px(14)))
        self._refresh_btn.setFixedWidth(_px(34))
        self._refresh_btn.setToolTip("Refresh port list")
        self._refresh_btn.clicked.connect(self._refresh_ports)
        top_row.addWidget(self._refresh_btn)

        self.reset_button = _btn("", "btn_outline")
        self.reset_button.setIcon(qta.icon("fa5s.power-off", color=C["muted"]))
        self.reset_button.setIconSize(QSize(_px(14), _px(14)))
        self.reset_button.setFixedWidth(_px(34))
        self.reset_button.setToolTip("Reset controller")
        self.reset_button.clicked.connect(self._on_reset)
        top_row.addWidget(self.reset_button)

        lay.addLayout(top_row)

        # Row 2: Connect | Disconnect — right-aligned
        bot_row = QHBoxLayout()
        bot_row.setSpacing(6)
        bot_row.addStretch(1)

        self.connect_button = _btn("Connect", "btn_primary")
        self.connect_button.setFixedWidth(_px(110))
        self.connect_button.setIcon(qta.icon("fa5s.plug", color="#FFFFFF"))
        self.connect_button.setIconSize(QSize(_px(13), _px(13)))
        self.connect_button.clicked.connect(self._on_connect)
        bot_row.addWidget(self.connect_button)

        self.disconnect_button = _btn("Disconnect", "btn_danger_outline")
        self.disconnect_button.setFixedWidth(_px(110))
        self.disconnect_button.setIcon(qta.icon("fa5s.unlink", color=C["red"]))
        self.disconnect_button.setIconSize(QSize(_px(13), _px(13)))
        self.disconnect_button.clicked.connect(self._on_disconnect)
        bot_row.addWidget(self.disconnect_button)

        lay.addLayout(bot_row)

        return card

    # ── SECTION 2: CONTROLLER STATUS ─────────────────────────────────────────

    def _build_status_card(self) -> QFrame:
        card = _card()
        self._status_card_frame = card
        lay = QVBoxLayout(card)
        lay.setContentsMargins(12, 5, 12, 5)
        lay.setSpacing(5)

        lay.addWidget(_section_row("Controller Status",
                                   "Real-time controller status and health"))

        # Row 1: status pill only — fault gets its own row below so it can never squish siblings
        pills_row = QHBoxLayout()
        pills_row.setSpacing(8)

        self.status_display = QLabel("✖  Disconnected")
        self.status_display.setObjectName("conn_pill_dis")
        self.status_display.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_display.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        pills_row.addWidget(self.status_display, 1)

        lay.addLayout(pills_row)

        # Row 2: fault label — isolated row so word-wrap never displaces status widgets
        fault_row = QHBoxLayout()
        fault_row.setSpacing(0)
        fault_row.setContentsMargins(0, 0, 0, 0)
        self.fault_label = QLabel("—  Unknown")
        self.fault_label.setObjectName("fault_unknown")
        self.fault_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.fault_label.setWordWrap(True)
        self.fault_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.fault_label.setMinimumHeight(24)
        self.fault_label.setMaximumHeight(44)
        fault_row.addWidget(self.fault_label)
        lay.addLayout(fault_row)

        # Clear Fault button — right-aligned below the pills
        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 0, 0)
        btn_row.addStretch(1)
        self.clear_button = _btn("✕  Clear Fault", "btn_clear_fault")
        self.clear_button.setToolTip("Clear active fault")
        self.clear_button.setEnabled(False)
        self.clear_button.setVisible(False)
        self.clear_button.clicked.connect(self._clear_fault)
        btn_row.addWidget(self.clear_button)
        lay.addLayout(btn_row)

        return card

    # ── SECTION 3: MODES & REFERENCES ────────────────────────────────────────

    def _build_modes_card(self) -> QFrame:
        card = _card()
        outer = QVBoxLayout(card)
        outer.setContentsMargins(14, 8, 14, 10)
        outer.setSpacing(7)

        # Active-mode pill — kept for logic, but not displayed (hidden)
        self._modes_active_pill = QLabel("Stop")
        self._modes_active_pill.setObjectName("modes_active_pill")
        self._modes_active_pill.setVisible(False)

        # ── CONTROL MODE (left) | SENSOR MODE (right) — side by side ─────────
        modes_row = QHBoxLayout()
        modes_row.setSpacing(16)
        modes_row.setContentsMargins(0, 0, 0, 0)

        # Left: Control Mode — wrapped in accent frame (red left border)
        ctrl_frame = QFrame()
        ctrl_frame.setObjectName("mode_group_ctrl")
        ctrl_col = QVBoxLayout(ctrl_frame)
        ctrl_col.setSpacing(4)
        ctrl_col.setContentsMargins(8, 6, 6, 6)
        ctrl_title = QLabel("Control Mode")
        ctrl_title.setObjectName("group_label")
        ctrl_col.addWidget(ctrl_title)
        ctrl_btns_row = QHBoxLayout()
        ctrl_btns_row.setSpacing(4)

        control_options = ["Stop", "Voltage", "Current", "Speed"]
        self._control_btns = []
        self._control_bg = QButtonGroup(self)
        self._control_bg.setExclusive(True)
        self._control_map = {}
        for opt in control_options:
            b = QPushButton(opt)
            b.setObjectName("radio_btn_stop" if opt == "Stop" else "radio_btn_active")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setMinimumHeight(34)
            b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            if opt == "Stop":
                b.setChecked(True)
            self._control_bg.addButton(b)
            self._control_btns.append(b)
            self._control_map[opt] = b
            ctrl_btns_row.addWidget(b, 1)
            b.toggled.connect(lambda checked, o=opt: self._on_control_btn_toggled(checked, o))
        ctrl_col.addLayout(ctrl_btns_row)
        modes_row.addWidget(ctrl_frame, 1)

        # Right: Sensor Mode — wrapped in accent frame (green left border)
        sens_frame = QFrame()
        sens_frame.setObjectName("mode_group_sens")
        sens_col = QVBoxLayout(sens_frame)
        sens_col.setSpacing(4)
        sens_col.setContentsMargins(8, 6, 6, 6)
        sens_title = QLabel("Sensor Mode")
        sens_title.setObjectName("group_label")
        sens_col.addWidget(sens_title)
        sens_btns_row = QHBoxLayout()
        sens_btns_row.setSpacing(4)

        sensor_options = ["FixAngle", "Sensor", "Sensorless_BEMF", "Sensorless_HFI"]
        sensor_labels  = {"FixAngle": "FixAngle", "Sensor": "Sensor",
                          "Sensorless_BEMF": "BEMF", "Sensorless_HFI": "HFI"}
        sensor_tooltips = {
            "FixAngle":        "Fixed angle: open-loop startup, no feedback — use for test only",
            "Sensor":          "Encoder/Hall sensor: closed-loop with physical position sensor",
            "Sensorless_BEMF": "Sensorless BEMF: estimates position from back-EMF — best for medium/high speed",
            "Sensorless_HFI":  "Sensorless HFI: high-frequency injection for position at low/zero speed",
        }
        self._sensor_btns = []
        self._sensor_bg = QButtonGroup(self)
        self._sensor_bg.setExclusive(True)
        self._sensor_map = {}
        for opt in sensor_options:
            b = QPushButton(sensor_labels[opt])
            b.setObjectName("radio_btn")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setMinimumHeight(34)
            b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            b.setToolTip(sensor_tooltips[opt])
            if opt == "Sensorless_BEMF":
                b.setChecked(True)
            self._sensor_bg.addButton(b)
            self._sensor_btns.append(b)
            self._sensor_map[opt] = b
            sens_btns_row.addWidget(b, 1)
            b.toggled.connect(lambda checked, o=opt: self._on_sensor_btn_toggled(checked, o))
        sens_col.addLayout(sens_btns_row)
        modes_row.addWidget(sens_frame, 1)

        outer.addLayout(modes_row)

        self._sensor_combo  = None
        self._control_combo = None

        # ── REFERENCE ENTRIES (no sliders) — 2 columns: label | entry | unit ──
        ref_defs = [
            ("UsdRef",  "UsdRef",  "V",     -500,  500),
            ("UsqRef",  "UsqRef",  "V",     -500,  500),
            ("IsdRef",  "IsdRef",  "A",     -100,  100),
            ("IsqRef",  "IsqRef",  "A",     -100,  100),
            ("SpeedRef","speed",   "RPM",  -5000, 5000),
            ("AccRef",  "accel",   "RPM/s",-10000,10000),
        ]

        self._ref_sliders     = {}
        self._ref_entries_map = {}
        self._ref_row_widgets = {}

        attr_names     = ["usdRef_entry","usqRef_entry","isdRef_entry",
                          "isqRef_entry","speedRef_entry","accRef_entry"]
        entry_map_keys = ["UsdRef","UsqRef","IsdRef","IsqRef","speed","accel"]

        # 3 columns × 2 column-groups, fixed widths so left/right always align
        # Layout per row: [lbl 80px | entry 110px | unit 50px | spacer | lbl 80px | entry 110px | unit 50px]
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        grid.setContentsMargins(4, 4, 4, 4)
        # All columns fixed (stretch 0) — predictable alignment
        for c in range(7):
            grid.setColumnStretch(c, 0)
        grid.setColumnStretch(3, 1)   # only the centre gap stretches
        grid.setColumnMinimumWidth(3, 24)

        for idx, (lbl_txt, var_key, unit_str, rmin, rmax) in enumerate(ref_defs):
            col_off = (idx % 2) * 4   # 0 for left column-group, 4 for right column-group
            grow    = idx // 2

            lbl = QLabel(lbl_txt)
            lbl.setObjectName("field_lbl")
            lbl.setFixedWidth(_px(80))
            lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

            # Dummy slider kept for API compat — never added to layout
            dummy_slider = QSlider(Qt.Orientation.Horizontal)
            dummy_slider.setMinimum(int(rmin))
            dummy_slider.setMaximum(int(rmax))
            dummy_slider.setValue(0)
            dummy_slider.setEnabled(False)
            dummy_slider.setVisible(False)

            entry = QLineEdit("")
            entry.setFixedWidth(_px(110))
            entry.setAlignment(Qt.AlignmentFlag.AlignCenter)
            entry.setPlaceholderText("0.00")
            entry.setEnabled(False)
            entry.returnPressed.connect(
                lambda ek=var_key, e=entry, s=dummy_slider: self._on_ref_entry_return(ek, e, s))
            _apply_mono(entry)

            u_lbl = QLabel(unit_str)
            u_lbl.setObjectName("unit_lbl")
            u_lbl.setFixedWidth(_px(50))
            u_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

            grid.addWidget(lbl,   grow, col_off,     Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(entry, grow, col_off + 1, Qt.AlignmentFlag.AlignLeft  | Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(u_lbl, grow, col_off + 2, Qt.AlignmentFlag.AlignLeft  | Qt.AlignmentFlag.AlignVCenter)

            self._ref_sliders[var_key]     = dummy_slider
            self._ref_entries_map[var_key] = entry
            setattr(self, attr_names[idx], entry)

        # Wrap grid in HBox so it doesn't span the full card width
        grid_wrap = QHBoxLayout()
        grid_wrap.setContentsMargins(0, 0, 0, 0)
        grid_wrap.addLayout(grid)
        grid_wrap.addStretch(0)
        outer.addLayout(grid_wrap)

        self.entry_map = {}
        for attr, key in zip(attr_names, entry_map_keys):
            e = getattr(self, attr)
            self.entry_map[e] = key
            self.old_values[e] = ""

        return card

    # ── SECTION 4: COMMANDS ───────────────────────────────────────────────────

    def _build_commands_card(self) -> QFrame:
        card = _card()
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        outer = QVBoxLayout(card)
        outer.setContentsMargins(14, 8, 14, 10)
        outer.setSpacing(6)

        outer.addWidget(_section_row("Commands",
                                     "Set a value, send it, read the live response"))

        set_defaults = ["Speed", "Usq", "Isq"]
        get_defaults = ["Speed", "Usq", "Isq"]
        self._get_var_cache    = list(get_defaults)
        self.get_var_comboboxes   = []
        self.set_var_comboboxes   = []
        self.set_value_entries    = []
        self.get_response_entries = []
        self._get_unit_labels     = []
        self._set_unit_labels     = []
        self._send_row_btns       = []
        self._arrow_lbls          = []
        self._flow_dot_lbls       = []
        self._read_eye_badges     = []   # kept for compat, unused
        self._live_indicator      = None

        # ── Two-zone layout: WRITE (left) | divider | READ (right) ──────────
        # Each zone has its own header row + 3 data rows, aligned by column.
        # A vertical rule between zones gives clear spatial separation.

        zones_lay = QHBoxLayout()
        zones_lay.setSpacing(0)
        zones_lay.setContentsMargins(0, 0, 0, 0)

        # ── WRITE ZONE ───────────────────────────────────────────────────────
        write_zone = QVBoxLayout()
        write_zone.setSpacing(2)
        write_zone.setContentsMargins(0, 0, 0, 0)

        write_hdr = QHBoxLayout()
        write_hdr.setSpacing(0)
        write_hdr.setContentsMargins(0, 0, 0, 4)
        wh_title = QLabel("WRITE")
        wh_title.setObjectName("cmd_zone_hdr_write")
        wh_var   = QLabel("Variable")
        wh_var.setFixedWidth(_px(130))
        wh_var.setObjectName("cmd_col_hdr")
        wh_val   = QLabel("Value")
        wh_val.setFixedWidth(_px(74))
        wh_val.setAlignment(Qt.AlignmentFlag.AlignCenter)
        wh_val.setObjectName("cmd_col_hdr")
        wh_unit  = QLabel("Unit")
        wh_unit.setFixedWidth(_px(38))
        wh_unit.setObjectName("cmd_col_hdr")
        wh_send  = QLabel("")
        wh_send.setFixedWidth(_px(62))
        write_hdr.addWidget(wh_title)
        write_hdr.addSpacing(_px(10))
        write_hdr.addWidget(wh_var)
        write_hdr.addSpacing(_px(8))
        write_hdr.addWidget(wh_val)
        write_hdr.addSpacing(_px(8))
        write_hdr.addWidget(wh_unit)
        write_hdr.addSpacing(_px(8))
        write_hdr.addWidget(wh_send)
        write_zone.addLayout(write_hdr)

        wz_sep = QFrame()
        wz_sep.setFrameShape(QFrame.Shape.HLine)
        wz_sep.setObjectName("cmd_write_sep")
        write_zone.addWidget(wz_sep)
        write_zone.addSpacing(2)

        for i in range(3):
            row_w = QHBoxLayout()
            row_w.setSpacing(8)
            row_w.setContentsMargins(0, 3, 0, 3)

            cb_var = _combo(list(self.VAR_MAP.keys()))
            cb_var.setObjectName("var_combo")
            cb_var.setCurrentText(set_defaults[i])
            cb_var.setFixedWidth(_px(130))
            cb_var.setToolTip("Variable to write")
            cb_var.setCursor(Qt.CursorShape.PointingHandCursor)
            cb_var.currentTextChanged.connect(lambda txt, idx=i: self._on_set_var_changed(idx))
            row_w.addWidget(cb_var)
            self.set_var_comboboxes.append(cb_var)

            val_e = QLineEdit("0")
            val_e.setMinimumWidth(_px(80))
            val_e.setAlignment(Qt.AlignmentFlag.AlignCenter)
            val_e.setPlaceholderText("value")
            val_e.returnPressed.connect(lambda idx=i: self._send_set_command(idx))
            _apply_mono(val_e)
            row_w.addWidget(val_e)
            self.set_value_entries.append(val_e)

            set_unit_lbl = _lbl(self.UNIT_MAP.get(set_defaults[i], ""), "unit_lbl")
            set_unit_lbl.setFixedWidth(_px(38))
            row_w.addWidget(set_unit_lbl)
            self._set_unit_labels.append(set_unit_lbl)

            send_btn = _btn("Send", "send_row_btn")
            send_btn.setFixedWidth(_px(62))
            send_btn.clicked.connect(lambda checked=False, idx=i: self._send_set_command(idx))
            row_w.addWidget(send_btn)
            self._send_row_btns.append(send_btn)

            self._flow_dot_lbls.append(None)

            write_zone.addLayout(row_w)
            if i < 2:
                rs = QFrame()
                rs.setFrameShape(QFrame.Shape.HLine)
                rs.setObjectName("cmd_row_sep")
                write_zone.addWidget(rs)

        zones_lay.addLayout(write_zone, 3)

        # ── VERTICAL DIVIDER ─────────────────────────────────────────────────
        v_div = QFrame()
        v_div.setFrameShape(QFrame.Shape.VLine)
        v_div.setFixedWidth(1)
        v_div.setObjectName("v_sep")
        zones_lay.addSpacing(_px(14))
        zones_lay.addWidget(v_div)
        zones_lay.addSpacing(_px(14))

        # ── READ ZONE ────────────────────────────────────────────────────────
        read_zone = QVBoxLayout()
        read_zone.setSpacing(2)
        read_zone.setContentsMargins(0, 0, 0, 0)

        read_hdr = QHBoxLayout()
        read_hdr.setSpacing(0)
        read_hdr.setContentsMargins(0, 0, 0, 4)
        rh_title = QLabel("READ")
        rh_title.setObjectName("cmd_zone_hdr_read")
        rh_var   = QLabel("Variable")
        rh_var.setFixedWidth(_px(120))
        rh_var.setObjectName("cmd_col_hdr")
        rh_resp  = QLabel("Value")
        rh_resp.setFixedWidth(_px(90))
        rh_resp.setAlignment(Qt.AlignmentFlag.AlignCenter)
        rh_resp.setObjectName("cmd_col_hdr")
        rh_unit  = QLabel("Unit")
        rh_unit.setFixedWidth(_px(38))
        rh_unit.setObjectName("cmd_col_hdr")
        read_hdr.addWidget(rh_title)
        read_hdr.addSpacing(_px(10))
        read_hdr.addWidget(rh_var)
        read_hdr.addSpacing(_px(8))
        read_hdr.addWidget(rh_resp)
        read_hdr.addSpacing(_px(8))
        read_hdr.addWidget(rh_unit)
        read_hdr.addStretch()
        read_zone.addLayout(read_hdr)

        rz_sep = QFrame()
        rz_sep.setFrameShape(QFrame.Shape.HLine)
        rz_sep.setObjectName("cmd_read_sep")
        read_zone.addWidget(rz_sep)
        read_zone.addSpacing(2)

        for i in range(3):
            row_r = QHBoxLayout()
            row_r.setSpacing(8)
            row_r.setContentsMargins(0, 3, 0, 3)

            cb_get = _combo(list(self.VAR_MAP.keys()))
            cb_get.setObjectName("var_combo")
            cb_get.setCurrentText(get_defaults[i])
            cb_get.setFixedWidth(_px(120))
            cb_get.setToolTip("Variable to read (polled automatically)")
            cb_get.setCursor(Qt.CursorShape.PointingHandCursor)
            cb_get.currentTextChanged.connect(lambda txt, idx=i: self._on_get_var_changed(idx))
            row_r.addWidget(cb_get)
            self.get_var_comboboxes.append(cb_get)

            resp_e = QLineEdit()
            resp_e.setReadOnly(True)
            resp_e.setFixedWidth(_px(90))
            resp_e.setAlignment(Qt.AlignmentFlag.AlignCenter)
            resp_e.setObjectName("read_resp_entry")
            resp_e.setPlaceholderText("—")
            row_r.addWidget(resp_e)
            self.get_response_entries.append(resp_e)

            get_unit_lbl = _lbl(self.UNIT_MAP.get(get_defaults[i], ""), "unit_lbl")
            get_unit_lbl.setFixedWidth(_px(38))
            row_r.addWidget(get_unit_lbl)
            self._get_unit_labels.append(get_unit_lbl)

            row_r.addStretch()
            read_zone.addLayout(row_r)
            if i < 2:
                rs = QFrame()
                rs.setFrameShape(QFrame.Shape.HLine)
                rs.setObjectName("cmd_row_sep")
                read_zone.addWidget(rs)

        zones_lay.addLayout(read_zone, 2)
        outer.addLayout(zones_lay)

        # Disconnected notice — collapses to zero height when hidden (no layout gap)
        self._cmd_notice = QLabel("Connect to a device to send commands")
        self._cmd_notice.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cmd_notice.setObjectName("cmd_notice")
        self._cmd_notice.setMaximumHeight(28)
        outer.addWidget(self._cmd_notice)

        # compat — used elsewhere to dim/restore
        self._write_header = None
        self._send_btn = self._send_row_btns[0] if self._send_row_btns else None

        return card

    # ── SECTION 5: ACTIVITY LOG ───────────────────────────────────────────────

    def _build_log_card(self) -> QFrame:
        card = _card()
        card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        card.setMinimumHeight(200)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(12, 5, 12, 5)
        lay.setSpacing(4)

        hdr_row = QHBoxLayout()
        hdr_row.setSpacing(4)
        act_lbl = _lbl("Activity Log", "sec_title")
        hdr_row.addWidget(act_lbl, 1)
        clr = _btn("", "btn_outline")
        clr.setIcon(qta.icon("fa5s.trash-alt", color=C["muted"]))
        clr.setIconSize(QSize(_px(13), _px(13)))
        clr.setFixedWidth(_px(30))
        clr.setToolTip("Clear log")
        clr.clicked.connect(self._clear_log)
        hdr_row.addWidget(clr)
        exp = _btn("", "btn_outline")
        exp.setIcon(qta.icon("fa5s.file-export", color=C["muted"]))
        exp.setIconSize(QSize(_px(13), _px(13)))
        exp.setFixedWidth(_px(30))
        exp.setToolTip("Export log")
        exp.clicked.connect(self._export_log)
        hdr_row.addWidget(exp)
        lay.addLayout(hdr_row)

        self._log_text = QTextEdit()
        self._log_text.setObjectName("log_area")
        self._log_text.setReadOnly(True)
        self._log_text.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._log_text.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        lay.addWidget(self._log_text, 1)   # stretch=1 fills card interior

        return card

    def _set_hand_cursors(self):
        """Walk all QPushButton and QComboBox descendants and ensure hand cursor."""
        for w in self.findChildren(QPushButton):
            w.setCursor(Qt.CursorShape.PointingHandCursor)
        for w in self.findChildren(QComboBox):
            w.setCursor(Qt.CursorShape.PointingHandCursor)

    # ── MENU ──────────────────────────────────────────────────────────────────

    def _build_menu(self):
        mb = self.menuBar()

        file_menu = mb.addMenu("File")
        if SaveParameters:
            a = QAction("Save Parameters", self)
            a.setIcon(qta.icon("fa5s.save", color=C["text2"]))
            a.triggered.connect(self.open_save_params)
            file_menu.addAction(a)
        else:
            a = QAction("Save Parameters", self)
            a.setIcon(qta.icon("fa5s.save", color=C["text2"]))
            a.triggered.connect(lambda: _ModernModal.error(self, "Module not found", "save_params_qt.py not found."))
            file_menu.addAction(a)
        if LoadParameters:
            a = QAction("Load Parameters", self)
            a.setIcon(qta.icon("fa5s.folder-open", color=C["text2"]))
            a.triggered.connect(self.open_load_params)
            file_menu.addAction(a)
        else:
            a = QAction("Load Parameters", self)
            a.setIcon(qta.icon("fa5s.folder-open", color=C["text2"]))
            a.triggered.connect(lambda: _ModernModal.error(self, "Module not found", "load_params_qt.py not found."))
            file_menu.addAction(a)

        ident_menu = mb.addMenu("Identification")
        if ElectricalParametersIdentification:
            a = QAction("Electrical Parameters", self)
            a.setIcon(qta.icon("fa5s.bolt", color=C["text2"]))
            a.triggered.connect(self.open_electrical_params)
            ident_menu.addAction(a)
        else:
            a = QAction("Electrical Parameters", self)
            a.setIcon(qta.icon("fa5s.bolt", color=C["text2"]))
            a.triggered.connect(lambda: _ModernModal.error(self, "Module not found", "electrical_params_qt.py not found."))
            ident_menu.addAction(a)
        if InertiaIdentification:
            a = QAction("Mechanical Parameters", self)
            a.setIcon(qta.icon("fa5s.cog", color=C["text2"]))
            a.triggered.connect(self.open_mechanical_params)
            ident_menu.addAction(a)
        else:
            a = QAction("Mechanical Parameters", self)
            a.setIcon(qta.icon("fa5s.cog", color=C["text2"]))
            a.triggered.connect(lambda: _ModernModal.error(self, "Module not found", "inertia_param_qt.py not found."))
            ident_menu.addAction(a)

        term_menu = mb.addMenu("Terminal")
        if Terminal:
            a = QAction("Open Terminal", self)
            a.setIcon(qta.icon("fa5s.terminal", color=C["text2"]))
            a.triggered.connect(self.open_terminal)
            term_menu.addAction(a)
        else:
            a = QAction("Open Terminal", self)
            a.setIcon(qta.icon("fa5s.terminal", color=C["text2"]))
            a.triggered.connect(lambda: _ModernModal.error(self, "Module not found", "terminal_qt.py not found."))
            term_menu.addAction(a)

        monitor_menu = mb.addMenu("Monitoring")
        if ScopeWindow:
            a = QAction("Oscilloscope", self)
            a.setIcon(qta.icon("fa5s.chart-line", color=C["text2"]))
            a.triggered.connect(self.open_monitoring)
            monitor_menu.addAction(a)
        else:
            a = QAction("Oscilloscope", self)
            a.setIcon(qta.icon("fa5s.chart-line", color=C["text2"]))
            a.triggered.connect(lambda: _ModernModal.error(self, "Module not found", "scope_qt.py not found."))
            monitor_menu.addAction(a)

        help_menu = mb.addMenu("Help")
        a_info = QAction("Info", self)
        a_info.setIcon(qta.icon("fa5s.info-circle", color=C["text2"]))
        a_info.triggered.connect(self.open_info)
        help_menu.addAction(a_info)

    # ──────────────────────────────────────────────────────────────────────────
    #  UI STATE
    # ──────────────────────────────────────────────────────────────────────────

    def _set_connected_ui(self, port, baud):
        self.connect_button.setEnabled(False)
        self.connect_button.setText("Connected")
        self.connect_button.setFixedWidth(_px(120))
        self.disconnect_button.setEnabled(True)
        self.reset_button.setEnabled(True)

        self._sb_label.setText(f"  ● Connected to {port} @ {baud} bps")
        self._sb_label.setObjectName("sb_connected")
        self._sb_label.style().unpolish(self._sb_label)
        self._sb_label.style().polish(self._sb_label)

        self._conn_pill.setText(f"  ✓  {port}  ●")
        self._conn_pill.setObjectName("conn_status_pill_ok")
        self._conn_pill.setStyle(self._conn_pill.style())

        self.status_display.setText("✔  Connected")
        self.status_display.setObjectName("conn_pill_ok")
        self.status_display.setStyle(self.status_display.style())

        self._cable_drop_announced = False
        self._user_disconnected    = False
        self._has_connected_once   = True
        self._last_port = port
        QSettings("Appcon Technologies", "AMC Interface").setValue("last_port", port)
        self._log_signal("INFO", f"Connected to {port} @ {baud} bps")
        self._apply_disconnected_dim(False)
        self._connected_at     = time.time()
        self._last_fault_state = None  # reset so first poll on this connection always logs
        self._set_mode_ui(self.current_control_mode)
        try:
            self._update_active_mode_pill(self.current_control_mode)
        except Exception:
            pass
        self._start_fault_loop()
        self._start_get_loop()
        self._start_status_loop()
        self._start_cmd_loop()

    def _set_disconnected_ui(self):
        # Loops are stopped by _on_disconnect before this is called; stop again
        # here only as a safety net (e.g. direct calls from tests or cable-drop path)
        self._stop_fault_loop()
        self._stop_get_loop()
        self._stop_status_loop()
        self._stop_cmd_loop()
        self.connect_button.setEnabled(True)
        self.connect_button.setText("Connect")
        self.connect_button.setFixedWidth(_px(110))
        self.disconnect_button.setEnabled(False)
        self.reset_button.setEnabled(False)

        self._sb_label.setText("  ● Disconnected")
        self._sb_label.setObjectName("sb_disconnected")
        self._sb_label.style().unpolish(self._sb_label)
        self._sb_label.style().polish(self._sb_label)
        self._sb_quality.setText("")

        self._conn_pill.setText("  ⊘  Disconnected  ●")
        self._conn_pill.setObjectName("conn_status_pill")
        self._conn_pill.setStyle(self._conn_pill.style())

        self.status_display.setText("✖  Disconnected")
        self.status_display.setObjectName("conn_pill_dis")
        self.status_display.setStyle(self.status_display.style())

        self.fault_label.setText("—  Unknown")
        self.fault_label.setObjectName("fault_unknown")
        self.fault_label.setStyle(self.fault_label.style())

        # If the port is no longer present in the system, clear the combo selection
        # so the user sees they are fully disconnected (not just "paused")
        try:
            current_text = self.port_combobox.currentText().strip().split(" — ")[0].strip()
            available = set(p.strip().split(" — ")[0].strip() for p in self._list_serial_ports())
            if current_text and current_text not in available:
                self.port_combobox.setCurrentIndex(-1)
        except Exception:
            pass

        self._status_card_frame.setStyleSheet("")
        self.clear_button.setEnabled(False)
        self.clear_button.setVisible(False)

        try:
            self._update_active_mode_pill("Stop")
        except Exception:
            pass

        for e in self.entry_map:
            e.setEnabled(False)
            e.setText("0.00")
            self.old_values[e] = ""
        for s in self._ref_sliders.values():
            s.setEnabled(False)
            s.setValue(0)
        for ent in self.get_response_entries:
            ent.setText("—")
        self._apply_disconnected_dim(True)

        self._stop_fault_loop()
        self._stop_get_loop()
        self._stop_status_loop()
        self._stop_cmd_loop()

        self.last_valid_get_responses = {i: "N/A" for i in range(3)}
        self.last_valid_status        = "Unknown"
        self.last_valid_fault         = "No Fault"
        self.previous_control_mode    = "Stop"
        self.current_control_mode     = "Stop"
        if hasattr(self, '_control_map') and self._control_map:
            btn = self._control_map.get("Stop")
            if btn:
                btn.blockSignals(True)
                btn.setChecked(True)
                btn.blockSignals(False)

    def _get_enabled_vars(self, control_mode):
        if control_mode == "Voltage": return ["UsdRef", "UsqRef"]
        if control_mode == "Current": return ["IsdRef", "IsqRef"]
        if control_mode == "Speed":   return ["speed", "accel"]
        return []

    def _set_mode_ui(self, control_mode):
        name_to_entry = {v: k for k, v in self.entry_map.items()}
        enabled_vars  = self._get_enabled_vars(control_mode)
        mode_changed  = (control_mode != self.previous_control_mode)

        # Only clear previous mode entries when the mode actually changes,
        # not on every poll-driven sync call
        if mode_changed:
            prev_enabled_vars = self._get_enabled_vars(self.previous_control_mode)
            for var in prev_enabled_vars:
                e = name_to_entry.get(var)
                if e and not e.hasFocus():
                    self.old_values[e] = ""
                    e.clear()

        # Lock all, then unlock only current mode's entries
        for e in self.entry_map:
            e.setReadOnly(True)
            e.setEnabled(True)   # stay enabled so clicks don't fall through
        for s in self._ref_sliders.values():
            s.setEnabled(False)

        for var in enabled_vars:
            e = name_to_entry.get(var)
            if e:
                e.setReadOnly(False)
                if not e.hasFocus():
                    e.setText(self.old_values.get(e, ""))
            s = self._ref_sliders.get(var)
            if s:
                s.setEnabled(True)

        # Visual lock styling — use QSS property so it adapts to theme
        all_vars = ["UsdRef","UsqRef","IsdRef","IsqRef","speed","accel"]
        for var in all_vars:
            entry  = self._ref_entries_map.get(var)
            locked = var not in enabled_vars
            if entry:
                entry.setProperty("locked", locked)
                entry.style().unpolish(entry)
                entry.style().polish(entry)

    def _apply_disconnected_dim(self, dim: bool):
        """Grey out all interactive controls when disconnected; restore when connected."""
        for s in self._ref_sliders.values():
            s.setEnabled(not dim)

        for e in self.entry_map:
            e.setEnabled(not dim)

        for e in self.set_value_entries:
            e.setEnabled(not dim)

        for b in getattr(self, '_send_row_btns', []):
            b.setEnabled(not dim)
        for b in getattr(self, '_control_btns', []):
            b.setEnabled(not dim)
        for b in getattr(self, '_sensor_btns', []):
            b.setEnabled(not dim)

        if hasattr(self, '_cmd_notice'):
            self._cmd_notice.setMaximumHeight(28 if dim else 0)


    def _update_active_mode_pill(self, mode: str):
        """Updates the prominent 'current mode' pill in the Modes card header."""
        if not hasattr(self, "_modes_active_pill") or self._modes_active_pill is None:
            return
        self._modes_active_pill.setText(mode.upper())
        if mode == "Stop":
            self._modes_active_pill.setProperty("mode", "Stop")
        else:
            self._modes_active_pill.setProperty("mode", "active")
        # repolish so QSS attribute selector takes effect
        self._modes_active_pill.style().unpolish(self._modes_active_pill)
        self._modes_active_pill.style().polish(self._modes_active_pill)

        # Stop button: red only when motor is NOT stopped (acts as emergency stop)
        stop_btn = getattr(self, '_control_map', {}).get("Stop")
        if stop_btn:
            name = "radio_btn_stop" if mode == "Stop" else "radio_btn_stop_active"
            stop_btn.setObjectName(name)
            stop_btn.style().unpolish(stop_btn)
            stop_btn.style().polish(stop_btn)

    def _toggle_log_panel(self):
        visible = self._log_panel.isVisible()
        if visible:
            self._log_splitter_sizes = self._splitter.sizes()
            self._log_panel.setVisible(False)
            self._log_toggle_btn.setIcon(qta.icon("fa5s.eye", color=C["blue"]))
            self._log_toggle_btn.setToolTip("Show Activity Log")
        else:
            self._log_panel.setVisible(True)
            saved = getattr(self, "_log_splitter_sizes", None)
            if saved and len(saved) == 2 and saved[1] > 20:
                self._splitter.setSizes(saved)
            else:
                total = self._splitter.width()
                log_w = max(420, total // 3)
                self._splitter.setSizes([total - log_w, log_w])
            self._log_toggle_btn.setIcon(qta.icon("fa5s.eye-slash", color=C["muted"]))
            self._log_toggle_btn.setToolTip("Hide Activity Log")

    def _toggle_theme(self):
        global C, _THEME
        if _THEME == "light":
            C = dict(C_DARK)
            _THEME = "dark"
        else:
            C = dict(C_LIGHT)
            _THEME = "light"
        icon_name = "fa5s.sun" if _THEME == "light" else "fa5s.moon"
        self._theme_btn.setIcon(qta.icon(icon_name, color=C["muted"]))
        self._theme_btn.setToolTip(
            "Switch to dark mode" if _THEME == "light" else "Switch to light mode")
        _make_arrow_png(C["muted"], _ARROW_MUTED)
        _make_arrow_png(C["blue"],  _ARROW_BLUE)
        self.setStyleSheet(_build_qss(C))
        # Re-apply palette to any open sub-dialogs (electrical, inertia, etc.)
        for child in self.findChildren(QDialog):
            if hasattr(child, '_apply_style'):
                child._apply_style()
        # Explicitly update non-modal scope window (may not be in findChildren tree)
        scope = getattr(self, '_scope_window', None)
        if scope is not None:
            try:
                scope._apply_style()
            except RuntimeError:
                self._scope_window = None

    # ── Combined view ────────────────────────────────────────────────────────

    def _toggle_combined_view(self):
        """Toggle split-screen: embed scope body next to interface or release it."""
        if self._combined_view_active:
            self._exit_combined_view()
        else:
            self._enter_combined_view()

    def _enter_combined_view(self):
        # Ensure scope window exists
        if not ScopeWindow:
            self._show_toast("Scope module not available.", "error")
            self._combined_btn.setChecked(False)
            return
        if not hasattr(self, '_scope_window') or self._scope_window is None:
            dlg = ScopeWindow(self, self.serial, fpwm=self.fpwm)
            dlg.setWindowModality(Qt.WindowModality.NonModal)
            dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
            dlg.finished.connect(lambda: setattr(self, '_scope_window', None))
            self._scope_window = dlg

        scope = self._scope_window
        body = scope.detach_body()

        # Clear placeholder layout and insert scope body
        pane_lay = self._scope_embed_pane.layout()
        while pane_lay.count():
            pane_lay.takeAt(0)
        pane_lay.addWidget(body)
        body.show()

        # Hide activity log to free space for the scope panel
        self._combined_prev_log_visible = self._log_panel.isVisible()
        self._log_panel.setVisible(False)

        self._scope_embed_pane.setVisible(True)
        total = self.width()
        # Give main interface 40%, scope 60% (log is hidden so more room)
        self._outer_splitter.setSizes([total * 4 // 10, total * 6 // 10])

        self._combined_view_active = True
        self._combined_btn.setText("⊟  Separate View")
        self._combined_btn.setToolTip("Switch back to separate scope window  [Ctrl+Shift+M]")
        QSettings("Appcon Technologies", "AMC Interface").setValue("combined_view", True)
        self._show_toast("Combined view. Drag the divider to resize.", "info")

    def _exit_combined_view(self):
        scope = getattr(self, '_scope_window', None)
        if scope is None:
            self._scope_embed_pane.setVisible(False)
            self._combined_view_active = False
            self._combined_btn.setText("⊞  Combined View")
            self._combined_btn.setChecked(False)
            return

        # Remove body from embed pane
        pane_lay = self._scope_embed_pane.layout()
        while pane_lay.count():
            pane_lay.takeAt(0)
        self._scope_embed_pane.setVisible(False)

        # Reattach body to standalone dialog
        scope.attach_body()

        # Restore log panel to its previous state
        if getattr(self, "_combined_prev_log_visible", True):
            self._log_panel.setVisible(True)

        self._combined_view_active = False
        self._combined_btn.setText("⊞  Combined View")
        self._combined_btn.setToolTip("Show interface + oscilloscope side-by-side  [Ctrl+Shift+M]")
        self._combined_btn.setChecked(False)
        QSettings("Appcon Technologies", "AMC Interface").setValue("combined_view", False)
        self._show_toast("Scope window detached.", "info")

    def _update_control_combo_style(self, mode):
        pass  # replaced by radio button checked states

    _SENSOR_CMD = {
        "FixAngle":        "s sens 1",
        "Sensor":          "s sens 2",
        "Sensorless_BEMF": "s sens 3",
        "Sensorless_HFI":  "s sens 4",
    }

    def _on_sensor_btn_toggled(self, checked: bool, option: str):
        if not checked:
            return
        self._mode_lock_until = time.time() + 2.0
        if not self.serial.is_open:
            return
        cmd_str = self._SENSOR_CMD.get(option)
        if not cmd_str:
            return
        try:
            self.serial.send(cmd_str, expect_response=True)
            self._log_signal("INFO", f"Sensor mode → {option}")
        except Exception as e:
            logging.exception("Error setting sensor mode")
            self.response_q.put(("error", f"Communication error: {e}"))
            self.response_q.put(("disconnect", None, None))

    def _on_control_btn_toggled(self, checked: bool, option: str):
        if not checked:
            return
        # Optimistic UI: update status pill + active-mode pill immediately
        # (real status will be confirmed by next poll cycle)
        status_map = {"Stop": "MODE_OFF", "Voltage": "MODE_VOLTAGE",
                      "Current": "MODE_CURRENT", "Speed": "MODE_SPEED"}
        try:
            self._update_status_display(status_map.get(option, "Unknown"))
            self._update_active_mode_pill(option)
        except Exception:
            pass
        # Command format matches reference: s stop / s contr 1 / s contr 2 / s contr 3
        mode_cmd = {"Stop": "s stop", "Voltage": "s contr 1",
                    "Current": "s contr 2", "Speed": "s contr 3"}
        self._mode_lock_until = time.time() + 2.0
        self.previous_control_mode = self.current_control_mode
        self.current_control_mode  = option
        self._set_mode_ui(option)
        if self.serial.is_open:
            cmd_str = mode_cmd.get(option, "s stop")
            def worker():
                try:
                    self.serial.send(cmd_str, expect_response=True)
                    self._log_signal("INFO", f"Control mode → {option}")
                except Exception as e:
                    self.response_q.put(("error", f"Communication error: {e}"))
                    self.response_q.put(("disconnect", None, None))
            threading.Thread(target=worker, daemon=True).start()

    def _update_status_display(self, value):
        # Status pill only shows connection state — mode is shown in Modes card pill
        pass

    def _update_fault_indicator(self, value):
        cleaned = ''.join(c for c in value if c.isalnum())
        fault_map = {
            "i": "SW_OVER_CURRENT", "v": "SW_OVER_VOLTAGE",
            "I": "HW_FAULT",        "b": "ERROR_BREAK",
            "o": "CURRENT_OFFSET",  "t": "MOS_OVER_TEMP",
        }
        if not cleaned:
            self._set_fault_ok()
            return
        faults = [fault_map[c] for c in cleaned if c in fault_map]
        if not faults:
            self._set_fault_ok()
            return
        fault_text = " & ".join(faults)
        self.fault_label.setStyleSheet("")
        self.last_valid_fault = fault_text
        self.fault_label.setText(f"✖  {fault_text}")
        self.fault_label.setObjectName("fault_err")
        self.fault_label.setStyle(self.fault_label.style())
        self.clear_button.setVisible(True)
        self.clear_button.setEnabled(True)
        if self._last_fault_state != fault_text:
            self._last_fault_state = fault_text
            self._log_signal("ERR", f"Fault: {fault_text}")

    def _set_fault_ok(self):
        self.last_valid_fault = "No Fault"
        self.fault_label.setText("✔  No Fault")
        self.fault_label.setObjectName("fault_ok")
        self.fault_label.setStyleSheet("")
        self.fault_label.setStyle(self.fault_label.style())
        self.clear_button.setEnabled(False)
        self.clear_button.setVisible(False)
        if self._last_fault_state not in (None, "No Fault"):
            self._log_signal("OK", "Fault cleared")
        self._last_fault_state = "No Fault"

    def _sync_mode_radiobuttons(self, contr_text: str, sens_text: str):
        if time.time() < self._mode_lock_until:
            return
        rb_map = {"MODE_OFF": "Stop", "MODE_VOLTAGE": "Voltage",
                  "MODE_CURRENT": "Current", "MODE_SPEED": "Speed"}
        rb = rb_map.get(contr_text)
        if rb:
            btn = self._control_map.get(rb)
            if btn and not btn.isChecked():
                btn.blockSignals(True)
                btn.setChecked(True)
                btn.blockSignals(False)
            # Only update mode UI when the firmware reports a mode change
            if rb != self.current_control_mode:
                self.previous_control_mode = self.current_control_mode
                self.current_control_mode  = rb
                self._set_mode_ui(rb)
                self._update_active_mode_pill(rb)
        if sens_text:
            btn = self._sensor_map.get(sens_text)
            if btn and not btn.isChecked():
                btn.blockSignals(True)
                btn.setChecked(True)
                btn.blockSignals(False)

    def _update_get_response(self, idx: int, value: str):
        try:
            var = self.get_var_comboboxes[idx].currentText()
            display = self._scale_get(var, value)
            entry = self.get_response_entries[idx]
            if not display or display.strip() in ("", "N/A", "—") or display.startswith("Err"):
                return
            entry.setText(display)
            self.last_valid_get_responses[idx] = display
            self._flash_read_entry(entry, idx)
        except Exception:
            pass

    def _update_write_entry(self, idx: int, confirmed_value: str):
        try:
            entry = self.set_value_entries[idx]
            entry.setText(confirmed_value)
            prev = entry.styleSheet()
            entry.setStyleSheet(f"background: {C['green_bg']}; color: {C['text']};")
            QTimer.singleShot(400, lambda e=entry, s=prev: e.setStyleSheet(s))
        except Exception:
            pass

    def _flash_read_entry(self, entry: QLineEdit, idx: int = -1):
        # Border color-only flash (same 1px width → zero reflow). Light green = new value arrived.
        entry.setStyleSheet(
            f"QLineEdit#read_resp_entry {{ border: 1px solid {C['green_border']}; border-left: 3px solid {C['green']}; color: {C['text']}; background: {C['input_bg']}; }}")
        QTimer.singleShot(350, lambda e=entry: e.setStyleSheet(""))

    # ──────────────────────────────────────────────────────────────────────────
    #  QUEUE DRAIN
    # ──────────────────────────────────────────────────────────────────────────

    def _drain_response_queue(self):
        try:
            while True:
                item = self.response_q.get_nowait()
                tag = item[0]
                if   tag == "update_status":        self._sig_update_status.emit(item[1])
                elif tag == "update_fault":          self._sig_update_fault.emit(item[1])
                elif tag == "update_get_response":   self._sig_update_get.emit(item[1], item[2])
                elif tag == "update_write_entry":    self._sig_update_write.emit(item[1], item[2])
                elif tag == "sync_modes":           self._sig_sync_modes.emit(item[1], item[2])
                elif tag == "error":                self._sig_error.emit(item[1])
                elif tag == "disconnect":
                    if not self._cable_drop_announced and self.serial.is_open:
                        self._cable_drop_announced = True
                        QTimer.singleShot(0, lambda: self._show_toast(
                            "Cable disconnected. Connection closed.", "error"))
                    self._sig_disconnect.emit()
                elif tag == "cmd_completed":        self._sig_cmd_completed.emit(item[1], item[2])
                elif tag == "_quality":             self._sig_quality.emit(item[1])
        except queue.Empty:
            pass

    def _on_error_signal(self, message: str):
        self._log_signal("ERR", message)

    def _on_cmd_completed(self, state, cmd_name: str):
        logging.info("Command '%s' finished: %s", cmd_name, state)

    # ──────────────────────────────────────────────────────────────────────────
    #  EVENT HANDLERS
    # ──────────────────────────────────────────────────────────────────────────

    def _show_toast(self, message: str, level: str = "warn"):
        """Modern floating toast card with drop shadow, anchored below header."""
        spec = {
            "warn":  ("fa5s.exclamation-triangle", C["orange_bg"], C["orange"],  C["orange_border"]),
            "error": ("fa5s.times-circle",          C["red_bg"],    C["red"],     C["red_border"]),
            "ok":    ("fa5s.check-circle",          C["green_bg"],  C["green"],   C["green_border"]),
            "info":  ("fa5s.info-circle",           C["blue_light"],C["blue"],    C["blue"]),
        }
        icon_name, bg, fg, border = spec.get(level, spec["warn"])

        anchor = self

        # Non-stacking: dismiss any currently visible toast before showing new one
        if hasattr(self, "_toast_stack") and self._toast_stack:
            for t in list(self._toast_stack):
                try:
                    t.hide()
                    t.deleteLater()
                except Exception:
                    pass
            self._toast_stack.clear()

        # Outer translucent window provides shadow margin; inner card is opaque pill
        outer = QFrame(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.NoDropShadowWindowHint,
        )
        outer.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        outer.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        outer_lay = QVBoxLayout(outer)
        outer_lay.setContentsMargins(12, 8, 12, 12)
        outer_lay.setSpacing(0)

        card = QFrame(outer)
        card.setObjectName("modern_toast_card")
        card.setStyleSheet(
            f"QFrame#modern_toast_card {{"
            f"  background: {bg};"
            f"  border: 1.5px solid {border};"
            f"  border-radius: 14px;"
            f"}}"
            f"QFrame#modern_toast_card QLabel {{"
            f"  background: transparent;"
            f"  color: {fg};"
            f"  font-size: {_px(12)}px;"
            f"  font-weight: 600;"
            f"}}"
        )
        outer_lay.addWidget(card)

        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(28)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(0, 0, 0, 100))
        card.setGraphicsEffect(shadow)

        card_lay = QHBoxLayout(card)
        card_lay.setContentsMargins(14, 8, 18, 8)
        card_lay.setSpacing(10)

        icon_lbl = QLabel()
        try:
            icon_lbl.setPixmap(qta.icon(icon_name, color=fg).pixmap(QSize(18, 18)))
        except Exception:
            icon_lbl.setText("●")
        card_lay.addWidget(icon_lbl)

        text_lbl = QLabel(message)
        text_lbl.setWordWrap(False)
        card_lay.addWidget(text_lbl)

        outer.adjustSize()
        tw = max(outer.sizeHint().width(), 284)
        th = outer.sizeHint().height()

        if not hasattr(self, "_toast_stack") or self._toast_stack is None:
            self._toast_stack = []
        self._toast_stack = [t for t in self._toast_stack if t is not None and t.isVisible()]
        y_step = th + 12

        try:
            ageom = anchor.frameGeometry()
            top_center_global = anchor.mapToGlobal(QPoint(ageom.width() // 2, 0))
            ax = top_center_global.x() - tw // 2
            ay = top_center_global.y() + 48
        except Exception:
            ax = (self.width() - tw) // 2
            ay = 72
        # No stacking: stack is always empty here (cleared above)

        outer.setGeometry(ax, ay - 12, tw, th)
        outer.show()
        outer.raise_()

        slide = QPropertyAnimation(outer, b"geometry", outer)
        slide.setDuration(220)
        slide.setStartValue(QRect(ax, ay - 12, tw, th))
        slide.setEndValue(QRect(ax, ay, tw, th))
        slide.setEasingCurve(QEasingCurve.Type.OutCubic)
        slide.start()

        self._toast_stack.append(outer)

        def _dismiss():
            try:
                if not outer.isVisible():
                    return
                out = QPropertyAnimation(outer, b"geometry", outer)
                out.setDuration(180)
                cur = outer.geometry()
                out.setStartValue(cur)
                out.setEndValue(QRect(cur.x(), cur.y() - 10, cur.width(), cur.height()))
                out.setEasingCurve(QEasingCurve.Type.InCubic)
                out.finished.connect(outer.hide)
                out.finished.connect(outer.deleteLater)
                out.start()
                if outer in self._toast_stack:
                    self._toast_stack.remove(outer)
            except Exception:
                try:
                    outer.deleteLater()
                except Exception:
                    pass

        QTimer.singleShot(3000, _dismiss)

    def _on_connect(self):
        port = self.port_combobox.currentText().strip().split(" — ")[0].strip()
        if not port:
            self._show_toast("No serial port selected. Choose a COM port first.", "error")
            return
        try:
            baud = int(self.baud_entry.currentText().strip())
        except Exception:
            self._show_toast("Invalid baud rate.", "error")
            return
        _VALID_BAUDS = {9600, 19200, 38400, 57600, 115200, 230400,
                        460800, 921600, 1000000, 1500000, 2000000}
        if baud not in _VALID_BAUDS:
            self._show_toast(f"Non-standard baud {baud} — verify hardware supports it", "warn")

        # Always disconnect first to clear any stale serial state
        self.serial.disconnect()

        def worker():
            try:
                self.serial.connect(port, baud)
                try:
                    resp = self.serial.send("g fpwm", expect_response=True)
                    self.fpwm = dec_decode(resp)
                    logging.info("Fpwm = %.0f Hz", self.fpwm)
                except Exception:
                    self.fpwm = 16000.0
                    logging.warning("Could not read Fpwm — using 16000 Hz default")
                self._sig_connect.emit(port, str(baud))
            except Exception as e:
                logging.exception("Failed to open serial port")
                msg = f"Failed to open {port}: {e}"
                self._sig_error.emit(msg)
                self._sig_disconnect.emit()

        threading.Thread(target=worker, daemon=True).start()

    def _on_disconnect(self):
        if self._user_disconnected:
            return  # guard against double-call (cable drop + user click race)
        self._user_disconnected = True
        try:
            self.serial.disconnect()  # close port first — loops hit exception and exit cleanly
        except Exception:
            pass
        self._stop_loop_thread()
        self._stop_fault_loop()
        self._stop_get_loop()
        self._stop_status_loop()
        self._stop_cmd_loop()
        self._set_disconnected_ui()
        self._log_signal("WARN", "Disconnected from serial port")

    def _on_reset(self):
        if not self.serial.is_open:
            _ModernModal.error(self, "Error", "Not connected.")
            return
        threading.Thread(target=self._send_reset_command, daemon=True).start()

    def _send_reset_command(self):
        try:
            self.serial.send("$", expect_response=True)
            self._reset_at = time.time()
            self.response_q.put(("update_response", "Reset sent", None))
        except Exception as e:
            self._reset_at = time.time()
            self.response_q.put(("error", f"Reset error: {e}"))
            self.response_q.put(("disconnect", None, None))

    def _on_sensor_combo_changed(self, idx: int):
        self._mode_lock_until = time.time() + 2.0
        if not self.serial.is_open:
            return
        mode_map = {0: "s sens 1", 1: "s sens 2", 2: "s sens 3", 3: "s sens 4"}
        cmd_str = mode_map.get(idx)
        if cmd_str:
            threading.Thread(target=lambda: self._safe_send(cmd_str), daemon=True).start()

    def _on_sensor_mode_changed_id(self, btn_id: int):
        self._on_sensor_combo_changed(btn_id - 1)

    def _on_control_combo_changed(self, idx: int):
        self._mode_lock_until = time.time() + 2.0
        modes = {0: ("Stop", "stop"), 1: ("Voltage", "contr 1"),
                 2: ("Current", "contr 2"), 3: ("Speed", "contr 3")}
        mode_name, cmd_val = modes.get(idx, ("Stop", "stop"))
        self._update_control_combo_style(mode_name)
        self.previous_control_mode = self.current_control_mode
        self.current_control_mode  = mode_name
        self._set_mode_ui(mode_name)
        if self.serial.is_open:
            threading.Thread(
                target=lambda: self._safe_send(f"s {cmd_val}"), daemon=True).start()

    def _on_control_mode_changed_id(self, btn_id: int):
        self._on_control_combo_changed(btn_id)

    def _safe_send(self, cmd_str):
        try:
            self.serial.send(cmd_str, expect_response=True)
        except Exception as e:
            self.response_q.put(("error", f"Communication error: {e}"))
            self.response_q.put(("disconnect", None, None))

    def _on_slider_changed(self, var_key: str, val: int, entry: QLineEdit):
        if self._slider_updating:
            return
        self._slider_updating = True
        entry.setText(str(float(val)))
        self._slider_updating = False

    def _on_ref_entry_return(self, var_key: str, entry: QLineEdit, slider: QSlider):
        if entry.isReadOnly() or not entry.isEnabled():
            return
        txt = entry.text().strip()
        if not txt:
            return
        try:
            val = float(txt)
        except ValueError:
            return
        self._slider_updating = True
        clamped = max(slider.minimum(), min(slider.maximum(), int(val)))
        slider.setValue(clamped)
        self._slider_updating = False

        e_widget = entry
        self.old_values[e_widget] = txt
        entry.setText(txt)
        scaled = self._scale_set(var_key, txt)
        cmd_map = {
            "IsdRef": f"s isd {scaled}", "IsqRef": f"s isq {scaled}",
            "UsdRef": f"s usd {scaled}", "UsqRef": f"s usq {scaled}",
            "speed":  f"s speed {scaled}", "accel": f"s accel {scaled}",
        }
        cmd_str = cmd_map.get(var_key, f"s {var_key} {scaled}")
        self._log_signal("OK", f"SET {var_key} = {txt}")
        threading.Thread(target=lambda: self._send_set_only(cmd_str), daemon=True).start()

    def _on_entry_return(self, entry: QLineEdit):
        if not entry.isEnabled():
            return
        new_value = entry.text().strip()
        if not new_value:
            return
        var_name = self.entry_map[entry]
        self.old_values[entry] = new_value
        entry.clear()
        scaled = self._scale_set(var_name, new_value)
        cmd_map = {
            "IsdRef": f"s isd {scaled}", "IsqRef": f"s isq {scaled}",
            "UsdRef": f"s usd {scaled}", "UsqRef": f"s usq {scaled}",
            "speed":  f"s speed {scaled}", "accel": f"s accel {scaled}",
        }
        cmd_str = cmd_map.get(var_name, f"s {var_name} {scaled}")
        threading.Thread(target=lambda: self._send_set_only(cmd_str), daemon=True).start()

    def _send_set_command(self, idx: int):
        if not self.serial.is_open:
            self._show_toast("Not connected. Connect to a serial port first.", "warn")
            return
        self._animate_write_header(idx)

        var_name = self.set_var_comboboxes[idx].currentText()
        if not var_name:
            _ModernModal.error(self, "Error", "Variable is required.")
            return

        value   = self.set_value_entries[idx].text().strip()
        var_key = self.VAR_MAP.get(var_name, var_name.lower())

        if not value:
            _ModernModal.error(self, "Error", "Value is required.")
            return

        if var_name == "Dy":
            try:
                dy = float(value)
                dg = self._get_current_dg()
                if dg is not None:
                    dy_max = 1.0 / (2.0 * dg * 0.1)
                    if dy > dy_max:
                        _ModernModal.error(self, "Dy Limit Exceeded",
                            f"<b>Dy = {dy:.2f} rad/s</b> exceeds the calculated maximum "
                            f"of <b>{dy_max:.1f} rad/s</b>.<br><br>"
                            f"Maximum Dy is computed as: <i>1 / (2 × Dg × 0.1)</i><br>"
                            f"Current Dg = {dg:.4f}<br><br>"
                            f"Please reduce Dy or recalibrate Dg first.")
                        return
            except ValueError:
                _ModernModal.error(self, "Error", "Dy must be a numeric value.")
                return

        scaled  = self._scale_set(var_name, value)
        cmd_str = f"s {var_key} {scaled}"
        self._log_signal("INFO", f"SET {var_name} = {scaled.strip()}")
        threading.Thread(
            target=lambda: self._send_set_and_readback(cmd_str, var_key, var_name, idx),
            daemon=True).start()

    def _safe_send_receive(self, cmd_str):
        if not self.serial.is_open:
            self.response_q.put(("error", "Serial port not available."))
            return
        try:
            self.serial.send(cmd_str, expect_response=True)
        except Exception as e:
            self.response_q.put(("error", f"Communication error: {e}"))
            self.response_q.put(("disconnect", None, None))

    def _send_and_receive(self, cmd_str, expect_response=True):
        if not self.serial.is_open:
            self.response_q.put(("error", "Serial port not available."))
            self.response_q.put(("disconnect", None, None))
            return
        try:
            resp = self.serial.send(cmd_str, expect_response=expect_response)
            if expect_response:
                self.response_q.put(("update_response", resp, None))
        except Exception as e:
            self.response_q.put(("error", f"Communication error: {e}"))
            self.response_q.put(("disconnect", None, None))

    def _send_set_and_readback(self, cmd_str, var_key, var_name, idx):
        """Send SET, then immediately GET the same variable and push confirmed value back to the entry."""
        if not self.serial.is_open:
            self.response_q.put(("error", "Serial port not available."))
            self.response_q.put(("disconnect", None, None))
            return
        try:
            self.serial.send(cmd_str, expect_response=True)
            # Read back the actual value the firmware accepted
            raw = self.serial.send(f"g {var_key}", expect_response=True)
            display = self._scale_get(var_name, raw)
            self.response_q.put(("update_write_entry", idx, display))
        except ConnectionError as e:
            self.response_q.put(("disconnect", None, None))
        except Exception as e:
            self.response_q.put(("error", f"Communication error: {e}"))
            self.response_q.put(("disconnect", None, None))

    def _send_set_only(self, cmd_str, entry=None):
        if not self.serial.is_open:
            self.response_q.put(("error", "Serial port not available."))
            self.response_q.put(("disconnect", None, None))
            return
        try:
            self.serial.send(cmd_str, expect_response=True)
        except Exception as e:
            self.response_q.put(("error", f"Communication error: {e}"))
            self.response_q.put(("disconnect", None, None))

    def _clear_fault(self):
        if not self.serial.is_open:
            _ModernModal.error(self, "Error", "Not connected.")
            return
        self._fault_clear_time = time.time()
        self._last_fault_state = None   # allow re-logging when fault truly returns
        threading.Thread(
            target=lambda: self._send_and_receive("s clrerr"), daemon=True).start()

    def _animate_write_header(self, idx: int = 0):
        pass  # arrow is now a static label; animation removed

    def _on_get_var_changed(self, idx: int):
        var = self.get_var_comboboxes[idx].currentText()
        self._get_unit_labels[idx].setText(self.UNIT_MAP.get(var, ""))
        self._get_var_cache[idx] = var

    def _on_set_var_changed(self, idx: int):
        var = self.set_var_comboboxes[idx].currentText()
        self._set_unit_labels[idx].setText(self.UNIT_MAP.get(var, ""))

    def _refresh_ports(self):
        prev = self.port_combobox.currentText().strip().split(" — ")[0].strip()
        ports = self._list_serial_ports()
        self.port_combobox.clear()
        self.port_combobox.addItems(ports)
        # Try to re-select the previously chosen port
        for i, p in enumerate(ports):
            if p.startswith(prev):
                self.port_combobox.setCurrentIndex(i)
                break
        if ports:
            self._log_signal("OK", f"Found {len(ports)} port(s): {', '.join(p.split('—')[0].strip() for p in ports)}")
        else:
            self._log_signal("WARN", "No serial ports found.")

    # ──────────────────────────────────────────────────────────────────────────
    #  VALUE SCALING
    # ──────────────────────────────────────────────────────────────────────────

    def _scale_set(self, var_name: str, value: str) -> str:
        try:
            return dec_encode(float(value))
        except ValueError:
            return value

    def _scale_get(self, var_name: str, value: str) -> str:
        try:
            return f"{dec_decode(value):.6g}"
        except (ValueError, Exception):
            return ""

    def _get_current_dg(self):
        try:
            if not self.serial.is_open:
                return None
            resp = self.serial.send("g damp ", expect_response=True)
            return float(resp.strip())
        except Exception:
            return None

    # ──────────────────────────────────────────────────────────────────────────
    #  SERIAL LOOPS  (unchanged)
    # ──────────────────────────────────────────────────────────────────────────

    def _list_serial_ports(self):
        if list_ports is None:
            return []
        try:
            ports = []
            for p in list_ports.comports():
                desc = p.description or ""
                if p.device in desc:
                    desc = desc.replace(f"({p.device})", "").strip(" -")
                # Friendly label: AMC devices are typically STMicro virtual COM ports
                if desc:
                    label = f"{p.device}  —  {desc}"
                else:
                    label = p.device
                ports.append(label)
            return ports
        except Exception:
            return []

    def _start_status_loop(self):
        self._stop_status_loop()
        self.status_loop_running = True
        self._status_stop.clear()
        self.status_loop_thread  = threading.Thread(target=self._status_loop, daemon=True)
        self.status_loop_thread.start()

    def _stop_status_loop(self):
        self.status_loop_running = False
        self._status_stop.set()
        if self.status_loop_thread and self.status_loop_thread.is_alive():
            self.status_loop_thread.join(timeout=2.0)
        self.status_loop_thread = None
        self._status_stop.clear()

    def _status_loop(self):
        _cm = {0: "MODE_OFF", 1: "MODE_VOLTAGE", 2: "MODE_CURRENT", 3: "MODE_SPEED"}
        _sm = {1: "FixAngle", 2: "Sensor", 3: "Sensorless_BEMF", 4: "Sensorless_HFI"}
        _err_count = 0
        while not self._status_stop.is_set():
            if self.serial.is_open:
                _t0 = time.monotonic()
                try:
                    resp = self.serial.send("g contr", expect_response=True)
                    _latency_ms = (time.monotonic() - _t0) * 1000.0
                    _err_count = max(0, _err_count - 1)
                    if _latency_ms < 50:
                        quality = f"  ● {_latency_ms:.0f} ms"
                    elif _latency_ms < 200:
                        quality = f"  ◑ {_latency_ms:.0f} ms  (slow)"
                    else:
                        quality = f"  ◌ {_latency_ms:.0f} ms  (degraded)"
                    self.response_q.put(("_quality", quality, None))
                    val  = int(round(dec_decode(resp)))
                    st   = _cm.get(val, f"MODE_{val}")
                    self.last_valid_status = st
                    self.response_q.put(("update_status", st, None))
                except ConnectionError as e:
                    logging.warning("Status loop cable drop: %s", e)
                    self.response_q.put(("disconnect", None, None))
                    break
                except Exception:
                    self.response_q.put(("update_status", self.last_valid_status, None))
                try:
                    resp = self.serial.send("g sens ", expect_response=True)
                    val  = int(round(dec_decode(resp)))
                    st   = _sm.get(val)
                    if st:
                        self.response_q.put(("sync_modes", self.last_valid_status, st))
                except ConnectionError as e:
                    logging.warning("Status loop cable drop: %s", e)
                    self.response_q.put(("disconnect", None, None))
                    break
                except Exception:
                    pass
            self._status_stop.wait(1.0)

    def _start_fault_loop(self):
        self._stop_fault_loop()
        self.fault_loop_running = True
        self._fault_stop.clear()
        self.fault_loop_thread  = threading.Thread(target=self._fault_loop, daemon=True)
        self.fault_loop_thread.start()

    def _stop_fault_loop(self):
        self.fault_loop_running = False
        self._fault_stop.set()
        if self.fault_loop_thread and self.fault_loop_thread.is_alive():
            self.fault_loop_thread.join(timeout=2.0)
        self.fault_loop_thread = None
        self._fault_stop.clear()

    def _fault_loop(self):
        while not self._fault_stop.is_set():
            if self.serial.is_open:
                in_reboot = (time.time() - getattr(self, "_reset_at", 0)) < 30.0
                if in_reboot:
                    time.sleep(1)
                    continue
                try:
                    resp = self.serial.send("g err", expect_response=True)
                    self.response_q.put(("update_fault", resp, None))
                    self.last_valid_fault = resp
                except ValueError as e:
                    logging.debug("fault_loop: no prompt yet: %s", e)
                    self.response_q.put(("update_fault", self.last_valid_fault, None))
                except (ConnectionError, OSError) as e:
                    logging.warning("fault_loop connection error: %s", e)
                    self.response_q.put(("update_fault", self.last_valid_fault, None))
                except Exception:
                    logging.exception("fault_loop unexpected error")
                    self.response_q.put(("update_fault", self.last_valid_fault, None))
            self._fault_stop.wait(1.0)

    def _start_get_loop(self):
        self._stop_get_loop()
        self.get_loop_running = True
        self._get_stop.clear()
        self.get_loop_thread  = threading.Thread(target=self._get_loop, daemon=True)
        self.get_loop_thread.start()

    def _stop_get_loop(self):
        self.get_loop_running = False
        self._get_stop.set()
        if self.get_loop_thread and self.get_loop_thread.is_alive():
            self.get_loop_thread.join(timeout=2.0)
        self.get_loop_thread = None
        self._get_stop.clear()

    def _get_loop(self):
        sleep_dur = 0.1
        iters_per_poll = int(1.0 / sleep_dur)
        count = 0
        while not self._get_stop.is_set():
            if not self.serial.is_open:
                break
            if count >= iters_per_poll:
                for i in range(3):
                    if self._get_stop.is_set() or not self.serial.is_open:
                        break
                    var = self._get_var_cache[i] if i < len(self._get_var_cache) else ""
                    if var:
                        cmd_var = self.VAR_MAP.get(var, var)
                        try:
                            resp = self.serial.send(f"g {cmd_var}", expect_response=True)
                            self.response_q.put(("update_get_response", i, resp))
                            self.last_valid_get_responses[i] = resp
                        except ConnectionError as e:
                            logging.warning("Get loop cable drop: %s", e)
                            self.response_q.put(("disconnect", None, None))
                            self._get_stop.set()
                            break
                        except ValueError:
                            self.response_q.put(("update_get_response", i,
                                                 self.last_valid_get_responses.get(i, "N/A")))
                        except Exception:
                            self.response_q.put(("update_get_response", i,
                                                 self.last_valid_get_responses.get(i, "N/A")))
                count = 0
            count += 1
            self._get_stop.wait(sleep_dur)

    def _start_cmd_loop(self):
        self._stop_cmd_loop()
        self.cmd_loop_running = True
        self._cmd_stop.clear()
        self.cmd_loop_thread  = threading.Thread(target=self._cmd_loop, daemon=True)
        self.cmd_loop_thread.start()

    def _stop_cmd_loop(self):
        self.cmd_loop_running = False
        self._cmd_stop.set()
        if self.cmd_loop_thread and self.cmd_loop_thread.is_alive():
            self.cmd_loop_thread.join(timeout=2.0)
        self.cmd_loop_thread = None
        self._cmd_stop.clear()

    def _cmd_loop(self):
        sleep_dur = 0.1
        iters = int(1.0 / sleep_dur)
        count = 0
        while not self._cmd_stop.is_set():
            if not self.serial.is_open:
                break
            if count >= iters:
                try:
                    self.cmd_manager.poll_queue_status()
                except ConnectionError as e:
                    logging.warning("Cmd loop cable drop: %s", e)
                    self.response_q.put(("disconnect", None, None))
                    break
                count = 0
            count += 1
            self._cmd_stop.wait(sleep_dur)

    def _stop_loop_thread(self):
        if hasattr(self, 'loop_thread') and self.loop_thread and \
                self.loop_thread.is_alive():
            self.loop_running = False
            self.loop_thread.join(timeout=1)
            self.loop_thread = None

    # ──────────────────────────────────────────────────────────────────────────
    #  ACTIVITY LOG
    # ──────────────────────────────────────────────────────────────────────────

    def _log_signal(self, level: str, message: str):
        self._sig_log.emit(level, message)

    def _append_log(self, level: str, message: str):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        # (badge_bg, badge_fg, text_color)
        level_styles = {
            "SYS":  ("#1E2A3A", "#7B9AB8", "#A8B8C8"),
            "OK":   ("#0F2A1A", "#34D399", "#C8EDD8"),
            "WARN": ("#2A1F08", "#FBBF24", "#F0D89A"),
            "ERR":  ("#2A0F0F", "#F87171", "#F0C8C8"),
            "INFO": ("#0F1E35", "#60A5FA", "#A8C8F0"),
        }
        bg, fg, txt = level_styles.get(level, ("#1E2A3A", "#7B9AB8", "#A8B8C8"))
        term = "font-family:'Cascadia Code','JetBrains Mono','Fira Code',Consolas,monospace;"
        badge = (
            f'background:{bg}; color:{fg}; '
            f'border:1px solid {fg}55; border-radius:3px; '
            f'padding:1px 7px; font-size:10px; font-weight:700; letter-spacing:0.5px; {term}'
        )
        html = (
            f'<span style="{term} color:#3D5068; font-size:11px;">{ts}</span>'
            f'&nbsp;&nbsp;'
            f'<span style="{badge}">{level[:4].upper()}</span>'
            f'&nbsp;&nbsp;'
            f'<span style="{term} color:{txt}; font-size:12px;">{escape(message)}</span>'
        )
        self._log_text.append(html)
        sb = self._log_text.verticalScrollBar()
        sb.setValue(sb.maximum())
        if hasattr(self, "_log_file"):
            self._log_file.write(f"[{ts}] [{level:4}] {message}")

    def _clear_log(self):
        self._log_text.clear()

    def _export_log(self):
        default_name = os.path.basename(self._log_file.current_path()) if hasattr(self, "_log_file") else "amc_log.txt"
        default_name = os.path.splitext(default_name)[0] + ".txt"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Log", default_name, "Text Files (*.txt);;All Files (*)")
        if path:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(self._log_text.toPlainText())
                self._log_signal("INFO", f"Log exported to {path}")
                self._show_toast(f"Log exported to {os.path.basename(path)}", "ok")
            except Exception as e:
                _ModernModal.error(self, "Export failed", str(e))

    # ──────────────────────────────────────────────────────────────────────────
    #  SUB-WINDOW OPENERS
    # ──────────────────────────────────────────────────────────────────────────

    def _check_ready_for_identification(self) -> bool:
        if not self.serial.is_open:
            _ModernModal.error(self, "Error", "Not connected.")
            return False
        if self.last_valid_status != "MODE_OFF":
            role = _ModernModal.warn(
                self, "Cannot Start",
                f"Motor must be in STOP state.<br>Current: <b>{self.last_valid_status}</b>",
                [("Cancel", "secondary"), ("Stop Motor", "primary")])
            if role == "primary":
                btn = self._control_map.get("Stop")
                if btn:
                    btn.setChecked(True)
            return False
        if self.last_valid_fault != "No Fault":
            _ModernModal.error(
                self, "Cannot Start",
                f"Active fault: <b>{self.last_valid_fault}</b><br>Clear the fault manually before starting.")
            return False
        return True

    def open_electrical_params(self):
        if not ElectricalParametersIdentification or not self._check_ready_for_identification():
            return
        self._stop_get_loop(); self._stop_fault_loop(); self._stop_status_loop()
        try:
            self.serial._ser.reset_input_buffer()
        except Exception:
            pass
        defaults = {}
        try:
            pole  = dec_decode(self.serial.send("g mpole ", expect_response=True))
            isqmx = dec_decode(self.serial.send("g iqmax ", expect_response=True))
            speed = dec_decode(self.serial.send("g spmax ", expect_response=True))
            udc   = dec_decode(self.serial.send("g gudc  ", expect_response=True))
            defaults = {
                "pole": int(round(pole)),
                "current": round(isqmx / math.sqrt(2.0), 2),
                "speed": int(round(speed)),
                "voltage": round(udc / math.sqrt(3.0), 2),
            }
        except Exception as e:
            logging.warning("Could not read defaults: %s", e)
        if ElectricalParametersIdentification:
            dlg = ElectricalParametersIdentification(self, self.serial, self.cmd_manager,
                                                     defaults=defaults)
            dlg.exec()
        else:
            _ModernModal.error(self, "Module not found", "electrical_params_qt.py not found.")
        self._start_get_loop(); self._start_fault_loop(); self._start_status_loop()

    def open_save_params(self):
        if not SaveParameters:
            _ModernModal.error(self, "Module not found", "save_params_qt.py not found.")
            return
        dlg = SaveParameters(self, self.serial)
        dlg.exec()

    def open_load_params(self):
        if not LoadParameters:
            _ModernModal.error(self, "Module not found", "load_params_qt.py not found.")
            return
        dlg = LoadParameters(self, self.serial)
        dlg.exec()

    def open_mechanical_params(self):
        if not InertiaIdentification or not self._check_ready_for_identification():
            return
        # Stop background serial loops so identification worker has exclusive access
        self._stop_get_loop(); self._stop_fault_loop(); self._stop_status_loop()
        try:
            self.serial._ser.reset_input_buffer()
        except Exception:
            pass
        if InertiaIdentification:
            dlg = InertiaIdentification(self, self.serial, self.cmd_manager)
            dlg.exec()
        else:
            _ModernModal.error(self, "Module not found", "inertia_param_qt.py not found.")
        # Restart loops after dialog closes (only if still connected)
        if self.serial.is_open:
            self._start_fault_loop()
            self._start_get_loop()
            self._start_status_loop()

    def open_terminal(self):
        if not Terminal:
            _ModernModal.error(self, "Module not found", "terminal_qt.py not found.")
            return
        dlg = Terminal(self, self.serial)
        dlg.exec()

    def open_monitoring(self):
        if not ScopeWindow:
            _ModernModal.error(self, "Module not found", "scope_qt.py not found.")
            return
        # If combined view is active, exit it first then show standalone
        if self._combined_view_active:
            self._exit_combined_view()
            return
        # Non-modal: keep a reference so it doesn't get garbage-collected
        if hasattr(self, '_scope_window') and self._scope_window is not None:
            try:
                self._scope_window.raise_()
                self._scope_window.activateWindow()
                return
            except RuntimeError:
                pass
        dlg = ScopeWindow(self, self.serial, fpwm=self.fpwm)
        dlg.setWindowModality(Qt.WindowModality.NonModal)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dlg.finished.connect(lambda: setattr(self, '_scope_window', None))
        self._scope_window = dlg
        dlg.show()

    def open_info(self):
        try:
            y = int(APP_VERSION[0]) + 2020
            m = int(APP_VERSION[1:3])
            d = int(APP_VERSION[3:5])
            date_str = f"{y}-{m:02d}-{d:02d}"
        except Exception:
            date_str = "—"
        _ModernModal.info(self, "AMC Interface",
            f"<b>AMC Interface</b><br><br>"
            f"Project: {APP_PROJECT}<br>"
            f"Version: {APP_VERSION}<br>"
            f"Date: {date_str}<br><br>"
            f"<small>Appcon Technologies</small>")

    def closeEvent(self, event):
        role = _ModernModal.warn(
            self, "Quit AMC Interface?",
            "Closing will disconnect the serial port and stop all monitoring.<br>"
            "Your activity log is saved automatically in the <b>logs/</b> folder.<br><br>"
            "Are you sure you want to exit?",
            [("Cancel", MODAL_CANCELLED), ("Quit", MODAL_CONFIRMED)],
        )
        if role != MODAL_CONFIRMED:
            event.ignore()
            return
        scope = getattr(self, "_scope_window", None)
        if scope is not None:
            try:
                scope.close()
            except Exception:
                pass
        try:
            self.serial.disconnect()
        except Exception:
            pass
        self._stop_loop_thread()
        self._stop_fault_loop()
        self._stop_get_loop()
        self._stop_status_loop()
        self._stop_cmd_loop()
        try:
            self._log_file.close()
        except Exception:
            pass
        event.accept()


# ═══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # These must be set BEFORE QApplication is created.
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    os.environ.setdefault("QT_SCALE_FACTOR_ROUNDING_POLICY", "PassThrough")
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)

    app = QApplication(sys.argv)
    app.setApplicationName("AMC Interface")
    app.setOrganizationName("Appcon Technologies")
    app.setStyle("Fusion")   # consistent cross-platform rendering; avoids Windows-native widget quirks

    font = QFont("Segoe UI", 10)
    app.setFont(font)

    window = AMCMainWindow()
    window.show()
    sys.exit(app.exec())
