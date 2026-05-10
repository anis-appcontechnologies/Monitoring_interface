#!/usr/bin/env python3
"""
Oscilloscope / Scope — PySide6 Edition

Port and redesign of the Tkinter scope module.
All serial communication logic, buffer parsing, ring-buffer scroll mode,
real-time mode, and single-shot recording are preserved exactly.
Only the UI framework changes: Tkinter -> PySide6, styled to match the
AMC Interface design system defined in amc_interface_qt.py.

Author: DAGBAGI Mohamed  (PySide6 port: Appcon Technologies)
"""

import os
import re
import subprocess
import collections
import threading
import time
import struct
import logging

import numpy as np

# ── ELF variable extraction (optional — silently skipped if pyelftools missing)
try:
    from elftools.elf.elffile import ELFFile as _ELFFile
    _HAS_ELFTOOLS = True
except ImportError:
    _HAS_ELFTOOLS = False

# Module-level store for ELF-extracted variable short names (loaded once)
_ELF_VARS: list[str] = []          # short display names
_ELF_VARS_FULL: list[str] = []     # full names (kept for reference, not displayed)
_ELF_LOADED = False

_ELF_GDB_EXE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "SCAN_COMMAND_FILE",
                             "APPCON_COMMAND_SCAN_GLOBAL_VAR.exe")


def _elf_short(full: str) -> str:
    parts = full.split("_")
    if len(parts) <= 1:
        return full
    return "_".join(parts[-2:]) if parts[-1].isdigit() else parts[-1]


def _elf_read_symbols(elf_path: str) -> list:
    """Read OBJECT symbols from ELF using pyelftools."""
    out = []
    if not _HAS_ELFTOOLS:
        return out
    try:
        with open(elf_path, "rb") as f:
            elf = _ELFFile(f)
            for sec in elf.iter_sections():
                if sec.name not in (".symtab", ".dynsym"):
                    continue
                for sym in sec.iter_symbols():
                    if sym.entry["st_info"]["type"] != "STT_OBJECT":
                        continue
                    sz   = sym.entry["st_size"]
                    name = sym.name
                    if sz == 0 or name.startswith("_") or "." in name:
                        continue
                    out.append(name)
    except Exception as exc:
        logging.warning("ELF symbol read failed: %s", exc)
    return out


def _elf_load(elf_path: str) -> list:
    """Load short names from an ELF file. Returns list of short names."""
    global _ELF_VARS, _ELF_VARS_FULL, _ELF_LOADED
    full_names = _elf_read_symbols(elf_path)
    if not full_names:
        return []
    _ELF_VARS_FULL = full_names
    _ELF_VARS      = [_elf_short(n) for n in full_names]
    _ELF_LOADED    = True
    return _ELF_VARS


def _elf_find_in_folder(folder: str, timeout_s: float = 12.0) -> list:
    """
    Walk a project folder and return .elf/.axf paths, Debug/Release first.
    Raises TimeoutError if the walk takes longer than timeout_s seconds.
    """
    import time as _time
    deadline = _time.monotonic() + timeout_s
    pref, other = [], []
    for root, _, files in os.walk(folder):
        if _time.monotonic() > deadline:
            raise TimeoutError(
                f"Folder scan exceeded {timeout_s:.0f} s — "
                "please select a more specific subfolder (e.g. Debug or Release).")
        for fn in files:
            if fn.lower().endswith((".elf", ".axf")):
                full  = os.path.join(root, fn)
                mtime = os.path.getmtime(full)
                (pref if (os.sep + "Debug"   + os.sep in full or
                          os.sep + "Release" + os.sep in full)
                       else other).append((mtime, full))
    pref.sort(reverse=True); other.sort(reverse=True)
    return [p for _, p in pref] + [p for _, p in other]

from PySide6.QtWidgets import (
    QDialog, QFrame, QLabel, QPushButton, QComboBox, QDoubleSpinBox,
    QCheckBox, QHBoxLayout, QVBoxLayout, QSizePolicy, QFileDialog, QWidget,
    QScrollArea, QMessageBox, QListWidget, QListWidgetItem, QAbstractItemView,
    QDialogButtonBox,
)
from PySide6.QtCore import Qt, QTimer, Signal, QSettings, QMetaObject, Q_ARG
from PySide6.QtGui import QFont, QKeySequence, QShortcut, QPainter, QPixmap, QColor, QPen, QIcon, QBrush

import matplotlib
matplotlib.use("QtAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(message)s")
logging.getLogger("matplotlib").setLevel(logging.WARNING)
logging.getLogger("matplotlib.font_manager").setLevel(logging.WARNING)

# ═══════════════════════════════════════════════════════════════════════════════
#  CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════

VARIABLE_CODES = {
    "None": 0, "IS1": 1, "IS2": 2, "IS3": 3,
    "US1": 4, "US2": 5, "US3": 6,
    "ISD": 7, "ISQ": 8, "UDC": 9, "DMACNT": 10,
}

VARIABLE_NAMES = {
    0: "None", 1: "IS1", 2: "IS2", 3: "IS3",
    4: "US1", 5: "US2", 6: "US3",
    7: "ISD", 8: "ISQ", 9: "UDC", 10: "DMACNT",
}

VARIABLE_DATATYPES = {
    0: 0, 1: 1, 2: 1, 3: 1,
    4: 1, 5: 1, 6: 1,
    7: 1, 8: 1, 9: 1, 10: 2,
}

CHANNEL_COLORS = ['#2196F3', '#F44336', '#4CAF50', '#FF9800']

VARIABLE_UNITS = {
    "None": "",
    "IS1":  "A",   "IS2": "A",  "IS3":  "A",
    "US1":  "V",   "US2": "V",  "US3":  "V",
    "ISD":  "A",   "ISQ": "A",  "UDC":  "V",
    "DMACNT": "",
}

MONO_FONT_FAMILY = "Cascadia Code, JetBrains Mono, Fira Code, Consolas, monospace"


def _apply_mono(widget):
    f = widget.font()
    f.setFamily(MONO_FONT_FAMILY)
    f.setStyleHint(QFont.StyleHint.Monospace)
    widget.setFont(f)


def _make_maximize_icon(color_hex: str, size: int = 16) -> QIcon:
    """Browser-style full-screen icon: four corner arrows pointing inward."""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(color_hex))
    pen.setWidth(2)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    m, a = 2, 4  # margin, arrow arm length
    # Top-left corner: right angle lines + arrow
    p.drawLine(m, m + a, m, m); p.drawLine(m, m, m + a, m)
    # Top-right corner
    p.drawLine(size - m - a, m, size - m, m); p.drawLine(size - m, m, size - m, m + a)
    # Bottom-left corner
    p.drawLine(m, size - m - a, m, size - m); p.drawLine(m, size - m, m + a, size - m)
    # Bottom-right corner
    p.drawLine(size - m - a, size - m, size - m, size - m); p.drawLine(size - m, size - m, size - m, size - m - a)
    p.end()
    return QIcon(pix)


def _make_restore_icon(color_hex: str, size: int = 16) -> QIcon:
    """Restore icon: four corner arrows pointing outward."""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(color_hex))
    pen.setWidth(2)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    c, a = size // 2, 4  # center, arm length
    # Four L-shapes from center outward
    p.drawLine(c - 1, c - 1, c - a, c - a); p.drawLine(c - a, c - a, c - a, c - a + 2); p.drawLine(c - a, c - a, c - a + 2, c - a)
    p.drawLine(c + 1, c - 1, c + a, c - a); p.drawLine(c + a, c - a, c + a, c - a + 2); p.drawLine(c + a, c - a, c + a - 2, c - a)
    p.drawLine(c - 1, c + 1, c - a, c + a); p.drawLine(c - a, c + a, c - a, c + a - 2); p.drawLine(c - a, c + a, c - a + 2, c + a)
    p.drawLine(c + 1, c + 1, c + a, c + a); p.drawLine(c + a, c + a, c + a, c + a - 2); p.drawLine(c + a, c + a, c + a - 2, c + a)
    p.end()
    return QIcon(pix)


def _make_elf_icon(size: int = 40, dark: bool = False) -> QIcon:
    """
    Crisp ELF file icon. Drawn at 512 px internally then scaled.
    dark=True → light-on-dark variant for dark UI themes.
    """
    from PySide6.QtGui import QPolygonF
    from PySide6.QtCore import QPointF, QRectF

    S   = 512
    pix = QPixmap(S, S)
    pix.fill(Qt.GlobalColor.transparent)
    p   = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)

    # theme-dependent colors
    if dark:
        body_fill    = "#2A2D3E"   # dark navy body
        fold_fill    = "#3D4255"   # slightly lighter fold
        outline_col  = "#8892A4"   # muted light-grey outline
        badge_col    = "#2A5BA8"   # bright blue badge
        arrow_col    = "#8892A4"   # same as outline
    else:
        body_fill    = "#FFFFFF"
        fold_fill    = "#C5CDD8"
        outline_col  = "#3C4858"
        badge_col    = "#1C3F6E"
        arrow_col    = "#3C4858"

    # geometry constants
    m    = int(S * 0.06)
    fold = int(S * 0.24)

    # ── document body ─────────────────────────────────────────────
    body = QPolygonF([
        QPointF(m,                m),
        QPointF(S - fold - m,     m),
        QPointF(S - m,            fold + m),
        QPointF(S - m,            S - m),
        QPointF(m,                S - m),
    ])
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(QColor(body_fill)))
    p.drawPolygon(body)

    # folded corner triangle
    corner = QPolygonF([
        QPointF(S - fold - m,  m),
        QPointF(S - fold - m,  fold + m),
        QPointF(S - m,         fold + m),
    ])
    p.setBrush(QBrush(QColor(fold_fill)))
    p.drawPolygon(corner)

    # document outline
    outline = QPen(QColor(outline_col))
    outline.setWidthF(S * 0.038)
    outline.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
    outline.setCapStyle(Qt.PenCapStyle.SquareCap)
    p.setPen(outline)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPolygon(body)
    p.drawLine(QPointF(S - fold - m, m),
               QPointF(S - fold - m, fold + m))
    p.drawLine(QPointF(S - fold - m, fold + m),
               QPointF(S - m,        fold + m))

    # ── .ELF badge ────────────────────────────────────────────────
    bx = m + int(S * 0.04)
    by = int(S * 0.29)
    bw = S - bx * 2
    bh = int(S * 0.24)
    badge = QRectF(bx, by, bw, bh)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(QColor(badge_col)))
    p.drawRoundedRect(badge, bh * 0.15, bh * 0.15)

    font = p.font()
    font.setFamily("Arial")
    font.setBold(True)
    font.setPixelSize(int(bh * 0.65))
    p.setFont(font)
    p.setPen(QPen(QColor("#FFFFFF")))
    p.drawText(badge, Qt.AlignmentFlag.AlignCenter, ".ELF")

    # ── download arrow ────────────────────────────────────────────
    ap = QPen(QColor(arrow_col))
    ap.setWidthF(S * 0.048)
    ap.setCapStyle(Qt.PenCapStyle.RoundCap)
    ap.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(ap)
    p.setBrush(Qt.BrushStyle.NoBrush)

    cx      = S * 0.50
    shaft_t = by + bh + S * 0.05
    shaft_b = S * 0.80
    hw      = S * 0.13
    tray_y  = shaft_b + S * 0.07
    tw      = S * 0.22

    p.drawLine(QPointF(cx, shaft_t), QPointF(cx, shaft_b - hw * 0.6))
    p.drawLine(QPointF(cx - hw, shaft_b - hw), QPointF(cx, shaft_b))
    p.drawLine(QPointF(cx + hw, shaft_b - hw), QPointF(cx, shaft_b))
    p.drawLine(QPointF(cx - tw, tray_y), QPointF(cx + tw, tray_y))

    p.end()

    final = pix.scaled(size, size,
                       Qt.AspectRatioMode.KeepAspectRatio,
                       Qt.TransformationMode.SmoothTransformation)
    return QIcon(final)


def _make_updown_arrow_path(color_hex: str, size: int = 14) -> str:
    """
    Draw a ▲▼ pixmap and save to a temp PNG.
    Returns the file path so it can be used in QSS url().
    """
    import tempfile, os as _os
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(color_hex))
    pen.setWidth(1)
    p.setPen(pen)
    p.setBrush(QBrush(QColor(color_hex)))
    half = size // 2
    margin = size // 4

    # ▲ top half
    from PySide6.QtGui import QPolygon
    from PySide6.QtCore import QPoint
    up = QPolygon([
        QPoint(margin,      half - 1),
        QPoint(size - margin, half - 1),
        QPoint(half,        1),
    ])
    p.drawPolygon(up)
    # ▼ bottom half
    dn = QPolygon([
        QPoint(margin,      half + 1),
        QPoint(size - margin, half + 1),
        QPoint(half,        size - 1),
    ])
    p.drawPolygon(dn)
    p.end()

    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.close()
    pix.save(tmp.name, "PNG")
    return tmp.name.replace("\\", "/")


def _make_theme_icon(dark_mode: bool, size: int = 18) -> QIcon:
    """Draw a sun (light mode) or crescent-moon (dark mode) icon."""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    if dark_mode:
        # Crescent moon — filled circle with bite taken out
        color = QColor("#FFC107")
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(color))
        p.drawEllipse(2, 2, size - 4, size - 4)
        p.setBrush(QBrush(Qt.GlobalColor.transparent))
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
        p.drawEllipse(5, 1, size - 4, size - 4)
    else:
        # Sun — circle with rays
        color = QColor("#F59E0B")
        cx, cy, r = size // 2, size // 2, size // 2 - 4
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(color))
        p.drawEllipse(cx - r, cy - r, 2 * r, 2 * r)
        pen = QPen(color)
        pen.setWidth(2)
        p.setPen(pen)
        import math
        for i in range(8):
            angle = math.radians(i * 45)
            inner = r + 2
            outer = r + 5
            x1 = int(cx + inner * math.cos(angle))
            y1 = int(cy + inner * math.sin(angle))
            x2 = int(cx + outer * math.cos(angle))
            y2 = int(cy + outer * math.sin(angle))
            p.drawLine(x1, y1, x2, y2)
    p.end()
    return QIcon(pix)


# ═══════════════════════════════════════════════════════════════════════════════
#  PALETTE HELPER
# ═══════════════════════════════════════════════════════════════════════════════

def _get_palette():
    try:
        import amc_interface_qt as _amcqt
        return _amcqt.C
    except Exception:
        return {
            "white":         "#FFFFFF",
            "bg":            "#F2F3F5",
            "card":          "#FFFFFF",
            "border":        "#E0E2E7",
            "text":          "#1A1A2E",
            "text2":         "#3D3D5C",
            "muted":         "#6B7280",
            "faint":         "#B0B8C8",
            "red":           "#B71C1C",
            "red_dark":      "#7F1212",
            "red_bg":        "#FEECEC",
            "red_border":    "#F5BABA",
            "blue":          "#B71C1C",
            "blue_dark":     "#7F1212",
            "blue_light":    "#FEECEC",
            "green":         "#2E7D32",
            "green_dark":    "#1B5E20",
            "green_bg":      "#E8F5E9",
            "green_border":  "#A5D6A7",
            "orange":        "#E65100",
            "orange_bg":     "#FFF3E0",
            "orange_border": "#FFCC80",
            "input_bg":      "#F8F9FB",
            "log_bg":        "#1A1A2E",
            "log_text":      "#E8EAF0",
        }


# ═══════════════════════════════════════════════════════════════════════════════
#  DECIMAL ENCODE  (verbatim from reference scope.py / SAL_AMCComm.c)
# ═══════════════════════════════════════════════════════════════════════════════

def dec_encode(value: float) -> str:
    sign = '-' if value < 0 else '+'
    absval = abs(value)
    if absval > 999999999.0:
        absval = 999999999.0
    int_part = int(absval)
    int_digits = max(1, len(str(int_part)))
    frac_digits = max(0, 8 - int_digits) if int_digits < 9 else 0
    result = sign + f"{absval:.{frac_digits}f}"
    return result[:10].ljust(10)


# ═══════════════════════════════════════════════════════════════════════════════
#  CUSTOM COMBO — looks like QComboBox, popup has [−] per row
# ═══════════════════════════════════════════════════════════════════════════════

class _ChannelCombo(QWidget):
    """
    Drop-in replacement for QComboBox on the channel grid.
    Visually identical to QComboBox#sc_combo: same height, same border,
    same dropdown arrow.  The popup is a QListWidget where every row
    has the variable name on the left and a red [−] button on the right.
    Pressing [−] removes that item; clicking the name selects it.

    Public API mirrors the QComboBox subset used by ScopeWindow:
        currentText(), currentIndex(), setCurrentText(), setCurrentIndex(),
        addItem(), removeItem(), findText(), itemText(), count()
    Signal:
        currentIndexChanged(int)
    """

    currentIndexChanged = Signal(int)

    # "None" is the only truly un-removable item
    _PROTECTED = {"None"}

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: list[str] = []
        self._current: int = 0
        self._popup: "QFrame | None" = None
        self._close_others_cb = None  # set by ScopeWindow to enforce singleton popup
        self._toast_cb = None         # set by ScopeWindow to show remove notifications
        self._build()

    # ── appearance ────────────────────────────────────────────────

    def _build(self):
        self.setObjectName("sc_channel_combo_wrap")
        # outer frame styled like a QComboBox
        self._frame = QFrame(self)
        self._frame.setObjectName("sc_combo_display_frame")
        self._frame.setCursor(Qt.CursorShape.PointingHandCursor)

        fl = QHBoxLayout(self._frame)
        fl.setContentsMargins(7, 0, 2, 0)
        fl.setSpacing(0)

        self._lbl = QLabel()
        self._lbl.setObjectName("sc_combo_display_lbl")
        self._lbl.setSizePolicy(QSizePolicy.Policy.Expanding,
                                QSizePolicy.Policy.Preferred)
        fl.addWidget(self._lbl)

        # drop-down arrow area (mimics QComboBox ::drop-down)
        self._arrow_area = QFrame()
        self._arrow_area.setObjectName("sc_combo_display_arrow")
        self._arrow_area.setFixedWidth(22)
        al = QHBoxLayout(self._arrow_area)
        al.setContentsMargins(0, 0, 0, 0)
        arr_lbl = QLabel("▾")
        arr_lbl.setObjectName("sc_combo_display_arrowlbl")
        arr_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        al.addWidget(arr_lbl)
        fl.addWidget(self._arrow_area)

        # make entire frame clickable
        self._frame.mousePressEvent = lambda e: self._toggle_popup()

        # wrap in outer layout
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self._frame)

    def _refresh_display(self):
        txt = self._items[self._current] if self._items else ""
        self._lbl.setText(txt)

    # ── public QComboBox-compatible API ──────────────────────────

    def currentText(self) -> str:
        return self._items[self._current] if self._items else ""

    def currentIndex(self) -> int:
        return self._current

    def count(self) -> int:
        return len(self._items)

    def itemText(self, index: int) -> str:
        return self._items[index] if 0 <= index < len(self._items) else ""

    def findText(self, text: str) -> int:
        try:
            return self._items.index(text)
        except ValueError:
            return -1

    def addItem(self, text: str):
        self._items.append(text)
        self._refresh_display()

    def addItems(self, texts):
        for t in texts:
            self._items.append(t)
        self._refresh_display()

    def removeItem(self, index: int):
        if index < 0 or index >= len(self._items):
            return
        self._items.pop(index)
        if self._current >= len(self._items):
            self._current = max(0, len(self._items) - 1)
        self._refresh_display()
        self.currentIndexChanged.emit(self._current)

    def setCurrentIndex(self, index: int):
        if 0 <= index < len(self._items):
            self._current = index
            self._refresh_display()
            self.currentIndexChanged.emit(self._current)

    def setCurrentText(self, text: str):
        idx = self.findText(text)
        if idx >= 0:
            self.setCurrentIndex(idx)

    # ── popup ─────────────────────────────────────────────────────

    def _toggle_popup(self):
        if self._popup and self._popup.isVisible():
            self._close_popup()
            return
        self._open_popup()

    def _close_popup(self):
        if self._popup:
            self._popup.hide()
            self._popup.deleteLater()
            self._popup = None

    def _open_popup(self):
        self._close_popup()
        # Enforce singleton: close any other channel combo popup first
        if self._close_others_cb is not None:
            self._close_others_cb(self)

        # FramelessWindowHint + Tool: stays open, doesn't steal focus,
        # won't auto-close on outside click — we manage close ourselves.
        popup = QFrame(None,
                       Qt.WindowType.Tool |
                       Qt.WindowType.FramelessWindowHint |
                       Qt.WindowType.NoDropShadowWindowHint)
        popup.setObjectName("sc_combo_popup")
        popup.setFrameShape(QFrame.Shape.StyledPanel)
        self._popup = popup
        self._popup_lay = QVBoxLayout(popup)
        self._popup_lay.setContentsMargins(2, 2, 2, 2)
        self._popup_lay.setSpacing(1)
        self._rebuild_popup_rows()

        gp = self._frame.mapToGlobal(self._frame.rect().bottomLeft())
        popup.move(gp)
        popup.setFixedWidth(max(self._frame.width(), 150))
        popup.show()
        popup.adjustSize()

    def _rebuild_popup_rows(self):
        if not self._popup:
            return
        # clear existing rows
        lay = self._popup_lay
        while lay.count():
            item = lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        for idx, name in enumerate(self._items):
            row = QWidget()
            row.setObjectName("sc_combo_row")
            rl = QHBoxLayout(row)
            rl.setContentsMargins(4, 1, 2, 1)
            rl.setSpacing(4)

            lbl = QPushButton(name)
            lbl.setObjectName("sc_combo_row_lbl")
            lbl.setFlat(True)
            lbl.setCursor(Qt.CursorShape.PointingHandCursor)
            lbl.setSizePolicy(QSizePolicy.Policy.Expanding,
                              QSizePolicy.Policy.Fixed)
            lbl.clicked.connect(
                lambda checked=False, i=idx: self._select(i))
            rl.addWidget(lbl)

            if name not in self._PROTECTED:
                rem = QPushButton("−")
                rem.setObjectName("sc_combo_row_rem")
                rem.setFixedSize(20, 20)
                rem.setCursor(Qt.CursorShape.PointingHandCursor)
                rem.setToolTip(f"Remove '{name}'")
                rem.clicked.connect(
                    lambda checked=False, i=idx: self._remove_from_popup(i))
                rl.addWidget(rem)
            else:
                spacer = QWidget()
                spacer.setFixedSize(20, 20)
                rl.addWidget(spacer)

            lay.addWidget(row)

        if self._popup:
            self._popup.adjustSize()

    def _select(self, index: int):
        self._close_popup()
        old = self._current
        self._current = index
        self._refresh_display()
        if old != self._current:
            self.currentIndexChanged.emit(self._current)

    def _remove_from_popup(self, index: int):
        name = self._items[index] if 0 <= index < len(self._items) else ""
        if name in self._PROTECTED:
            return
        # remove item without closing popup
        self._items.pop(index)
        if self._current >= len(self._items):
            self._current = max(0, len(self._items) - 1)
        self._refresh_display()
        self.currentIndexChanged.emit(self._current)
        # rebuild popup rows in place — stays open
        self._rebuild_popup_rows()
        if self._toast_cb and name:
            self._toast_cb(f"Variable '{name}' removed", "info")

    def hideEvent(self, event):
        self._close_popup()
        super().hideEvent(event)


# ═══════════════════════════════════════════════════════════════════════════════
#  ELF VARIABLE PICKER DIALOG
# ═══════════════════════════════════════════════════════════════════════════════

class _ElfVarPickerDialog(QDialog):
    """
    Panel shown when the user clicks [+] on a channel.
    Each variable row has its name and a [+] button.
    Clicking [+] adds the variable to the channel combo immediately and
    turns the button into [−]. Clicking [−] removes it.
    Dialog stays open so the user can add/remove multiple variables.
    """

    def __init__(self, parent, ch_idx: int, all_names: list[str], combo: "QComboBox",
                 toast_cb=None):
        super().__init__(parent)
        self.setWindowTitle(f"Variables — Ch{ch_idx + 1}")
        self.setMinimumSize(300, 460)
        self.resize(300, 460)
        self._all   = all_names
        self._combo = combo
        self._ch_idx = ch_idx
        self._toast_cb = toast_cb  # optional callable(message, level)
        # track which names were added during this session (short_name -> row_btn)
        self._row_btns: dict[str, QPushButton] = {}
        self._build()

    def _build(self):
        from PySide6.QtWidgets import QLineEdit, QScrollArea as _SA

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        # search bar
        self._le = QLineEdit()
        self._le.setObjectName("sc_combo")
        self._le.setPlaceholderText("Search variable…")
        self._le.textChanged.connect(self._filter)
        root.addWidget(self._le)

        # scrollable rows container
        self._rows_widget = QWidget()
        self._rows_widget.setObjectName("sc_tag_area")
        self._rows_layout = QVBoxLayout(self._rows_widget)
        self._rows_layout.setContentsMargins(4, 4, 4, 4)
        self._rows_layout.setSpacing(3)
        self._rows_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        sa = _SA()
        sa.setObjectName("sc_tag_scroll")
        sa.setWidgetResizable(True)
        sa.setWidget(self._rows_widget)
        sa.setFrameShape(QFrame.Shape.NoFrame)
        root.addWidget(sa, 1)

        self._build_rows(self._all)

        # close button
        close_btn = QPushButton("Done")
        close_btn.setObjectName("sc_btn_primary")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.accept)
        root.addWidget(close_btn)

    def _build_rows(self, names: list[str]):
        # clear existing rows
        while self._rows_layout.count():
            item = self._rows_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._row_btns.clear()

        # current combo items (excluding default VARIABLE_CODES keys)
        combo_items = [self._combo.itemText(i) for i in range(self._combo.count())]

        for name in names:
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(6)

            lbl = QLabel(name)
            lbl.setObjectName("sc_ch_name")
            row.addWidget(lbl, 1)

            already_in = name in combo_items
            btn = QPushButton("−" if already_in else "+")
            btn.setObjectName("sc_btn_elf_minus" if already_in else "sc_btn_elf_plus")
            btn.setFixedSize(24, 24)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda checked=False, n=name, b=btn: self._toggle(n, b))
            row.addWidget(btn)

            container = QWidget()
            container.setLayout(row)
            self._rows_layout.addWidget(container)
            self._row_btns[name] = btn

    def _filter(self, text: str):
        q = text.strip().lower()
        filtered = [n for n in self._all if q in n.lower()] if q else self._all
        self._build_rows(filtered)

    def _toggle(self, name: str, btn: "QPushButton"):
        combo_items = [self._combo.itemText(i) for i in range(self._combo.count())]
        if name in combo_items:
            # remove from combo
            idx = self._combo.findText(name)
            if idx >= 0:
                self._combo.removeItem(idx)
            btn.setText("+")
            btn.setObjectName("sc_btn_elf_plus")
            btn.style().unpolish(btn)
            btn.style().polish(btn)
            if self._toast_cb:
                self._toast_cb(f"Variable '{name}' removed from Ch{self._ch_idx + 1}", "info")
        else:
            # add to combo
            self._combo.addItem(name)
            btn.setText("−")
            btn.setObjectName("sc_btn_elf_minus")
            btn.style().unpolish(btn)
            btn.style().polish(btn)
            if self._toast_cb:
                self._toast_cb(f"Variable '{name}' added to Ch{self._ch_idx + 1}", "ok")


# ═══════════════════════════════════════════════════════════════════════════════
#  SCOPE WINDOW
# ═══════════════════════════════════════════════════════════════════════════════

class ScopeWindow(QDialog):

    _sig_status         = Signal(str)
    _sig_bytes          = Signal(int, int)
    _sig_update_buttons = Signal()
    _sig_stop_spinner   = Signal()
    _sig_plot           = Signal(object, object, object)   # ch_data, t_axis, cfg
    _sig_show_warning   = Signal(str, str)                 # title, message
    _sig_elf_loaded     = Signal(int)                      # count of vars loaded
    _sig_elf_scanning   = Signal()                         # folder scan started

    def __init__(self, parent, serial_manager, fpwm=16000.0):
        super().__init__(parent)
        self.setObjectName("sc_dialog")
        self.setWindowTitle("Oscilloscope / Scope")
        self.setMinimumSize(560, 560)
        self.resize(680, 660)

        self.serial_manager = serial_manager
        self.fpwm = fpwm

        self.is_configured        = False
        self.last_config          = None
        self._updating_auto       = False
        self._configuring         = False
        self._realtime_running    = False
        self._scroll_running      = False
        self._scroll_rings        = None
        self._scroll_t_ring       = None
        self._scroll_t0           = 0.0
        self._scroll_t_display    = 1.0
        self._scroll_frame_count  = 0
        self._scroll_lock         = threading.Lock()
        self._scroll_rechalf_val  = 0
        self._scroll_poll_timer   = None
        self._scroll_display_timer= None
        self._last_plot_data      = None
        self._has_plot_data       = False
        self._ylim_locked         = None
        self._no_port_timer       = None
        self._no_port_pulse       = 0
        self._is_maximized        = False
        self._restore_geometry    = None
        self._crosshair_v         = None  # vertical crosshair line (blitted)
        # A/B measurement cursors
        self._ab_mode             = False
        self._cursor_a            = None  # (x, y) of cursor A
        self._cursor_b            = None  # (x, y) of cursor B
        self._cursor_a_line       = None  # vline artist
        self._cursor_b_line       = None  # vline artist
        # Trigger
        self._trigger_enabled     = False
        self._pan_release_cid     = None
        self._leg_pick_cid        = None
        self._leg_line_map        = {}
        self._plotted_lines       = {}
        self._legend_obj          = None
        # RT rolling buffer (pre-allocated numpy arrays)
        self._rt_buf_data         = None   # list of 4 np.ndarray
        self._rt_buf_time         = None   # np.ndarray of timestamps (seconds)
        self._rt_buf_cap          = 0      # total capacity (samples)
        self._rt_buf_head         = 0      # next write position
        self._rt_buf_count        = 0      # samples stored so far
        self._rt_buf_cfg          = None   # config used to build buffer
        self._rt_panned           = False  # True when user has panned away from live
        self._rt_t0               = None   # wall-clock time of first RT sample
        # Data range for zoom-out clamping
        self._data_xlim           = None
        self._data_ylim           = None
        # Right-click pan state (pixel-based — no drift)
        self._pan_active          = False
        self._pan_start_px        = None   # (x_px, y_px) at drag start
        self._pan_start_xlim      = None
        self._pan_start_ylim      = None
        self._pan_motion_cid      = None
        # Blit background for smooth crosshair
        self._blit_bg             = None
        # Zoom debounce timer
        # Live values (RT mode)
        self._rt_last_values      = {}   # ch_idx -> last value

        self._sig_status.connect(self._slot_set_status)
        self._sig_bytes.connect(self._slot_set_bytes)
        self._sig_update_buttons.connect(self._update_button_states)
        self._sig_stop_spinner.connect(self._stop_configure_spinner)
        self._sig_plot.connect(self._do_plot)
        self._sig_show_warning.connect(self._slot_show_warning)
        self._sig_elf_loaded.connect(self._on_elf_loaded_slot)
        self._sig_elf_scanning.connect(self._on_elf_scanning_slot)
        self._elf_spinner_timer: QTimer | None = None
        self._elf_spinner_step  = 0
        self._elf_banner: "QFrame | None" = None

        self._build_ui()
        self._apply_style()
        self._update_button_states()
        self._update_sample_counter()
        self._install_shortcuts()
        self._load_session_config()

    def _install_shortcuts(self):
        QShortcut(QKeySequence("Ctrl+G"), self).activated.connect(self._on_configure_clicked)
        QShortcut(QKeySequence("Ctrl+R"), self).activated.connect(self._on_realtime_clicked)
        QShortcut(QKeySequence("Ctrl+S"), self).activated.connect(self._on_scroll_clicked)
        QShortcut(QKeySequence("Ctrl+E"), self).activated.connect(self._on_export_clicked)
        QShortcut(QKeySequence("Ctrl+Shift+D"), self).activated.connect(self._on_dark_clicked)
        QShortcut(QKeySequence("Ctrl+M"), self).activated.connect(self._on_compact_clicked)

    # ══════════════════════════════════════════════════════════════════════════
    #  UI CONSTRUCTION
    # ══════════════════════════════════════════════════════════════════════════

    def _build_ui(self):
        from PySide6.QtWidgets import QGridLayout, QSizePolicy
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 8)
        root.setSpacing(4)

        var_keys = list(VARIABLE_CODES.keys())
        from PySide6.QtCore import QSize

        # ══════════════════════════════════════════════════════════════════════
        # CONTROL PANEL — full-width top section
        # ══════════════════════════════════════════════════════════════════════
        panel = QFrame()
        panel.setObjectName("sc_panel")
        panel_lay = QVBoxLayout(panel)
        panel_lay.setContentsMargins(10, 4, 10, 4)
        panel_lay.setSpacing(3)

        # ── CHANNELS section header — tiny icon buttons at top-right ─────────
        ch_hdr = QHBoxLayout()
        ch_hdr.setContentsMargins(0, 0, 0, 0)
        ch_section_title = QLabel("CHANNELS")
        ch_section_title.setObjectName("sc_section_title")
        ch_hdr.addWidget(ch_section_title)
        ch_hdr.addStretch(1)

        # ELF load button — one-time action, sits in the header
        self._btn_elf_load = QPushButton()
        self._btn_elf_load.setObjectName("sc_btn_elf")
        self._btn_elf_load.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_elf_load.setToolTip(
            "Load an ELF / project folder once to unlock variable names\n"
            "from your STM32 firmware. Click [+] on any channel to add them.")
        self._btn_elf_load.setFixedSize(34, 30)
        self._btn_elf_load.setIcon(_make_elf_icon(30, self._is_dark(_get_palette())))
        self._btn_elf_load.setIconSize(QSize(28, 28))
        self._btn_elf_load.clicked.connect(self._on_elf_load)
        ch_hdr.addWidget(self._btn_elf_load)

        self._btn_dark = QPushButton()
        self._btn_dark.setObjectName("sc_btn_compact")
        self._btn_dark.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_dark.setToolTip("Toggle dark / light mode  [Ctrl+Shift+D]")
        self._btn_dark.setFixedSize(20, 20)
        self._btn_dark.setIcon(_make_theme_icon(dark_mode=False))
        self._btn_dark.setIconSize(QSize(13, 13))
        self._btn_dark.clicked.connect(self._on_dark_clicked)
        ch_hdr.addWidget(self._btn_dark)
        self._btn_compact = QPushButton()
        self._btn_compact.setObjectName("sc_btn_compact")
        self._btn_compact.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_compact.setToolTip("Maximize / Restore  [Ctrl+M]")
        self._btn_compact.setFixedSize(20, 20)
        self._btn_compact.setIcon(_make_maximize_icon("#555555"))
        self._btn_compact.setIconSize(QSize(13, 13))
        self._btn_compact.clicked.connect(self._on_compact_clicked)
        ch_hdr.addWidget(self._btn_compact)
        panel_lay.addLayout(ch_hdr)

        # [+] button references so we can enable them after ELF load
        self._ch_plus_btns: list[QPushButton] = []

        ch_grid_widget = QFrame()
        ch_grid_widget.setObjectName("sc_ch_grid")

        from PySide6.QtWidgets import QGridLayout as _QGL
        ch_grid = _QGL(ch_grid_widget)
        ch_grid.setContentsMargins(0, 2, 0, 2)
        ch_grid.setHorizontalSpacing(12)
        ch_grid.setVerticalSpacing(6)

        self._ch_combos = []
        self._ch_dots: list[QLabel] = []

        for i in range(4):
            row_idx  = i // 2
            # cols per pair: dot(0) | name(1) | combo(2) | [+](3) | gap(4)
            col_base = (i % 2) * 5

            # color dot
            dot = QLabel("●")
            dot.setObjectName(f"sc_ch_dot_{i}")
            self._ch_dots.append(dot)
            dot.setFixedWidth(14)
            ch_grid.addWidget(dot, row_idx, col_base,
                              Qt.AlignmentFlag.AlignVCenter)

            # name label
            name_lbl = QLabel(f"Ch{i+1}")
            name_lbl.setObjectName("sc_ch_name")
            name_lbl.setFixedWidth(28)
            ch_grid.addWidget(name_lbl, row_idx, col_base + 1,
                              Qt.AlignmentFlag.AlignVCenter)

            # custom combo (identical look to QComboBox, popup has [−] per row)
            combo = _ChannelCombo()
            combo.addItems(var_keys)
            combo.setCurrentIndex(0)
            combo.setToolTip("Select variable — open to remove ELF entries")
            self._ch_combos.append(combo)
            ch_grid.addWidget(combo, row_idx, col_base + 2)

            # [+] open ELF variable picker
            plus_btn = QPushButton("+")
            plus_btn.setObjectName("sc_btn_elf_plus")
            plus_btn.setFixedSize(22, 22)
            plus_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            plus_btn.setToolTip("Add ELF variable to this channel  (load ELF first)")
            plus_btn.setEnabled(False)
            plus_btn.clicked.connect(
                lambda checked=False, ci=i: self._on_ch_plus(ci))
            ch_grid.addWidget(plus_btn, row_idx, col_base + 3,
                              Qt.AlignmentFlag.AlignVCenter)
            self._ch_plus_btns.append(plus_btn)

            # gap between the two pairs
            if i % 2 == 0:
                ch_grid.setColumnMinimumWidth(col_base + 4, 16)

        # both combo columns stretch equally
        ch_grid.setColumnStretch(2, 1)
        ch_grid.setColumnStretch(7, 1)

        panel_lay.addWidget(ch_grid_widget)

        # ── ELF status banner (hidden until ELF loaded / scanning) ──
        self._elf_banner = QFrame()
        self._elf_banner.setObjectName("sc_elf_banner")
        bl = QHBoxLayout(self._elf_banner)
        bl.setContentsMargins(8, 3, 8, 3)
        bl.setSpacing(6)
        self._elf_banner_icon = QLabel("●")
        self._elf_banner_icon.setObjectName("sc_elf_banner_icon")
        self._elf_banner_lbl  = QLabel("")
        self._elf_banner_lbl.setObjectName("sc_elf_banner_lbl")
        bl.addWidget(self._elf_banner_icon)
        bl.addWidget(self._elf_banner_lbl, 1)
        self._elf_banner.setVisible(False)
        panel_lay.addWidget(self._elf_banner)

        # connect signals after all combos built
        for combo in self._ch_combos:
            combo.currentIndexChanged.connect(self._on_config_changed)
            combo.currentIndexChanged.connect(lambda _: self._update_sample_counter())
        # Enforce singleton popup: closing other combos when one opens
        def _close_others(sender):
            for cb in self._ch_combos:
                if cb is not sender:
                    cb._close_popup()
        # Wire up toast for inline remove + singleton enforcement
        main_win = self.parent()
        _toast = getattr(main_win, '_show_toast', None) if main_win is not None else None
        for combo in self._ch_combos:
            combo._close_others_cb = _close_others
            combo._toast_cb = _toast

        # set initial dot colors
        self._update_dot_colors()

        # thin horizontal rule
        hdiv1 = QFrame()
        hdiv1.setObjectName("sc_hdiv")
        hdiv1.setFrameShape(QFrame.Shape.HLine)
        panel_lay.addWidget(hdiv1)

        # ── RECORDING section: 3 params in a single compact row ───────────────
        rec_title = QLabel("RECORDING")
        rec_title.setObjectName("sc_section_title")
        panel_lay.addWidget(rec_title)

        rec_inputs = QHBoxLayout()
        rec_inputs.setSpacing(14)

        def _input_group(label, spinbox, checkbox=None):
            col = QVBoxLayout()
            col.setSpacing(2)
            lbl = QLabel(label)
            lbl.setObjectName("sc_input_label")
            col.addWidget(lbl)
            row = QHBoxLayout()
            row.setSpacing(4)
            row.addWidget(spinbox)
            if checkbox:
                row.addWidget(checkbox)
            col.addLayout(row)
            return col

        self._spin_rectime = QDoubleSpinBox()
        self._spin_rectime.setObjectName("sc_spinbox")
        self._spin_rectime.setRange(1.0, 100000.0)
        self._spin_rectime.setDecimals(1)
        self._spin_rectime.setValue(20.0)
        self._spin_rectime.setMinimumWidth(80)
        self._spin_rectime.setCursor(Qt.CursorShape.PointingHandCursor)
        self._spin_rectime.valueChanged.connect(self._on_config_changed)
        self._spin_rectime.valueChanged.connect(self._on_rectime_changed)
        self._spin_rectime.valueChanged.connect(lambda _: self._update_sample_counter())
        self._spin_rectime.setKeyboardTracking(False)
        self._spin_rectime.lineEdit().returnPressed.connect(self._spin_rectime.editingFinished.emit)
        _apply_mono(self._spin_rectime)
        self._chk_rectime_max = QCheckBox("Max")
        self._chk_rectime_max.setObjectName("sc_checkbox")
        self._chk_rectime_max.setCursor(Qt.CursorShape.PointingHandCursor)
        self._chk_rectime_max.toggled.connect(self._on_rectime_max_toggled)
        rec_inputs.addLayout(_input_group("Rec [ms]", self._spin_rectime, self._chk_rectime_max))

        self._spin_samplefreq = QDoubleSpinBox()
        self._spin_samplefreq.setObjectName("sc_spinbox")
        self._spin_samplefreq.setRange(1.0, self.fpwm)
        self._spin_samplefreq.setDecimals(1)
        self._spin_samplefreq.setValue(self.fpwm)
        self._spin_samplefreq.setMinimumWidth(80)
        self._spin_samplefreq.setCursor(Qt.CursorShape.PointingHandCursor)
        self._spin_samplefreq.valueChanged.connect(self._on_config_changed)
        self._spin_samplefreq.valueChanged.connect(self._on_samplefreq_changed)
        self._spin_samplefreq.valueChanged.connect(lambda _: self._update_sample_counter())
        self._spin_samplefreq.setKeyboardTracking(False)
        self._spin_samplefreq.lineEdit().returnPressed.connect(self._spin_samplefreq.editingFinished.emit)
        _apply_mono(self._spin_samplefreq)
        self._chk_samplefreq_max = QCheckBox("Max")
        self._chk_samplefreq_max.setObjectName("sc_checkbox")
        self._chk_samplefreq_max.setCursor(Qt.CursorShape.PointingHandCursor)
        self._chk_samplefreq_max.toggled.connect(self._on_samplefreq_max_toggled)
        rec_inputs.addLayout(_input_group("Freq [Hz]", self._spin_samplefreq, self._chk_samplefreq_max))

        self._spin_tdisplay = QDoubleSpinBox()
        self._spin_tdisplay.setObjectName("sc_spinbox")
        self._spin_tdisplay.setRange(0.1, 3600.0)
        self._spin_tdisplay.setSingleStep(0.5)
        self._spin_tdisplay.setDecimals(1)
        self._spin_tdisplay.setValue(1.0)
        self._spin_tdisplay.setMinimumWidth(70)
        self._spin_tdisplay.setCursor(Qt.CursorShape.PointingHandCursor)
        self._spin_tdisplay.valueChanged.connect(self._on_tdisplay_change)
        self._spin_tdisplay.setKeyboardTracking(False)
        self._spin_tdisplay.lineEdit().returnPressed.connect(self._spin_tdisplay.editingFinished.emit)
        _apply_mono(self._spin_tdisplay)
        self._chk_ylock = QCheckBox("Lock Y")
        self._chk_ylock.setObjectName("sc_checkbox")
        self._chk_ylock.setCursor(Qt.CursorShape.PointingHandCursor)
        self._chk_ylock.setToolTip("Freeze Y-axis range at current limits")
        self._chk_ylock.toggled.connect(self._on_ylock_toggled)
        rec_inputs.addLayout(_input_group("Win [s]", self._spin_tdisplay, self._chk_ylock))

        # samples counter (inline, right side)
        samples_col = QVBoxLayout()
        samples_col.setSpacing(2)
        samples_col.addWidget(QLabel("Samples"))
        self._lbl_samples = QLabel("—")
        self._lbl_samples.setObjectName("sc_samples_ok")
        _apply_mono(self._lbl_samples)
        samples_col.addWidget(self._lbl_samples)
        rec_inputs.addLayout(samples_col)

        rec_inputs.addStretch(1)
        panel_lay.addLayout(rec_inputs)

        # thin horizontal rule
        hdiv2 = QFrame()
        hdiv2.setObjectName("sc_hdiv")
        hdiv2.setFrameShape(QFrame.Shape.HLine)
        panel_lay.addWidget(hdiv2)

        # ── ACTION BUTTONS row ────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.setSpacing(5)

        self._btn_configure = QPushButton("Configure")
        self._btn_configure.setObjectName("sc_btn_primary")
        self._btn_configure.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_configure.setFixedHeight(30)
        self._btn_configure.setToolTip(
            "Configure scope channels  [Ctrl+G]\n"
            "1. Set Ch1-Ch4 to the variable you want to capture\n"
            "2. Set Rec [ms], Freq [Hz], Win [s]\n"
            "3. Click Configure -- then use Single Shot or Real Time"
        )
        self._btn_configure.clicked.connect(self._on_configure_clicked)
        btn_row.addWidget(self._btn_configure)

        self._btn_single = QPushButton("Single Shot")
        self._btn_single.setObjectName("sc_btn_outline")
        self._btn_single.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_single.setFixedHeight(30)
        self._btn_single.setToolTip("Single-shot recording")
        self._btn_single.clicked.connect(self._on_single_clicked)
        btn_row.addWidget(self._btn_single)

        self._btn_realtime = QPushButton("Real Time ▸")
        self._btn_realtime.setObjectName("sc_btn_outline")
        self._btn_realtime.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_realtime.setFixedHeight(30)
        self._btn_realtime.setToolTip("Continuous real-time mode  [Ctrl+R]")
        self._btn_realtime.clicked.connect(self._on_realtime_clicked)
        btn_row.addWidget(self._btn_realtime)

        self._btn_scroll = QPushButton("Scroll ▸")
        self._btn_scroll.setObjectName("sc_btn_outline")
        self._btn_scroll.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_scroll.setFixedHeight(30)
        self._btn_scroll.setToolTip("Continuous scroll mode  [Ctrl+S]")
        self._btn_scroll.clicked.connect(self._on_scroll_clicked)
        btn_row.addWidget(self._btn_scroll)

        self._btn_export = QPushButton("Export…")
        self._btn_export.setObjectName("sc_btn_outline")
        self._btn_export.setEnabled(False)
        self._btn_export.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_export.setFixedHeight(30)
        self._btn_export.setToolTip("Export waveform as CSV or PNG  [Ctrl+E]")
        self._btn_export.clicked.connect(self._on_export_clicked)
        btn_row.addWidget(self._btn_export)

        btn_row.addStretch(1)

        # ── Tool buttons: Cursors | Zoom ─────────────────────────────────────
        self._btn_ab = QPushButton("Cursors: OFF")
        self._btn_ab.setObjectName("sc_btn_tool")
        self._btn_ab.setCheckable(True)
        self._btn_ab.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_ab.setFixedHeight(30)
        self._btn_ab.setToolTip(
            "Measurement cursors — click graph to place marker A, click again for marker B\n"
            "Time gap (DT) and value gap (DY) between A and B appear in the bar below the graph\n"
            "Scroll wheel on graph always zooms in/out centered on cursor"
        )
        self._btn_ab.clicked.connect(self._on_ab_toggled)
        btn_row.addWidget(self._btn_ab)

        panel_lay.addLayout(btn_row)

        # ── TRIGGER row (below action buttons) ───────────────────────────────
        trig_row = QHBoxLayout()
        trig_row.setSpacing(8)
        trig_row.setContentsMargins(0, 0, 0, 0)

        self._chk_trigger = QCheckBox("Trigger")
        self._chk_trigger.setObjectName("sc_checkbox")
        self._chk_trigger.setCursor(Qt.CursorShape.PointingHandCursor)
        self._chk_trigger.setToolTip(
            "Enable trigger: capture only fires when the selected channel\n"
            "crosses the threshold in the selected direction"
        )
        self._chk_trigger.toggled.connect(self._on_trigger_toggled)
        trig_row.addWidget(self._chk_trigger)

        self._combo_trig_ch = QComboBox()
        self._combo_trig_ch.setObjectName("sc_combo")
        self._combo_trig_ch.addItems(["Ch1", "Ch2", "Ch3", "Ch4"])
        self._combo_trig_ch.setFixedWidth(90)
        self._combo_trig_ch.setCursor(Qt.CursorShape.PointingHandCursor)
        self._combo_trig_ch.setToolTip("Enable the Trigger checkbox to configure")
        self._combo_trig_ch.setEnabled(False)
        trig_row.addWidget(self._combo_trig_ch)

        self._combo_trig_edge = QComboBox()
        self._combo_trig_edge.setObjectName("sc_combo")
        self._combo_trig_edge.addItems(["Rising ▲", "Falling ▼"])
        self._combo_trig_edge.setFixedWidth(100)
        self._combo_trig_edge.setCursor(Qt.CursorShape.PointingHandCursor)
        self._combo_trig_edge.setToolTip("Enable the Trigger checkbox to configure")
        self._combo_trig_edge.setEnabled(False)
        trig_row.addWidget(self._combo_trig_edge)

        trig_lbl = QLabel("Level:")
        trig_lbl.setObjectName("sc_input_label")
        trig_row.addWidget(trig_lbl)

        self._spin_trig_level = QDoubleSpinBox()
        self._spin_trig_level.setObjectName("sc_spinbox")
        self._spin_trig_level.setRange(-99999.0, 99999.0)
        self._spin_trig_level.setDecimals(2)
        self._spin_trig_level.setValue(0.0)
        self._spin_trig_level.setFixedWidth(80)
        self._spin_trig_level.setCursor(Qt.CursorShape.PointingHandCursor)
        self._spin_trig_level.setEnabled(False)
        self._spin_trig_level.setToolTip("Threshold value — capture fires when channel crosses this level")
        self._spin_trig_level.setKeyboardTracking(False)
        self._spin_trig_level.lineEdit().returnPressed.connect(self._spin_trig_level.editingFinished.emit)
        _apply_mono(self._spin_trig_level)
        trig_row.addWidget(self._spin_trig_level)

        trig_row.addStretch(1)
        panel_lay.addLayout(trig_row)

        # ── Status strip: pill | status text | bytes | fpwm ──────────────────
        status_row = QHBoxLayout()
        status_row.setSpacing(6)
        status_row.setContentsMargins(0, 0, 0, 0)

        self._lbl_status_pill = QLabel("IDLE")
        self._lbl_status_pill.setObjectName("sc_pill_idle")
        self._lbl_status_pill.setFixedHeight(20)
        status_row.addWidget(self._lbl_status_pill)

        self._lbl_status = QLabel("Ready — select channels and configure")
        self._lbl_status.setObjectName("sc_status_label")
        self._lbl_status.setWordWrap(False)
        self._lbl_status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        status_row.addWidget(self._lbl_status, 1)

        self._lbl_bytes = QLabel("Bytes: -- / --")
        self._lbl_bytes.setObjectName("sc_telemetry")
        _apply_mono(self._lbl_bytes)
        status_row.addWidget(self._lbl_bytes)

        self._lbl_fpwm = QLabel(f"Fpwm: {self.fpwm:.0f} Hz" if self.fpwm > 0 else "Fpwm: —")
        self._lbl_fpwm.setObjectName("sc_telemetry")
        _apply_mono(self._lbl_fpwm)
        status_row.addWidget(self._lbl_fpwm)

        # no-port warning (inline, hidden by default)
        self._no_port_frame = QFrame()
        self._no_port_frame.setObjectName("sc_no_port_frame")
        np_lay = QHBoxLayout(self._no_port_frame)
        np_lay.setContentsMargins(8, 3, 8, 3)
        np_lay.setSpacing(6)
        self._no_port_icon = QLabel("●")
        self._no_port_icon.setObjectName("sc_no_port_icon")
        np_lay.addWidget(self._no_port_icon)
        self._no_port_text_lbl = QLabel("Not connected — open a serial port in the main interface")
        self._no_port_text_lbl.setObjectName("sc_no_port_text")
        np_lay.addWidget(self._no_port_text_lbl)
        self._no_port_frame.setVisible(False)
        status_row.addWidget(self._no_port_frame)

        panel_lay.addLayout(status_row)
        root.addWidget(panel)

        # ══════════════════════════════════════════════════════════════════════
        # GRAPH PANEL — tall, dominant
        # ══════════════════════════════════════════════════════════════════════
        graph_frame = QFrame()
        graph_frame.setObjectName("sc_graph_frame")
        graph_lay = QVBoxLayout(graph_frame)
        graph_lay.setContentsMargins(0, 0, 0, 0)
        graph_lay.setSpacing(0)

        self.fig = Figure(dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.canvas = FigureCanvasQTAgg(self.fig)
        self.canvas.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.canvas.setCursor(Qt.CursorShape.ArrowCursor)
        self.canvas.mpl_connect('resize_event', self._on_canvas_resize)
        self.canvas.mpl_connect('motion_notify_event', self._on_mouse_move)
        self.canvas.mpl_connect('button_press_event', self._on_canvas_click)
        self.canvas.mpl_connect('scroll_event', self._on_scroll_zoom)
        self._pan_release_cid = self.canvas.mpl_connect('button_release_event', self._on_pan_release)
        self._pan_motion_cid  = self.canvas.mpl_connect('motion_notify_event', self._on_pan_motion)
        graph_lay.addWidget(self.canvas, 1)

        # ── Live values strip (visible during RT and scroll modes) ───────────
        self._live_strip = QFrame()
        self._live_strip.setObjectName("sc_live_strip")
        live_lay = QHBoxLayout(self._live_strip)
        live_lay.setContentsMargins(8, 2, 8, 2)
        live_lay.setSpacing(16)
        self._live_labels = []
        for i in range(4):
            lbl = QLabel(f"Ch{i+1}: —")
            lbl.setObjectName("sc_live_val")
            lbl.setStyleSheet(f"color: {CHANNEL_COLORS[i]};")
            _apply_mono(lbl)
            live_lay.addWidget(lbl)
            self._live_labels.append(lbl)
        live_lay.addStretch(1)
        self._lbl_live_badge = QLabel("● LIVE")
        self._lbl_live_badge.setObjectName("sc_live_badge_live")
        _apply_mono(self._lbl_live_badge)
        live_lay.addWidget(self._lbl_live_badge)
        self._live_strip.setVisible(False)
        graph_lay.addWidget(self._live_strip)

        self._lbl_coords = QLabel("")
        self._lbl_coords.setObjectName("sc_coords_label")
        self._lbl_coords.setAlignment(Qt.AlignmentFlag.AlignLeft)
        _apply_mono(self._lbl_coords)
        graph_lay.addWidget(self._lbl_coords)

        root.addWidget(graph_frame, 4)

        self._scroll_lines = []
        self._scroll_bg    = None

        self._init_empty_plot()

    def _init_empty_plot(self):
        p = _get_palette()
        dark = self._is_dark(p)
        grid_color = "#3A3A5C" if dark else "#E8EAF0"
        self.ax.cla()
        self.ax.set_facecolor(p['input_bg'])
        self.fig.patch.set_facecolor(p['card'])
        self.ax.set_xlabel("Time (s)", color=p['muted'], fontsize=9, fontweight='semibold', labelpad=2)
        self.ax.set_ylabel("Amplitude", color=p['muted'], fontsize=9, fontweight='semibold', labelpad=2)
        self.ax.tick_params(colors=p['muted'], labelsize=8)
        for spine in self.ax.spines.values():
            spine.set_color(p['border'])
            spine.set_linewidth(0.8)
        self.ax.grid(True, which='major', color=grid_color, linewidth=0.8, alpha=0.9, linestyle='--')
        self.ax.minorticks_on()
        self.ax.grid(True, which='minor', color=grid_color, linewidth=0.3, alpha=0.4, linestyle=':')
        self.ax.text(
            0.5, 0.5,
            "No data",
            transform=self.ax.transAxes,
            ha='center', va='center',
            fontsize=11, color=p['faint'],
            style='italic',
        )
        try:
            self.fig.tight_layout(pad=0.2)
        except Exception:
            pass
        self.canvas.draw()

    # ══════════════════════════════════════════════════════════════════════════
    #  STYLING
    # ══════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _is_dark(p):
        try:
            h = p['bg'].lstrip('#')
            r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
            return (0.299 * r + 0.587 * g + 0.114 * b) < 128
        except Exception:
            return False

    def _apply_style(self):
        p = _get_palette()
        dark = self._is_dark(p)

        RED       = p.get('red',      '#B71C1C')
        RED_DARK  = p.get('red_dark', '#7F1212')
        RED_BG    = p.get('red_bg',   '#FEECEC')
        RED_BDR   = p.get('red_border','#F5BABA')

        CARD      = p['card']
        BG        = p['bg']
        BORDER    = p['border']
        TEXT      = p['text']
        TEXT2     = p['text2']
        MUTED     = p['muted']
        FAINT     = p['faint']
        INPUT_BG  = p['input_bg']
        WHITE     = p['white']

        # popup-specific colors — Tool windows don't inherit #sc_dialog scope
        POPUP_TEXT   = "#E8EAF0" if dark else TEXT
        POPUP_CARD   = p.get('log_bg', '#12122A') if dark else CARD
        POPUP_BORDER = "#3D3D5C" if dark else BORDER
        POPUP_HOVER  = "#2A2A44" if dark else RED_BG

        # generate ▲▼ arrow image for combo drop-down
        arrow_color = "#888888" if not dark else "#AAAAAA"
        _arrow_path = _make_updown_arrow_path(arrow_color, size=12)

        # graph grid
        grid_color = "#3A3A5C" if dark else "#E8EAF0"

        # per-channel dot color rules
        dot_css = ""
        for i, col in enumerate(CHANNEL_COLORS):
            dot_css += f"#sc_dialog QLabel#sc_ch_dot_{i} {{ color: {col}; font-size: 14px; background: transparent; }}\n"

        qss = f"""
/* ── Root ────────────────────────────────────────────────────── */
#sc_dialog {{
    background: {BG};
    font-family: "Segoe UI", "Inter", system-ui, sans-serif;
}}
#sc_dialog QLabel {{
    background: transparent;
    color: {TEXT};
    font-family: "Segoe UI", "Inter", system-ui, sans-serif;
}}

/* ── Title ───────────────────────────────────────────────────── */
#sc_dialog QLabel#sc_title {{
    font-size: 14px;
    font-weight: 700;
    color: {TEXT};
    letter-spacing: 0.2px;
    background: transparent;
}}

/* ── Compact button ──────────────────────────────────────────── */
#sc_dialog QPushButton#sc_btn_compact {{
    background: {INPUT_BG};
    color: {TEXT2};
    border: 1px solid {BORDER};
    border-radius: 4px;
    font-size: 13px;
    font-weight: 400;
    padding: 0px 4px;
}}
#sc_dialog QPushButton#sc_btn_compact:hover {{
    background: {RED};
    border-color: {RED};
    color: white;
}}
#sc_dialog QPushButton#sc_btn_compact:pressed {{
    background: #B71C1C;
    border-color: #B71C1C;
    color: white;
}}


/* ── Control panel card ──────────────────────────────────────── */
#sc_dialog QFrame#sc_panel {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-top: 2px solid {RED};
    border-radius: 8px;
}}

/* ── Section titles ──────────────────────────────────────────── */
#sc_dialog QLabel#sc_section_title {{
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.8px;
    color: {MUTED};
    background: transparent;
}}

/* ── Input labels (above spinboxes) ─────────────────────────── */
#sc_dialog QLabel#sc_input_label {{
    font-size: 10px;
    font-weight: 600;
    color: {TEXT2};
    background: transparent;
}}

/* ── Channel grid container ──────────────────────────────────── */
#sc_dialog QFrame#sc_ch_grid {{
    background: transparent;
    border: none;
}}

/* ── Channel dots (colored unicode) ─────────────────────────── */
{dot_css}

/* ── Channel name labels ─────────────────────────────────────── */
#sc_dialog QLabel#sc_ch_name {{
    font-size: 11px;
    font-weight: 600;
    color: {"#C8D0DC" if dark else TEXT2};
    background: transparent;
}}


/* ── Dividers ────────────────────────────────────────────────── */
#sc_dialog QFrame#sc_hdiv {{
    background: {BORDER};
    max-height: 1px;
    border: none;
}}

/* ── Graph frame ─────────────────────────────────────────────── */
#sc_dialog QFrame#sc_graph_frame {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: 8px;
}}

/* ── Status pill labels ──────────────────────────────────────── */
#sc_dialog QLabel[objectName^="sc_pill_"] {{
    font-size: 10px;
    font-weight: 700;
    padding: 2px 8px;
    border-radius: 10px;
    letter-spacing: 0.4px;
    background: transparent;
}}
#sc_dialog QLabel#sc_pill_idle     {{ background: {BORDER};            color: {MUTED}; }}
#sc_dialog QLabel#sc_pill_config   {{ background: {RED_BG};            color: {RED}; }}
#sc_dialog QLabel#sc_pill_recording{{ background: {p['orange_bg']};    color: {p['orange']}; }}
#sc_dialog QLabel#sc_pill_running  {{ background: {p['green_bg']};     color: {p['green']}; }}
#sc_dialog QLabel#sc_pill_done     {{ background: {p['green_bg']};     color: {p['green_dark']}; }}
#sc_dialog QLabel#sc_pill_error    {{ background: {RED_BG};            color: {RED}; }}

/* ── Status + telemetry labels ───────────────────────────────── */
#sc_dialog QLabel#sc_status_label {{
    font-size: 11px;
    color: {TEXT2};
}}
#sc_dialog QLabel#sc_telemetry {{
    font-size: 12px;
    font-family: "Consolas", "Cascadia Code", monospace;
    color: {TEXT2};
    padding: 0px 4px;
}}
#sc_dialog QLabel#sc_bytes_ok {{
    font-size: 12px;
    font-family: "Consolas", monospace;
    color: {p['green']};
    padding: 0px 4px;
}}
#sc_dialog QLabel#sc_bytes_err {{
    font-size: 12px;
    font-family: "Consolas", monospace;
    color: {RED};
    padding: 0px 4px;
}}

/* ── Samples counter ─────────────────────────────────────────── */
#sc_dialog QLabel#sc_samples_ok   {{
    font-size: 10px; color: {MUTED}; background: transparent;
    border-radius: 3px; padding: 1px 4px;
}}
#sc_dialog QLabel#sc_samples_warn {{
    font-size: 10px; font-weight: 700; color: {p['orange']};
    background: {p['orange_bg']}; border: 1px solid {p['orange_border']};
    border-radius: 3px; padding: 1px 4px;
}}
#sc_dialog QLabel#sc_samples_err  {{
    font-size: 10px; font-weight: 700; color: white;
    background: {RED}; border: 1px solid {RED_DARK};
    border-radius: 3px; padding: 1px 4px;
}}

/* ── Coordinates overlay ─────────────────────────────────────── */
#sc_dialog QLabel#sc_coords_label {{
    font-size: 11px;
    font-family: "Consolas", "Cascadia Code", monospace;
    font-weight: 600;
    color: {TEXT2};
    background: {INPUT_BG};
    border-top: 1px solid {BORDER};
    padding: 3px 8px;
}}

/* ── No-port warning ─────────────────────────────────────────── */
#sc_dialog QFrame#sc_no_port_frame {{
    background: {p['orange_bg']};
    border: 1px solid {p['orange_border']};
    border-left: 3px solid {p['orange']};
    border-radius: 5px;
}}
#sc_dialog QLabel#sc_no_port_icon {{
    font-size: 9px;
    color: {p['orange']};
    background: transparent;
}}
#sc_dialog QLabel#sc_no_port_text {{
    font-size: 11px;
    font-weight: 600;
    color: {p['orange']};
    background: transparent;
}}

/* ── Combo boxes ─────────────────────────────────────────────── */
#sc_dialog QComboBox#sc_combo {{
    background: {INPUT_BG};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 2px 26px 2px 6px;
    font-size: 11px;
    min-height: 26px;
    selection-background-color: {RED_BG};
}}
#sc_dialog QComboBox#sc_combo:focus {{ border-color: {RED}; }}
#sc_dialog QComboBox#sc_combo::drop-down {{
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: 22px;
    border-left: 1px solid {BORDER};
    border-top-right-radius: 4px;
    border-bottom-right-radius: 4px;
    background: {BG};
}}
#sc_dialog QComboBox#sc_combo::drop-down:hover {{
    background: {RED_BG};
    border-color: {RED};
}}
#sc_dialog QComboBox#sc_combo::down-arrow {{
    image: url({_arrow_path});
    width: 10px;
    height: 10px;
}}
#sc_dialog QComboBox#sc_combo QAbstractItemView {{
    background: {CARD};
    color: {TEXT};
    border: 1px solid {BORDER};
    selection-background-color: {RED_BG};
    selection-color: {RED};
    outline: none;
}}

/* ── Custom channel combo widget ────────────────────────────── */
/* outer frame — mimics QComboBox border/background */
#sc_dialog QFrame#sc_combo_display_frame {{
    background: {INPUT_BG};
    border: 1px solid {BORDER};
    border-radius: 4px;
    min-height: 26px;
}}
#sc_dialog QFrame#sc_combo_display_frame:hover {{ border-color: {RED}; }}

/* selected-value label */
#sc_dialog QLabel#sc_combo_display_lbl {{
    background: transparent;
    color: {TEXT};
    font-size: 11px;
    padding: 0px;
}}

/* arrow area — mimics ::drop-down */
#sc_dialog QFrame#sc_combo_display_arrow {{
    background: {BG};
    border-left: 1px solid {BORDER};
    border-top-right-radius: 3px;
    border-bottom-right-radius: 3px;
}}
#sc_dialog QLabel#sc_combo_display_arrowlbl {{
    background: transparent;
    color: {MUTED};
    font-size: 11px;
}}

/* popup frame */
QFrame#sc_combo_popup {{
    background: {POPUP_CARD};
    border: 1px solid {POPUP_BORDER};
    border-radius: 4px;
}}

/* each row in the popup */
QWidget#sc_combo_row {{
    background: transparent;
}}
QWidget#sc_combo_row:hover {{
    background: {POPUP_HOVER};
    border-radius: 3px;
}}

/* row label (name) button */
QPushButton#sc_combo_row_lbl {{
    background: transparent;
    color: {POPUP_TEXT};
    border: none;
    font-size: 11px;
    padding: 3px 4px;
    text-align: left;
}}
QPushButton#sc_combo_row_lbl:hover {{
    color: {RED};
}}

/* row [−] remove button — borderless, red text only */
QPushButton#sc_combo_row_rem {{
    background: transparent;
    color: {RED};
    border: none;
    font-size: 16px;
    font-weight: 700;
    padding: 0px;
}}
QPushButton#sc_combo_row_rem:hover {{
    color: {RED_DARK};
    background: {RED_BG};
    border-radius: 3px;
}}

/* ── Spin boxes ──────────────────────────────────────────────── */
#sc_dialog QDoubleSpinBox#sc_spinbox {{
    background: {INPUT_BG};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 2px 22px 2px 6px;
    font-size: 11px;
    min-height: 26px;
}}
#sc_dialog QDoubleSpinBox#sc_spinbox:focus  {{ border-color: {RED}; }}
#sc_dialog QDoubleSpinBox#sc_spinbox:disabled {{ background: {BG}; color: {FAINT}; }}
#sc_dialog QDoubleSpinBox#sc_spinbox::up-button {{
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: 20px;
    border-left: 1px solid {BORDER};
    border-bottom: 1px solid {BORDER};
    border-top-right-radius: 4px;
    background: {BG};
}}
#sc_dialog QDoubleSpinBox#sc_spinbox::up-button:hover   {{ background: {RED_BG}; border-color: {RED}; }}
#sc_dialog QDoubleSpinBox#sc_spinbox::down-button {{
    subcontrol-origin: border;
    subcontrol-position: bottom right;
    width: 20px;
    border-left: 1px solid {BORDER};
    border-top: 1px solid {BORDER};
    border-bottom-right-radius: 4px;
    background: {BG};
}}
#sc_dialog QDoubleSpinBox#sc_spinbox::down-button:hover {{ background: {RED_BG}; border-color: {RED}; }}
#sc_dialog QDoubleSpinBox#sc_spinbox::up-arrow   {{ width: 8px; height: 8px; }}
#sc_dialog QDoubleSpinBox#sc_spinbox::down-arrow {{ width: 8px; height: 8px; }}

/* ── Checkboxes ──────────────────────────────────────────────── */
#sc_dialog QCheckBox#sc_checkbox {{
    color: {TEXT2};
    font-size: 10px;
    spacing: 3px;
}}
#sc_dialog QCheckBox#sc_checkbox::indicator {{
    width: 13px; height: 13px;
    border-radius: 3px;
    border: 1.5px solid {BORDER};
    background: {INPUT_BG};
}}
#sc_dialog QCheckBox#sc_checkbox::indicator:checked {{
    background: {RED};
    border-color: {RED};
}}

/* ── Buttons ─────────────────────────────────────────────────── */
#sc_dialog QPushButton#sc_btn_primary {{
    background: {RED};
    color: #FFFFFF;
    border: none;
    border-bottom: 2px solid {RED_DARK};
    border-radius: 5px;
    padding: 5px 14px;
    font-size: 12px;
    font-weight: 700;
    min-width: 82px;
}}
#sc_dialog QPushButton#sc_btn_primary:hover   {{ background: {RED_DARK}; }}
#sc_dialog QPushButton#sc_btn_primary:pressed {{ border-bottom: 1px solid {RED_DARK}; padding-top: 6px; }}
#sc_dialog QPushButton#sc_btn_primary:disabled {{ background: {FAINT}; color: #E8E8E8; border-bottom: none; }}

#sc_dialog QPushButton#sc_btn_outline {{
    background: {WHITE};
    color: {TEXT2};
    border: 1px solid {BORDER};
    border-radius: 5px;
    padding: 5px 10px;
    font-size: 11px;
    font-weight: 500;
}}
#sc_dialog QPushButton#sc_btn_outline:hover {{
    border-color: {RED};
    color: {RED};
    background: {RED_BG};
}}
#sc_dialog QPushButton#sc_btn_outline:pressed {{
    background: {RED_BG};
    color: {RED_DARK};
}}
#sc_dialog QPushButton#sc_btn_outline:disabled {{
    background: {INPUT_BG};
    color: {FAINT};
    border-color: {BORDER};
}}

#sc_dialog QPushButton#sc_btn_stop {{
    background: {RED};
    color: #FFFFFF;
    border: none;
    border-bottom: 2px solid {RED_DARK};
    border-radius: 5px;
    padding: 5px 10px;
    font-size: 11px;
    font-weight: 700;
}}
#sc_dialog QPushButton#sc_btn_stop:hover    {{ background: {RED_DARK}; }}
#sc_dialog QPushButton#sc_btn_stop:disabled {{ background: {FAINT}; color: #E8E8E8; border-bottom: none; }}

/* ── Tool toggle buttons (A/B, Zoom, Ghost) ──────────────────── */
#sc_dialog QPushButton#sc_btn_tool {{
    background: {INPUT_BG};
    color: {TEXT2};
    border: 1px solid {BORDER};
    border-radius: 5px;
    padding: 5px 8px;
    font-size: 11px;
    font-weight: 600;
    min-width: 42px;
}}
#sc_dialog QPushButton#sc_btn_tool:hover {{
    border-color: {RED};
    color: {RED};
    background: {RED_BG};
}}
#sc_dialog QPushButton#sc_btn_tool:checked {{
    background: {RED};
    color: white;
    border-color: {RED_DARK};
}}
#sc_dialog QPushButton#sc_btn_tool:disabled {{
    background: {INPUT_BG};
    color: {FAINT};
    border-color: {BORDER};
}}

/* ── Live values strip ───────────────────────────────────────── */
#sc_dialog QFrame#sc_live_strip {{
    background: {INPUT_BG};
    border-top: 1px solid {BORDER};
}}
#sc_dialog QLabel#sc_live_val {{
    font-size: 11px;
    font-family: "Consolas", monospace;
    font-weight: 600;
    background: transparent;
}}
#sc_dialog QLabel#sc_live_badge_live {{
    font-size: 10px;
    font-weight: 700;
    color: {p['green']};
    background: {p['green_bg']};
    border: 1px solid {p['green_border']};
    border-radius: 8px;
    padding: 1px 7px;
}}
#sc_dialog QLabel#sc_live_badge_hist {{
    font-size: 10px;
    font-weight: 700;
    color: {MUTED};
    background: {BORDER};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 1px 7px;
}}

/* ── ELF status banner ───────────────────────────────────────── */
QFrame#sc_elf_banner_scanning {{
    background: {p['orange_bg']};
    border: 1px solid {p['orange_border']};
    border-radius: 4px;
}}
QFrame#sc_elf_banner_ok {{
    background: {p['green_bg']};
    border: 1px solid {p['green_border']};
    border-radius: 4px;
}}
QFrame#sc_elf_banner_error {{
    background: {RED_BG};
    border: 1px solid {RED_BDR};
    border-radius: 4px;
}}
QFrame#sc_elf_banner_scanning QLabel,
QFrame#sc_elf_banner_ok QLabel,
QFrame#sc_elf_banner_error QLabel {{
    background: transparent;
    font-size: 11px;
}}
QFrame#sc_elf_banner_scanning QLabel#sc_elf_banner_icon {{ color: {p['orange']}; font-size: 13px; }}
QFrame#sc_elf_banner_scanning QLabel#sc_elf_banner_lbl  {{ color: {p['orange']}; }}
QFrame#sc_elf_banner_ok QLabel#sc_elf_banner_icon {{ color: {p['green']}; font-weight: 700; }}
QFrame#sc_elf_banner_ok QLabel#sc_elf_banner_lbl  {{ color: {p['green_dark']}; font-weight: 500; }}
QFrame#sc_elf_banner_error QLabel#sc_elf_banner_icon {{ color: {RED}; font-weight: 700; }}
QFrame#sc_elf_banner_error QLabel#sc_elf_banner_lbl  {{ color: {RED}; }}

/* ── Tooltip ─────────────────────────────────────────────────── */
QToolTip {{
    background: #1A1A2E;
    color: #E8EAF0;
    border: 1px solid #3D3D5C;
    border-radius: 5px;
    padding: 4px 8px;
    font-size: 11px;
}}

/* ── ELF load button (icon-only) ─────────────────────────────── */
#sc_dialog QPushButton#sc_btn_elf {{
    background: {INPUT_BG};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 0px;
}}
#sc_dialog QPushButton#sc_btn_elf:hover {{
    background: {RED_BG};
    border-color: {RED};
}}
#sc_dialog QPushButton#sc_btn_elf[loaded="true"] {{
    background: {p['green_bg']};
    border-color: {p['green_border']};
}}


/* ── Per-channel [+] button (in channel grid) ───────────────── */
#sc_dialog QPushButton#sc_btn_elf_plus {{
    background: {INPUT_BG};
    color: {TEXT2};
    border: 1px solid {BORDER};
    border-radius: 4px;
    font-size: 13px;
    font-weight: 700;
    padding: 0px;
}}
#sc_dialog QPushButton#sc_btn_elf_plus:hover {{
    background: {RED_BG};
    border-color: {RED};
    color: {RED};
}}
#sc_dialog QPushButton#sc_btn_elf_plus:disabled {{
    color: {FAINT};
    border-color: {BORDER};
    background: {BG};
}}



/* ── Picker dialog: [+] add button (always green) ───────────── */
QPushButton#sc_btn_elf_plus {{
    background: {p['green_bg']};
    color: {p['green']};
    border: 1px solid {p['green_border']};
    border-radius: 4px;
    font-size: 13px;
    font-weight: 700;
    padding: 0px;
}}
QPushButton#sc_btn_elf_plus:hover {{
    background: {p['green']};
    border-color: {p['green_dark']};
    color: white;
}}

/* ── Picker dialog: [−] remove button (always red) ──────────── */
QPushButton#sc_btn_elf_minus {{
    background: {RED_BG};
    color: {RED};
    border: 1px solid {RED_BDR};
    border-radius: 4px;
    font-size: 13px;
    font-weight: 700;
    padding: 0px;
}}
QPushButton#sc_btn_elf_minus:hover {{
    background: {RED};
    border-color: {RED_DARK};
    color: white;
}}

/* ── Picker scroll area ──────────────────────────────────────── */
QScrollArea#sc_tag_scroll {{
    background: transparent;
    border: none;
}}
QWidget#sc_tag_area {{
    background: transparent;
}}

/* ── Picker dialog (child QDialog) ──────────────────────────── */
QDialog {{
    background: {CARD};
}}
QDialog QLabel#sc_ch_name {{
    font-size: 11px;
    font-weight: 600;
    color: {"#C8D0DC" if dark else TEXT2};
    background: transparent;
}}
QDialog QLineEdit#sc_combo {{
    background: {INPUT_BG};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 2px 6px;
    font-size: 11px;
    min-height: 26px;
}}
"""
        self.setStyleSheet(qss)

        # dark mode button icon reflects current theme
        if dark:
            self._btn_dark.setText("Light")
            self._btn_dark.setToolTip("Switch to light mode  [Ctrl+Shift+D]")
        else:
            self._btn_dark.setText("Dark")
            self._btn_dark.setToolTip("Switch to dark mode  [Ctrl+Shift+D]")

        # refresh ELF button icon for current theme
        from PySide6.QtCore import QSize as _QS2
        self._btn_elf_load.setIcon(_make_elf_icon(30, dark))
        self._btn_elf_load.setIconSize(_QS2(28, 28))

        # matplotlib colors
        self.fig.patch.set_facecolor(p['card'])
        self.ax.set_facecolor(p['input_bg'])
        if not self.ax.xaxis.label.get_text():
            self.ax.set_xlabel("Time (s)", color=p['muted'], fontsize=9, fontweight='semibold', labelpad=2)
        self.ax.xaxis.label.set_color(p['muted'])
        self.ax.yaxis.label.set_color(p['muted'])
        self.ax.xaxis.label.set_fontsize(9)
        self.ax.yaxis.label.set_fontsize(9)
        self.ax.tick_params(colors=p['muted'], labelsize=8)
        self.ax.title.set_color(p['muted'])
        for spine in self.ax.spines.values():
            spine.set_color(p['border'])
            spine.set_linewidth(0.8)
        self.ax.grid(True, color=grid_color, linewidth=0.8, alpha=0.9, linestyle='--')
        leg = self.ax.get_legend()
        if leg is not None:
            leg.get_frame().set_facecolor('none')
            leg.get_frame().set_edgecolor('none')
            leg.get_frame().set_alpha(0)
            for txt in leg.get_texts():
                txt.set_color(p['text'])
        if not self.ax.lines and not self._realtime_running and not self._scroll_running:
            self._init_empty_plot()
        else:
            self.canvas.draw_idle()
        self._update_dot_colors()

    # ══════════════════════════════════════════════════════════════════════════
    #  BUTTON STATE MANAGEMENT
    # ══════════════════════════════════════════════════════════════════════════

    def _update_button_states(self):
        configured = self.is_configured
        rt_running = self._realtime_running
        sc_running = self._scroll_running
        any_running = rt_running or sc_running

        # Configure: always enabled when nothing is running
        self._btn_configure.setEnabled(not any_running)

        # Single Shot: only when configured, nothing running
        self._btn_single.setEnabled(configured and not any_running)

        # Real Time: enabled when configured and scroll not running (can stop itself)
        self._btn_realtime.setEnabled(configured and not sc_running)

        # Scroll: enabled when configured and RT not running (can stop itself)
        self._btn_scroll.setEnabled(configured and not rt_running)

        if rt_running:
            self._btn_realtime.setText("Stop ■")
            self._btn_realtime.setObjectName("sc_btn_stop")
        else:
            self._btn_realtime.setText("Real Time ▸")
            self._btn_realtime.setObjectName("sc_btn_outline")
        self._btn_realtime.style().unpolish(self._btn_realtime)
        self._btn_realtime.style().polish(self._btn_realtime)

        if sc_running:
            self._btn_scroll.setText("Stop Scroll ■")
            self._btn_scroll.setObjectName("sc_btn_stop")
        else:
            self._btn_scroll.setText("Scroll ▸")
            self._btn_scroll.setObjectName("sc_btn_outline")
        self._btn_scroll.style().unpolish(self._btn_scroll)
        self._btn_scroll.style().polish(self._btn_scroll)

    # ══════════════════════════════════════════════════════════════════════════
    # ══════════════════════════════════════════════════════════════════════════
    #  ELF VARIABLE INTEGRATION
    # ══════════════════════════════════════════════════════════════════════════

    def _on_elf_load(self):
        """Called by the ⬡ ELF button. Asks for file or folder, loads once."""
        from PySide6.QtWidgets import QDialog as _QD, QVBoxLayout as _QVL
        # small choice popup
        dlg = QDialog(self)
        dlg.setWindowTitle("Load ELF")
        dlg.setFixedSize(300, 100)
        lay = QVBoxLayout(dlg)
        lay.setSpacing(8)
        row = QHBoxLayout()
        btn_file   = QPushButton("ELF / AXF file")
        btn_folder = QPushButton("Project folder")
        btn_file.setObjectName("sc_btn_outline")
        btn_folder.setObjectName("sc_btn_primary")
        btn_file.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_folder.setCursor(Qt.CursorShape.PointingHandCursor)
        row.addWidget(btn_file); row.addWidget(btn_folder)
        lay.addLayout(row)

        chosen_path = [None]

        def pick_file():
            path, _ = QFileDialog.getOpenFileName(
                self, "Select ELF / AXF",
                filter="ELF/AXF (*.elf *.axf);;All files (*)")
            if path:
                chosen_path[0] = path
            dlg.accept()

        def pick_folder():
            folder = QFileDialog.getExistingDirectory(self, "Select STM32 project folder")
            if not folder:
                dlg.reject(); return
            chosen_path[0] = ("folder", folder)
            dlg.accept()

        btn_file.clicked.connect(pick_file)
        btn_folder.clicked.connect(pick_folder)
        dlg.exec()

        if not chosen_path[0]:
            return

        self._btn_elf_load.setEnabled(False)
        self._sig_elf_scanning.emit()   # show spinner immediately

        def _worker():
            val = chosen_path[0]
            if isinstance(val, tuple) and val[0] == "folder":
                folder = val[1]
                try:
                    elfs = _elf_find_in_folder(folder)
                except TimeoutError:
                    self._sig_elf_loaded.emit(-2)   # -2 = timeout
                    return
                if not elfs:
                    self._sig_elf_loaded.emit(-1)   # -1 = not found
                    return
                elf_path = elfs[0]   # take newest (Debug/Release preferred)
            else:
                elf_path = val
            names = _elf_load(elf_path)
            self._sig_elf_loaded.emit(len(names))

        threading.Thread(target=_worker, daemon=True).start()

    @staticmethod
    def _pick_elf_dialog(folder: str, elfs: list) -> str | None:
        """Show a list dialog when multiple ELFs are found, return chosen path."""
        dlg = QDialog()
        dlg.setWindowTitle("Multiple ELF files found")
        dlg.setMinimumSize(480, 200)
        lay = QVBoxLayout(dlg)
        lbl = QLabel("More than one ELF/AXF found — select one:")
        lbl.setObjectName("sc_input_label")
        lay.addWidget(lbl)
        lst = QListWidget()
        for p in elfs:
            lst.addItem(os.path.relpath(p, folder))
        lst.setCurrentRow(0)
        lay.addWidget(lst)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        lay.addWidget(btns)
        if dlg.exec() == QDialog.DialogCode.Accepted and lst.currentRow() >= 0:
            return elfs[lst.currentRow()]
        return None

    # ── ELF spinner helpers ───────────────────────────────────────

    _SPINNER_FRAMES = ["◐", "◓", "◑", "◒"]

    def _on_elf_scanning_slot(self):
        """Start spinner animation on the ELF button."""
        self._elf_spinner_step = 0
        self._show_elf_banner("scanning",
            "Scanning project folder for ELF files…")
        if self._elf_spinner_timer is None:
            self._elf_spinner_timer = QTimer(self)
            self._elf_spinner_timer.timeout.connect(self._spin_elf_btn)
        self._elf_spinner_timer.start(120)

    def _spin_elf_btn(self):
        frame = self._SPINNER_FRAMES[self._elf_spinner_step % 4]
        self._elf_spinner_step += 1
        # update banner icon
        if self._elf_banner_icon:
            self._elf_banner_icon.setText(frame)

    def _stop_elf_spinner(self):
        if self._elf_spinner_timer:
            self._elf_spinner_timer.stop()
        from PySide6.QtCore import QSize as _QS
        self._btn_elf_load.setIcon(_make_elf_icon(30, self._is_dark(_get_palette())))
        self._btn_elf_load.setIconSize(_QS(28, 28))

    def _show_elf_banner(self, state: str, msg: str):
        """state: 'scanning' | 'ok' | 'error'"""
        if (not hasattr(self, '_elf_banner') or self._elf_banner is None
                or not hasattr(self, '_elf_banner_lbl')):
            return
        self._elf_banner_lbl.setText(msg)
        obj = {"scanning": "sc_elf_banner_scanning",
               "ok":       "sc_elf_banner_ok",
               "error":    "sc_elf_banner_error"}.get(state, "sc_elf_banner_ok")
        self._elf_banner.setObjectName(obj)
        self._elf_banner_icon.setText(
            {"scanning": "◐", "ok": "✓", "error": "✕"}.get(state, "✓"))
        self._elf_banner.setVisible(True)
        self._elf_banner.style().unpolish(self._elf_banner)
        self._elf_banner.style().polish(self._elf_banner)

    def _on_elf_loaded_slot(self, count: int):
        """Called on the main thread after ELF load completes."""
        self._stop_elf_spinner()
        self._btn_elf_load.setEnabled(True)

        if count == -2:   # folder scan timed out
            self._show_elf_banner("error",
                "Folder too large — please select a more specific subfolder "
                "(e.g. Debug or Release).")
            return
        if count == -1:   # folder scan found nothing
            self._show_elf_banner("error", "No .elf / .axf file found in that folder.")
            return

        if count == 0:
            self._btn_elf_load.setToolTip("No symbols — rebuild with -g debug info")
            self._show_elf_banner("error",
                "No usable symbols found. Rebuild firmware with debug info (-g).")
            return

        self._btn_elf_load.setProperty("loaded", "true")
        self._btn_elf_load.setToolTip(f"ELF loaded — {count} variables available")
        self._btn_elf_load.style().unpolish(self._btn_elf_load)
        self._btn_elf_load.style().polish(self._btn_elf_load)
        for btn in self._ch_plus_btns:
            btn.setEnabled(True)
        self._show_elf_banner("ok",
            f"{count} variables loaded — click  +  on any channel to add them")
        self._apply_style()

    def _on_ch_plus(self, ch_idx: int):
        """Open the ELF variable picker for channel ch_idx."""
        if not _ELF_LOADED or not _ELF_VARS:
            QMessageBox.information(self, "No ELF loaded",
                                    "Click the chip icon first to load your firmware ELF file.")
            return
        # Wire toast to AMCMainWindow if available
        toast_cb = None
        main_win = self.parent()
        if main_win is not None and hasattr(main_win, '_show_toast'):
            toast_cb = main_win._show_toast
        dlg = _ElfVarPickerDialog(self, ch_idx, _ELF_VARS, self._ch_combos[ch_idx],
                                  toast_cb=toast_cb)
        dlg.exec()


    # ══════════════════════════════════════════════════════════════════════════
    #  CONFIG CHANGE TRACKING
    # ══════════════════════════════════════════════════════════════════════════

    def _on_config_changed(self):
        if self._updating_auto or self._configuring:
            return
        self._update_dot_colors()
        if self.is_configured:
            self.is_configured = False
            self._realtime_running = False
            self._scroll_running   = False
            if self._scroll_poll_timer is not None:
                self._scroll_poll_timer.stop()
            if self._scroll_display_timer is not None:
                self._scroll_display_timer.stop()
            self._set_status("Config changed — re-configure scope")
            self._update_button_states()

    def _update_dot_colors(self):
        if not hasattr(self, '_ch_dots'):
            return
        p = _get_palette()
        faint = p.get('faint', '#555555')
        for i, (dot, combo) in enumerate(zip(self._ch_dots, self._ch_combos)):
            code = VARIABLE_CODES.get(combo.currentText(), 0)
            color = CHANNEL_COLORS[i] if code != 0 else faint
            dot.setStyleSheet(f"color: {color}; font-size: 14px; background: transparent;")

    def _update_sample_counter(self):
        try:
            rt_ms    = self._spin_rectime.value()
            fs       = self._spin_samplefreq.value()
            n_active = sum(1 for cb in self._ch_combos if VARIABLE_CODES.get(cb.currentText(), 0) != 0)
            n_active = max(n_active, 1)
            max_buf  = 8000 // (n_active * 4)
            period_div = max(1, round(fs and self.fpwm / fs or 1))
            actual_fs  = self.fpwm / period_div
            n_samples  = max(1, round((rt_ms / 1000.0) * actual_fs))
            pct = n_samples / max_buf * 100.0

            if n_samples > max_buf:
                obj  = "sc_samples_err"
                text = f"⚠ OVERFLOW  {n_samples} / {max_buf}"
            elif pct >= 80:
                obj  = "sc_samples_warn"
                text = f"⚠ {n_samples} / {max_buf}  ({pct:.0f}%)"
            else:
                obj  = "sc_samples_ok"
                text = f"{n_samples} / {max_buf}  ({pct:.0f}%)"

            self._lbl_samples.setText(text)
            if self._lbl_samples.objectName() != obj:
                self._lbl_samples.setObjectName(obj)
                self._lbl_samples.style().unpolish(self._lbl_samples)
                self._lbl_samples.style().polish(self._lbl_samples)
        except Exception:
            pass

    def _on_ylock_toggled(self, checked):
        if checked:
            self._ylim_locked = self.ax.get_ylim()
        else:
            self._ylim_locked = None
            self.ax.autoscale(axis='y')
            self.canvas.draw_idle()

    def _on_rectime_changed(self, value):
        if self._updating_auto:
            return
        if self._chk_rectime_max.isChecked():
            self._updating_auto = True
            self._chk_rectime_max.setChecked(False)
            self._updating_auto = False

    def _on_samplefreq_changed(self, value):
        if self._updating_auto:
            return
        if self._chk_samplefreq_max.isChecked():
            self._updating_auto = True
            self._chk_samplefreq_max.setChecked(False)
            self._updating_auto = False

    def _on_rectime_max_toggled(self, checked):
        if self._updating_auto:
            return
        self._updating_auto = True
        if checked:
            self._chk_samplefreq_max.setChecked(False)
            self._update_auto_value(rectime_max=True)
            self._spin_rectime.setEnabled(False)
        else:
            self._spin_rectime.setEnabled(True)
        self._updating_auto = False
        self._on_config_changed()

    def _on_samplefreq_max_toggled(self, checked):
        if self._updating_auto:
            return
        self._updating_auto = True
        if checked:
            self._chk_rectime_max.setChecked(False)
            self._update_auto_value(samplefreq_max=True)
            self._spin_samplefreq.setEnabled(False)
        else:
            self._spin_samplefreq.setEnabled(True)
        self._updating_auto = False
        self._on_config_changed()

    def _update_auto_value(self, rectime_max=False, samplefreq_max=False):
        n_active = sum(1 for cb in self._ch_combos if VARIABLE_CODES.get(cb.currentText(), 0) != 0)
        n_active = max(n_active, 1)
        max_samples = 8000 // (n_active * 4)

        if rectime_max:
            fs = self._spin_samplefreq.value()
            if fs > 0:
                self._spin_rectime.setValue((max_samples / fs) * 1000.0)

        if samplefreq_max:
            rt_ms = self._spin_rectime.value()
            if rt_ms > 0:
                fs_max = min(max_samples / (rt_ms / 1000.0), self.fpwm)
                self._spin_samplefreq.setValue(fs_max)

    # ══════════════════════════════════════════════════════════════════════════
    #  NO-PORT WARNING
    # ══════════════════════════════════════════════════════════════════════════

    def _show_no_port_warning(self):
        if self._no_port_timer is not None:
            self._no_port_timer.stop()
            self._no_port_timer = None
        self._no_port_pulse = 0
        self._no_port_frame.setVisible(True)
        self._pulse_no_port_icon()
        self._no_port_timer = QTimer(self)
        self._no_port_timer.timeout.connect(self._pulse_no_port_icon)
        self._no_port_timer.start(400)
        QTimer.singleShot(2400, self._hide_no_port_warning)

    def _pulse_no_port_icon(self):
        p = _get_palette()
        if self._no_port_pulse % 2 == 0:
            self._no_port_icon.setStyleSheet(f"font-size:13px; color:{p['red']}; background:transparent;")
        else:
            self._no_port_icon.setStyleSheet("font-size:13px; color:rgba(183,28,28,80); background:transparent;")
        self._no_port_pulse += 1

    def _hide_no_port_warning(self):
        if self._no_port_timer is not None:
            self._no_port_timer.stop()
            self._no_port_timer = None
        self._no_port_frame.setVisible(False)

    # ══════════════════════════════════════════════════════════════════════════
    #  BUTTON HANDLERS
    # ══════════════════════════════════════════════════════════════════════════

    def _on_configure_clicked(self):
        if not self.serial_manager.is_open:
            self._show_no_port_warning()
            return
        if self._realtime_running or self._scroll_running:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "Operation in Progress",
                "Stop the current Real Time or Scroll session before reconfiguring.")
            return
        self._btn_configure.setEnabled(False)
        self._btn_configure.setText("Configuring…")
        self._start_configure_spinner()
        # Read all UI state on main thread before handing off to worker
        cfg_params = {
            'ch_codes':    [VARIABLE_CODES.get(cb.currentText(), 0) for cb in self._ch_combos],
            'rec_time_ms': self._spin_rectime.value(),
            'sample_freq': self._spin_samplefreq.value(),
            't_display':   self._spin_tdisplay.value(),
        }
        threading.Thread(target=lambda: self._worker_configure(cfg_params), daemon=True).start()

    def _start_configure_spinner(self):
        self._spinner_frames = ["Configuring ·", "Configuring ··", "Configuring ···", "Configuring ··"]
        self._spinner_idx = 0
        self._spinner_timer = QTimer(self)
        self._spinner_timer.timeout.connect(self._spinner_tick)
        self._spinner_timer.start(250)

    def _spinner_tick(self):
        self._btn_configure.setText(self._spinner_frames[self._spinner_idx % len(self._spinner_frames)])
        self._spinner_idx += 1

    def _stop_configure_spinner(self):
        if hasattr(self, '_spinner_timer') and self._spinner_timer is not None:
            self._spinner_timer.stop()
            self._spinner_timer = None
        self._btn_configure.setText("Configure")

    def _on_single_clicked(self):
        if not self.serial_manager.is_open:
            self._show_no_port_warning()
            return
        if self._realtime_running or self._scroll_running:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "Operation in Progress",
                "Stop the current Real Time or Scroll session before starting a Single Shot capture.")
            return
        if self._last_plot_data is not None:
            from PySide6.QtWidgets import QMessageBox
            r = QMessageBox.question(
                self,
                "Overwrite capture?",
                "You have unsaved capture data. Starting a new recording will erase it. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if r != QMessageBox.StandardButton.Yes:
                return
        self._btn_single.setEnabled(False)
        self._btn_configure.setEnabled(False)
        # Snapshot trigger UI values on main thread before entering worker thread
        self._trig_snapshot = (
            self._trigger_enabled,
            self._combo_trig_ch.currentIndex(),
            self._combo_trig_edge.currentIndex() == 0,
            self._spin_trig_level.value(),
        )
        threading.Thread(target=self._worker_record, daemon=True).start()

    def _on_realtime_clicked(self):
        if not self.serial_manager.is_open:
            self._show_no_port_warning()
            return
        if self._scroll_running:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "Operation in Progress",
                "Stop the Scroll session before switching to Real Time mode.")
            return
        if self._realtime_running:
            self._realtime_running = False
            self._set_status("Stopping real-time...")
        else:
            self._realtime_running = True
            self._update_button_states()
            threading.Thread(target=self._worker_realtime, daemon=True).start()

    def _on_scroll_clicked(self):
        if not self.serial_manager.is_open:
            self._show_no_port_warning()
            return
        if self._realtime_running:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "Operation in Progress",
                "Stop the Real Time session before switching to Scroll mode.")
            return
        if not self.is_configured or self.last_config is None:
            self._set_status("Configure scope first.")
            return
        if self._scroll_running:
            # Stop: clear flag, send recmod=0, restore buttons
            self._scroll_running = False
            try:
                with self.serial_manager._lock:
                    self.serial_manager._ser.write(
                        f"#s recmod {dec_encode(0.0)};\n".encode("ascii"))
            except Exception:
                pass
            self._update_button_states()
            self._set_status("Scroll stopped")
        else:
            # Validate display window
            t_display = self._spin_tdisplay.value()
            if t_display <= 0:
                self._set_status("Display window must be > 0 s")
                return
            cfg = self.last_config
            sample_rate = cfg['samplefreq']
            ring_size = max(2, round(t_display * sample_rate))
            # Init ring buffers (deque, matching reference)
            self._scroll_rings = [
                collections.deque([float('nan')] * ring_size, maxlen=ring_size)
                for _ in range(4)
            ]
            self._scroll_t_ring   = collections.deque([float('nan')] * ring_size, maxlen=ring_size)
            self._scroll_t0       = time.monotonic()
            self._scroll_t_display = t_display
            self._scroll_frame_count = 0
            self._scroll_running  = True
            # Build axes once
            self._scroll_setup_axes(cfg['ch_codes'], t_display, ring_size)
            self._update_button_states()
            # Set firmware to continuous mode
            try:
                with self.serial_manager._lock:
                    self.serial_manager._ser.write(
                        f"#s recmod {dec_encode(1.0)};\n".encode("ascii"))
                self._set_status("Scroll — running")
            except Exception as e:
                self._set_status(f"Scroll arm error: {e}")
                self._scroll_running = False
                self._update_button_states()
                return
            # Start QTimer-based poll (non-blocking, on main thread)
            self._scroll_poll_timer = QTimer(self)
            self._scroll_poll_timer.timeout.connect(self._scroll_poll_half)
            self._scroll_poll_timer.start(5)
            # Start display refresh timer
            self._scroll_display_timer = QTimer(self)
            self._scroll_display_timer.timeout.connect(self._scroll_display_tick)
            self._scroll_display_timer.start(20)

    def _on_compact_clicked(self):
        if not self._is_maximized:
            self._restore_geometry = self.geometry()
            self.showMaximized()
            self._is_maximized = True
            self._btn_compact.setIcon(_make_restore_icon("#555555"))
            self._btn_compact.setToolTip("Restore window  [Ctrl+M]")
        else:
            self.showNormal()
            if self._restore_geometry is not None:
                self.setGeometry(self._restore_geometry)
            self._is_maximized = False
            self._btn_compact.setIcon(_make_maximize_icon("#555555"))
            self._btn_compact.setToolTip("Maximize / Restore  [Ctrl+M]")

    def _on_dark_clicked(self):
        try:
            import amc_interface_qt as _amcqt
            if _amcqt._THEME == "light":
                _amcqt.C = dict(_amcqt.C_DARK)
                _amcqt._THEME = "dark"
                self._btn_dark.setIcon(_make_theme_icon(dark_mode=True))
                self._btn_dark.setToolTip("Switch to light mode  [Ctrl+Shift+D]")
            else:
                _amcqt.C = dict(_amcqt.C_LIGHT)
                _amcqt._THEME = "light"
                self._btn_dark.setIcon(_make_theme_icon(dark_mode=False))
                self._btn_dark.setToolTip("Switch to dark mode  [Ctrl+Shift+D]")
        except Exception:
            pass
        self._apply_style()

    # ══════════════════════════════════════════════════════════════════════════
    #  TOOL BUTTON HANDLERS
    # ══════════════════════════════════════════════════════════════════════════

    def _on_ab_toggled(self, checked):
        self._ab_mode = checked
        self._btn_ab.setText("Cursors: ON" if checked else "Cursors: OFF")
        if not checked:
            self._cursor_a = None
            self._cursor_b = None
            self._clear_ab_lines()
            self.canvas.draw_idle()
            self._lbl_coords.setText("")
            self.canvas.setCursor(Qt.CursorShape.ArrowCursor)
        else:
            self.canvas.setCursor(Qt.CursorShape.CrossCursor)

    def _clear_ab_lines(self):
        for attr in ('_cursor_a_line', '_cursor_b_line'):
            line = getattr(self, attr, None)
            if line is not None:
                try:
                    line.remove()
                except Exception:
                    pass
                setattr(self, attr, None)

    def _autoscale_y_to_view(self):
        """Rescale Y axis to the min/max of data visible in the current X window."""
        if not self._has_plot_data or not self._plotted_lines:
            return
        x0, x1 = self.ax.get_xlim()
        y_min, y_max = float('inf'), float('-inf')
        for line in self._plotted_lines.values():
            if not line.get_visible():
                continue
            xd = line.get_xdata()
            yd = line.get_ydata()
            if xd is None or yd is None or len(xd) == 0:
                continue
            mask = (xd >= x0) & (xd <= x1)
            if not np.any(mask):
                continue
            visible_y = np.asarray(yd)[mask]
            visible_y = visible_y[np.isfinite(visible_y)]
            if len(visible_y) == 0:
                continue
            y_min = min(y_min, float(visible_y.min()))
            y_max = max(y_max, float(visible_y.max()))
        if y_min < y_max:
            margin = (y_max - y_min) * 0.08
            self.ax.set_ylim(y_min - margin, y_max + margin)
        elif y_min == y_max:
            self.ax.set_ylim(y_min - 1.0, y_max + 1.0)

    def _on_scroll_zoom(self, event):
        """Scroll wheel zooms X axis anchored to cursor — no translation."""
        if event.inaxes is not self.ax or event.xdata is None:
            return
        factor = 0.75 if event.button == 'up' else 1.0 / 0.75
        x0, x1 = self.ax.get_xlim()
        xc = event.xdata
        # Scale span around cursor: left and right distances scale equally
        new_xlim = (xc - (xc - x0) * factor, xc + (x1 - xc) * factor)
        # Clamp zoom-out to full data range
        data_xlim = self._data_xlim
        if data_xlim is not None and event.button != 'up':
            if (new_xlim[1] - new_xlim[0]) > (data_xlim[1] - data_xlim[0]):
                new_xlim = data_xlim
        self.ax.set_xlim(new_xlim)
        self._autoscale_y_to_view()
        self._blit_bg = None
        self.canvas.draw_idle()
        # RT: if user scrolled forward to live edge, return to live view
        if self._realtime_running and self._rt_panned and self._rt_t0 is not None:
            buf_t_max_s = time.time() - self._rt_t0
            if buf_t_max_s > 0 and new_xlim[1] >= buf_t_max_s * 0.97:
                self._rt_panned = False
                QTimer.singleShot(0, self._rt_update_live_badge)

    def _on_pan_motion(self, event):
        """Right-click drag pan using pixel coordinates — no drift."""
        if not self._pan_active:
            return
        if event.x is None or event.y is None:
            return
        # Convert pixel delta to data units using the transform
        ax = self.ax
        px0, py0 = self._pan_start_px
        dx_px = event.x - px0
        dy_px = event.y - py0
        # Transform one pixel to data coords
        inv = ax.transData.inverted()
        pt0 = inv.transform((0, 0))
        pt1 = inv.transform((dx_px, dy_px))
        dx = pt1[0] - pt0[0]
        dy = pt1[1] - pt0[1]
        x0, x1 = self._pan_start_xlim
        y0, y1 = self._pan_start_ylim
        self.ax.set_xlim(x0 - dx, x1 - dx)
        self._autoscale_y_to_view()
        self._blit_bg = None
        self.canvas.draw_idle()

    def _on_pan_release(self, event):
        if event.button == 3:
            self._pan_active = False
            self._pan_start_px   = None
            self._pan_start_xlim = None
            self._pan_start_ylim = None
        if self._ab_mode:
            self.canvas.setCursor(Qt.CursorShape.CrossCursor)
        else:
            self.canvas.setCursor(Qt.CursorShape.ArrowCursor)

    def _on_legend_pick(self, event):
        artist = event.artist
        leg_map = getattr(self, '_leg_line_map', {})
        if artist not in leg_map:
            return
        orig_line = leg_map[artist]
        visible = not orig_line.get_visible()
        orig_line.set_visible(visible)
        # Refresh all handle + text alphas for this line
        if self._legend_obj is not None:
            p = _get_palette()
            for a, ol in leg_map.items():
                if ol is not orig_line:
                    continue
                a.set_alpha(1.0 if visible else (0.35 if hasattr(a, 'get_text') else 0.2))
            for txt in self._legend_obj.get_texts():
                ol = leg_map.get(txt)
                if ol is not None:
                    ov = ol.get_visible()
                    txt.set_alpha(1.0 if ov else 0.35)
                    txt.set_color(p['text'] if ov else p['muted'])
        self._blit_bg = None
        self.canvas.draw_idle()

    def _on_trigger_toggled(self, checked):
        self._trigger_enabled = checked
        self._combo_trig_ch.setEnabled(checked)
        self._combo_trig_edge.setEnabled(checked)
        self._spin_trig_level.setEnabled(checked)

    def _on_canvas_click(self, event):
        if event.inaxes is not self.ax or event.xdata is None:
            return
        # Right-click: start pan
        if event.button == 3:
            self._pan_active = True
            self._pan_start_px   = (event.x, event.y)
            self._pan_start_xlim = self.ax.get_xlim()
            self._pan_start_ylim = self.ax.get_ylim()
            self.canvas.setCursor(Qt.CursorShape.ClosedHandCursor)
            if self._realtime_running:
                self._rt_panned = True
            return
        # Left-click: A/B measurement cursors (only in cursor mode)
        if event.button != 1 or not self._ab_mode:
            return
        x_s = event.xdata
        y = event.ydata
        if self._cursor_a is None:
            self._cursor_a = (event.xdata, y)
            self._clear_ab_lines()
            self._cursor_a_line = self.ax.axvline(event.xdata, color='#F0A000',
                                                   linewidth=1.2, alpha=0.9, linestyle='-')
            self._lbl_coords.setText(f"  A: t={x_s:.4f} s  val={y:.4g}  —  left-click to set B")
            self._blit_bg = None
            self.canvas.draw_idle()
        else:
            self._cursor_b = (event.xdata, y)
            if self._cursor_b_line is not None:
                try:
                    self._cursor_b_line.remove()
                except Exception:
                    pass
            self._cursor_b_line = self.ax.axvline(event.xdata, color='#40C0A0',
                                                   linewidth=1.2, alpha=0.9, linestyle='-')
            xa, ya = self._cursor_a
            dt = event.xdata - xa
            dy = y - ya
            self._lbl_coords.setText(
                f"  A: t={xa:.4f}s  val={ya:.4g}    "
                f"B: t={x_s:.4f}s  val={y:.4g}    "
                f"ΔT={dt:.4f}s  ΔY={dy:.4g}  —  click to move B"
            )
            self._cursor_b = None  # allow re-clicking B
            self._blit_bg = None
            self.canvas.draw_idle()

    # ══════════════════════════════════════════════════════════════════════════
    #  SESSION SAVE / LOAD
    # ══════════════════════════════════════════════════════════════════════════

    def _save_session_config(self):
        from PySide6.QtCore import QSettings
        s = QSettings("Appcon Technologies", "AMC Scope")
        s.setValue("ch0", self._ch_combos[0].currentText())
        s.setValue("ch1", self._ch_combos[1].currentText())
        s.setValue("ch2", self._ch_combos[2].currentText())
        s.setValue("ch3", self._ch_combos[3].currentText())
        s.setValue("rec_time",    self._spin_rectime.value())
        s.setValue("sample_freq", self._spin_samplefreq.value())
        s.setValue("t_display",   self._spin_tdisplay.value())

    def _load_session_config(self):
        from PySide6.QtCore import QSettings
        s = QSettings("Appcon Technologies", "AMC Scope")
        try:
            var_keys = list(VARIABLE_CODES.keys())
            for i, key in enumerate(["ch0", "ch1", "ch2", "ch3"]):
                v = s.value(key, "None")
                if v in var_keys:
                    self._ch_combos[i].setCurrentText(v)
            rt = s.value("rec_time",    20.0,        type=float)
            sf = s.value("sample_freq", self.fpwm,   type=float)
            td = s.value("t_display",   1.0,         type=float)
            self._spin_rectime.setValue(rt)
            self._spin_samplefreq.setValue(sf)
            self._spin_tdisplay.setValue(td)
        except Exception:
            pass

    # ══════════════════════════════════════════════════════════════════════════
    #  DISPLAY WINDOW CHANGE (scroll mode live resize)
    # ══════════════════════════════════════════════════════════════════════════

    def _on_tdisplay_change(self, value):
        if not self._scroll_running or self._scroll_rings is None or self.last_config is None:
            return
        try:
            t_display = float(value)
            if t_display <= 0:
                return
        except (ValueError, TypeError):
            return
        sample_rate = self.last_config['samplefreq']
        ring_size = max(2, round(t_display * sample_rate))
        new_rings = []
        for old_deque in self._scroll_rings:
            old_data = list(old_deque)
            if len(old_data) >= ring_size:
                new_data = old_data[-ring_size:]
            else:
                new_data = [float('nan')] * (ring_size - len(old_data)) + old_data
            new_rings.append(collections.deque(new_data, maxlen=ring_size))
        old_times = list(self._scroll_t_ring)
        if len(old_times) >= ring_size:
            new_times = old_times[-ring_size:]
        else:
            dt = 1.0 / sample_rate
            pad = [old_times[0] - (ring_size - len(old_times) - i) * dt
                   for i in range(ring_size - len(old_times))] if old_times else [0.0] * (ring_size - len(old_times))
            new_times = pad + old_times
        self._scroll_rings     = new_rings
        self._scroll_t_ring    = collections.deque(new_times, maxlen=ring_size)
        self._scroll_t_display = t_display
        self._scroll_setup_axes(self.last_config['ch_codes'], t_display, ring_size)
        logging.debug("SCOPE _on_tdisplay_change: resized rings to %d pts", ring_size)

    # ══════════════════════════════════════════════════════════════════════════
    #  CANVAS RESIZE
    # ══════════════════════════════════════════════════════════════════════════

    def _on_canvas_resize(self, event):
        self._blit_bg = None
        try:
            self.fig.tight_layout(pad=0.2)
        except Exception:
            pass
        self.canvas.draw_idle()
        if self._scroll_running and self._scroll_lines:
            QTimer.singleShot(50, self._recapture_blit_bg)

    def _recapture_blit_bg(self):
        if not self._scroll_running:
            return
        self.canvas.draw()
        self._scroll_bg = self.canvas.copy_from_bbox(self.ax.bbox)

    def _on_mouse_move(self, event):
        """Smooth crosshair via blitting — no per-frame annotation overhead."""
        if self._pan_active:
            return
        # Change cursor to pointer when hovering over the legend
        leg = getattr(self, '_legend_obj', None)
        if leg is not None and event.x is not None and event.y is not None:
            try:
                bb = leg.get_window_extent()
                if bb.contains(event.x, event.y):
                    self.canvas.setCursor(Qt.CursorShape.PointingHandCursor)
                    from PySide6.QtWidgets import QToolTip
                    from PySide6.QtCore import QPoint
                    gpos = self.canvas.mapToGlobal(QPoint(int(event.x), self.canvas.height() - int(event.y)))
                    QToolTip.showText(gpos, "Click a channel to show / hide it", self.canvas)
                else:
                    QToolTip.hideText()
                    if not self._ab_mode:
                        self.canvas.setCursor(Qt.CursorShape.ArrowCursor)
            except Exception:
                pass
        if event.inaxes is self.ax and event.xdata is not None and self._has_plot_data:
            self._lbl_coords.setText(f"  t = {event.xdata:.4f} s    val = {event.ydata:.4g}")
            p = _get_palette()
            cross_color = "#4A90D9" if not self._is_dark(p) else "#6BAEE8"
            canvas = self.canvas
            # Capture background once (or after any full redraw)
            if self._blit_bg is None:
                if self._crosshair_v is not None:
                    self._crosshair_v.set_visible(False)
                self._blit_bg = canvas.copy_from_bbox(self.ax.bbox)
                if self._crosshair_v is not None:
                    self._crosshair_v.set_visible(True)
            if self._crosshair_v is None:
                self._crosshair_v, = self.ax.plot(
                    [event.xdata, event.xdata],
                    self.ax.get_ylim(),
                    color=cross_color, linewidth=1.0, alpha=0.75,
                    linestyle='--', animated=True
                )
            else:
                self._crosshair_v.set_xdata([event.xdata, event.xdata])
                self._crosshair_v.set_ydata(self.ax.get_ylim())
            canvas.restore_region(self._blit_bg)
            self.ax.draw_artist(self._crosshair_v)
            canvas.blit(self.ax.bbox)
        else:
            if self._lbl_coords.text():
                self._lbl_coords.setText("")
            if self._crosshair_v is not None:
                self._blit_bg = None
                self._crosshair_v.remove()
                self._crosshair_v = None
                if not self._scroll_running:
                    self.canvas.draw_idle()

    # ══════════════════════════════════════════════════════════════════════════
    #  STATUS / BYTES SIGNALS + SLOTS
    # ══════════════════════════════════════════════════════════════════════════

    def _set_status(self, text: str):
        self._sig_status.emit(text)

    def _slot_set_status(self, text: str):
        self._lbl_status.setText(text)
        tl = text.lower()
        if any(k in tl for k in ("trigger waiting", "waiting for trigger")):
            pill_obj, pill_txt = "sc_pill_recording", "WAITING"
        elif any(k in tl for k in ("recording", "reading", "arming")):
            pill_obj, pill_txt = "sc_pill_recording", "RECORDING"
        elif any(k in tl for k in ("running", "real-time", "real time", "scroll")):
            pill_obj, pill_txt = "sc_pill_running",   "RUNNING"
        elif any(k in tl for k in ("configuring", "configure")):
            pill_obj, pill_txt = "sc_pill_config",    "CONFIGURING"
        elif any(k in tl for k in ("error", "failed", "incomplete", "not open", "not configured", "timeout")):
            pill_obj, pill_txt = "sc_pill_error",     "ERROR"
        elif tl.endswith("configured") or tl == "done" or any(k in tl for k in ("saved", "success")):
            pill_obj, pill_txt = "sc_pill_done",      "DONE"
        else:
            pill_obj, pill_txt = "sc_pill_idle",      "IDLE"

        self._lbl_status_pill.setText(pill_txt)
        if self._lbl_status_pill.objectName() != pill_obj:
            self._lbl_status_pill.setObjectName(pill_obj)
            self._lbl_status_pill.style().unpolish(self._lbl_status_pill)
            self._lbl_status_pill.style().polish(self._lbl_status_pill)

    def _set_bytes(self, received: int, expected: int):
        self._sig_bytes.emit(received, expected)

    def _slot_set_bytes(self, received: int, expected: int):
        ok  = received == expected
        self._lbl_bytes.setText(f"Bytes: {received} / {expected}")
        obj = "sc_bytes_ok" if ok else "sc_bytes_err"
        self._lbl_bytes.setObjectName(obj)
        self._lbl_bytes.style().unpolish(self._lbl_bytes)
        self._lbl_bytes.style().polish(self._lbl_bytes)

    def _slot_show_warning(self, title: str, message: str):
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.warning(self, title, message)

    # ══════════════════════════════════════════════════════════════════════════
    #  WORKER: CONFIGURE
    # ══════════════════════════════════════════════════════════════════════════

    def _worker_configure(self, cfg_params: dict):
        logging.debug("SCOPE _worker_configure: starting")
        self._configuring = True
        self._set_status("Configuring scope...")

        try:
            if not self.serial_manager.is_open:
                raise RuntimeError("Serial port not open.")

            ch_codes    = cfg_params['ch_codes']
            rec_time_ms = cfg_params['rec_time_ms']
            sample_freq = cfg_params['sample_freq']
            t_display   = cfg_params['t_display']

            n_active = sum(1 for c in ch_codes if c != 0)
            if n_active == 0:
                raise ValueError("Select at least one channel.")

            period_div   = max(1, round(self.fpwm / sample_freq))
            actual_fs    = self.fpwm / period_div
            n_samples    = max(1, round(rec_time_ms * actual_fs / 1000.0))
            rec_time_s   = n_samples / actual_fs

            ch_datatypes = [VARIABLE_DATATYPES.get(c, 1) for c in ch_codes]

            rectyp_value = 0
            for idx, dt in enumerate(ch_datatypes):
                if dt == 1:
                    rectyp_value |= (1 << idx)

            expected_bytes = sum(
                n_samples * (4 if ch_datatypes[i] == 1 else 2)
                for i, c in enumerate(ch_codes) if c != 0
            )

            if expected_bytes > 8000:
                raise ValueError(f"Buffer overflow: {expected_bytes} bytes needed, limit is 8000 bytes.")
            if n_samples > 4000:
                raise ValueError(f"Too many samples: {n_samples} > limit 4000")

            recad_value = (ch_codes[0] * 1_000_000 + ch_codes[1] * 10_000 +
                           ch_codes[2] * 100        + ch_codes[3])

            with self.serial_manager._lock:
                ser = self.serial_manager._ser

                def _send_set(name, value):
                    name_padded = name.ljust(6)
                    value_str   = dec_encode(float(value))
                    ser.write(f"#s {name_padded} {value_str};\n".encode("ascii"))
                    time.sleep(0.02)

                _send_set("recad",  recad_value)
                _send_set("recns",  n_samples)
                _send_set("recap",  period_div)
                _send_set("rectyp", rectyp_value)

                logging.info("SCOPE SET: recad=%d recns=%d recap=%d rectyp=%d",
                             recad_value, n_samples, period_div, rectyp_value)

                def _send_get(name):
                    ser.write(f"#g {name.ljust(6)};\n".encode("ascii"))
                    try:
                        ser.readline()  # discard "->" prompt
                        resp = ser.readline().decode("ascii", errors="ignore").strip("\r\n ")
                        return resp
                    except Exception as e:
                        return f"<error: {e}>"

                rb_recns  = _send_get("recns")
                rb_recap  = _send_get("recap")
                rb_rectyp = _send_get("rectyp")
                rb_recad  = _send_get("recad")
                logging.info("SCOPE VERIFY: recns=%s (sent %d)", rb_recns, n_samples)
                logging.info("SCOPE VERIFY: recap=%s (sent %d)", rb_recap, period_div)
                logging.info("SCOPE VERIFY: rectyp=%s (sent %d)", rb_rectyp, rectyp_value)
                logging.info("SCOPE VERIFY: recad=%s (sent %d)", rb_recad, recad_value)

            self.last_config = {
                'ch_codes':       ch_codes,
                'ch_datatypes':   ch_datatypes,
                'rec_time_ms':    rec_time_ms,
                'samplefreq':     actual_fs,
                'n_samples':      n_samples,
                'period_div':     period_div,
                'expected_bytes': expected_bytes,
                'rec_time_s':     rec_time_s,
                't_display':      t_display,
            }
            self.is_configured = True
            ch_names = [VARIABLE_NAMES.get(c, '?') for c in ch_codes if c != 0]
            logging.info("SCOPE CONFIGURE OK: recad=%d rectyp=%d n_samples=%d actual_fs=%.1f",
                         recad_value, rectyp_value, n_samples, actual_fs)
            self._set_status(
                f"Configured: {n_samples} smp @ {actual_fs:.0f} Hz  —  ch: {', '.join(ch_names)}"
            )

        except Exception as e:
            logging.exception("Scope configure failed")
            self.is_configured = False
            msg = str(e)
            if "not open" in msg.lower() or "port" in msg.lower():
                self._set_status("Error: No serial port connected — connect first, then configure")
            elif "channel" in msg.lower() or "select" in msg.lower():
                self._set_status("Error: Select at least one channel (set Ch1–Ch4 to a variable, not None)")
            elif "overflow" in msg.lower() or "bytes" in msg.lower():
                self._set_status("Error: Buffer overflow — reduce Rec time or sample rate, or use fewer channels")
            elif "samples" in msg.lower():
                self._set_status("Error: Too many samples — reduce Rec [ms] or increase period")
            else:
                self._set_status(f"Configure failed: {msg}")
        finally:
            self._configuring = False
            self._sig_stop_spinner.emit()
            QMetaObject.invokeMethod(
                self._btn_configure, "setEnabled",
                Qt.ConnectionType.QueuedConnection, Q_ARG(bool, True)
            )
            self._sig_update_buttons.emit()

    # ══════════════════════════════════════════════════════════════════════════
    #  WORKER: SINGLE SHOT RECORD
    # ══════════════════════════════════════════════════════════════════════════

    def _worker_record(self):
        logging.debug("SCOPE _worker_record: starting")
        if not self.last_config:
            self._set_status("Not configured")
            self._sig_update_buttons.emit()
            return

        cfg            = self.last_config
        expected_bytes = cfg['expected_bytes']
        rec_time_s     = cfg['rec_time_s']

        # Use snapshot captured on main thread before this worker was started
        trig_enabled, trig_ch_idx, trig_rising, trig_level = getattr(
            self, '_trig_snapshot', (False, 0, True, 0.0)
        )

        try:
            if not self.serial_manager.is_open:
                raise RuntimeError("Serial port not open.")

            # Trigger wait loop: poll the trigger channel until condition is met
            if trig_enabled:
                trig_ch_code = cfg['ch_codes'][trig_ch_idx]
                trig_var_name = VARIABLE_NAMES.get(trig_ch_code, "")
                if trig_var_name and trig_var_name != "None":
                    self._set_status(f"Waiting for trigger on Ch{trig_ch_idx+1} ({trig_var_name}) "
                                     f"{'rising' if trig_rising else 'falling'} @ {trig_level:.3g}…")
                    prev_val = None
                    deadline = time.monotonic() + 30.0  # 30s trigger timeout
                    while time.monotonic() < deadline:
                        try:
                            with self.serial_manager._lock:
                                ser = self.serial_manager._ser
                                ser.write(f"#g {trig_var_name.ljust(6)} ;\n".encode("ascii"))
                                ser.readline()  # prompt
                                resp = ser.readline().decode("ascii", errors="ignore").strip()
                            cur_val = float(resp.replace('+', '').replace(' ', ''))
                            if prev_val is not None:
                                crossed = (trig_rising and prev_val < trig_level <= cur_val) or \
                                          (not trig_rising and prev_val > trig_level >= cur_val)
                                if crossed:
                                    logging.debug("SCOPE trigger fired: prev=%.4g cur=%.4g", prev_val, cur_val)
                                    break
                            prev_val = cur_val
                        except Exception as _e:
                            logging.debug("SCOPE trigger poll failed: %s", _e)
                        time.sleep(0.01)
                    else:
                        self._set_status("Trigger timeout — no crossing detected within 30 s")
                        self._sig_update_buttons.emit()
                        return

            with self.serial_manager._lock:
                ser = self.serial_manager._ser

                self._set_status("Arming recording...")
                ser.write(f"#s recptr {dec_encode(0.0)};\n".encode("ascii"))
                time.sleep(0.02)

                self._set_status(f"Recording {rec_time_s*1000:.1f} ms...")
                time.sleep(rec_time_s + 0.05)

                self._set_status(f"Reading {expected_bytes} bytes...")
                ser.reset_input_buffer()
                ser.write(b"#g recbuf ;\n")
                logging.info("SCOPE: Sent #g recbuf ; (expecting %d bytes)", expected_bytes)
                # Expert-style polling loop: up to 5 s, reads in chunks as bytes arrive
                buffer_response = bytearray()
                timeout_count = 0
                timeout_max = 500  # 500 × 10 ms = 5 s
                while len(buffer_response) < expected_bytes and timeout_count < timeout_max:
                    available = ser.in_waiting
                    if available > 0:
                        to_read = min(available, expected_bytes - len(buffer_response))
                        chunk = ser.read(to_read)
                        if chunk:
                            buffer_response += chunk
                            timeout_count = 0
                    else:
                        timeout_count += 1
                        time.sleep(0.01)
                buffer_response = bytes(buffer_response)
                received = len(buffer_response)
                leftover = ser.in_waiting
                if leftover > 0:
                    logging.warning("SCOPE: %d extra bytes cleared", leftover)
                    ser.reset_input_buffer()

            self._set_bytes(received, expected_bytes)

            # Always plot whatever was received — partial data is better than nothing
            if len(buffer_response) > 0:
                self._parse_and_plot_data(bytes(buffer_response), cfg)

            if received == expected_bytes:
                self._set_status(f"Done — received {received} bytes (ready for next shot)")
            else:
                missing = expected_bytes - received
                self._set_status(f"Incomplete read — {received}/{expected_bytes} bytes")
                self._sig_show_warning.emit(
                    "Incomplete Data Transfer",
                    f"Only {received} of {expected_bytes} bytes were received ({missing} missing).\n\n"
                    f"How to fix:\n"
                    f"  1. Reduce Rec [ms] (e.g. try 20 ms instead of {cfg['rec_time_ms']:.0f} ms)\n"
                    f"  2. Lower Freq [Hz] (e.g. try {max(100, cfg['samplefreq'] // 2):.0f} Hz)\n"
                    f"  3. Use fewer channels (disable unused Ch slots)\n"
                    f"  4. Click Configure again, then Single Shot\n\n"
                    f"The firmware buffer is 8000 bytes total. Your current setup needs "
                    f"{expected_bytes} bytes which is within the limit, but the serial "
                    f"link may be dropping bytes at this rate."
                )

        except Exception as e:
            logging.exception("Scope record failed")
            self._set_status(f"Record error: {e}")
        finally:
            self._sig_update_buttons.emit()

    # ══════════════════════════════════════════════════════════════════════════
    #  RT ROLLING BUFFER
    # ══════════════════════════════════════════════════════════════════════════

    def _rt_init_buffer(self, cfg):
        """Pre-allocate 30-second circular numpy arrays for the RT rolling buffer."""
        fs = cfg['samplefreq']
        cap = max(64, int(30.0 * fs))
        self._rt_buf_data  = [np.full(cap, np.nan, dtype=np.float32) for _ in range(4)]
        self._rt_buf_time  = np.full(cap, np.nan, dtype=np.float64)
        self._rt_buf_cap   = cap
        self._rt_buf_head  = 0
        self._rt_buf_count = 0
        self._rt_buf_cfg   = cfg
        self._rt_panned    = False
        self._rt_t0        = None

    def _rt_buf_push(self, ch_data, t_start_abs: float):
        """Write one parsed frame into the circular buffer.

        ch_data: list of 4 lists (from _parse_buffer).
        t_start_abs: wall-clock time.time() at frame start.
        """
        if self._rt_buf_data is None:
            return
        cfg = self._rt_buf_cfg
        fs  = cfg['samplefreq']
        n   = cfg['n_samples']
        cap = self._rt_buf_cap

        if self._rt_t0 is None:
            self._rt_t0 = t_start_abs

        t_offset_start = t_start_abs - self._rt_t0
        for i in range(n):
            t = t_offset_start + i / fs
            idx = self._rt_buf_head % cap
            self._rt_buf_time[idx] = t
            for ch in range(4):
                if ch_data[ch]:
                    self._rt_buf_data[ch][idx] = ch_data[ch][i] if i < len(ch_data[ch]) else np.nan
                else:
                    self._rt_buf_data[ch][idx] = np.nan
            self._rt_buf_head += 1
            if self._rt_buf_count < cap:
                self._rt_buf_count += 1

    def _rt_buf_get_view(self):
        """Return (t_ms, ch_arrays) from circular buffer in chronological order.

        Applies progressive downsampling:
          - last 5 s  → full resolution
          - 5–15 s    → 2× decimated
          - 15–30 s   → 4× decimated
        """
        cap   = self._rt_buf_cap
        count = self._rt_buf_count
        if count == 0:
            return None, None

        if count < cap:
            t_raw = self._rt_buf_time[:count].copy()
            ch_raw = [self._rt_buf_data[ch][:count].copy() for ch in range(4)]
        else:
            head = self._rt_buf_head % cap
            idx = np.arange(count)
            order = (head + idx) % cap
            t_raw  = self._rt_buf_time[order]
            ch_raw = [self._rt_buf_data[ch][order] for ch in range(4)]

        if len(t_raw) == 0:
            return None, None

        t_max = t_raw[-1]
        cutoff_full = t_max - 5.0
        cutoff_2x   = t_max - 15.0

        mask_full = t_raw >= cutoff_full
        mask_2x   = (t_raw >= cutoff_2x) & ~mask_full
        mask_4x   = t_raw < cutoff_2x

        def _decimate(arr, step):
            return arr[::step]

        t_parts = []
        ch_parts = [[] for _ in range(4)]

        if np.any(mask_4x):
            t_parts.append(_decimate(t_raw[mask_4x], 4))
            for ch in range(4):
                ch_parts[ch].append(_decimate(ch_raw[ch][mask_4x], 4))
        if np.any(mask_2x):
            t_parts.append(_decimate(t_raw[mask_2x], 2))
            for ch in range(4):
                ch_parts[ch].append(_decimate(ch_raw[ch][mask_2x], 2))
        if np.any(mask_full):
            t_parts.append(t_raw[mask_full])
            for ch in range(4):
                ch_parts[ch].append(ch_raw[ch][mask_full])

        if not t_parts:
            return None, None

        t_out  = np.concatenate(t_parts) * 1000.0  # convert to ms
        ch_out = [np.concatenate(ch_parts[ch]) for ch in range(4)]
        return t_out, ch_out

    def _rt_update_live_badge(self):
        """Set badge to green LIVE or grey HIST depending on pan state."""
        if self._rt_panned:
            self._lbl_live_badge.setObjectName("sc_live_badge_hist")
            self._lbl_live_badge.setText("◌ HIST")
        else:
            self._lbl_live_badge.setObjectName("sc_live_badge_live")
            self._lbl_live_badge.setText("● LIVE")
        self._lbl_live_badge.style().unpolish(self._lbl_live_badge)
        self._lbl_live_badge.style().polish(self._lbl_live_badge)

    def _rt_emit_plot(self, cfg):
        """Build a plot from the rolling buffer and emit _sig_plot if not panned,
        or just update the badge if panned (user is inspecting history)."""
        t_ms, ch_out = self._rt_buf_get_view()
        if t_ms is None:
            return
        # Build ch_data as lists (as _do_plot expects)
        ch_codes = cfg['ch_codes']
        ch_data = [[] for _ in range(4)]
        for ch in range(4):
            if ch_codes[ch] > 0:
                ch_data[ch] = ch_out[ch].tolist()

        if not self._rt_panned:
            # Auto-scroll: show full buffer window, live edge at right
            self._sig_plot.emit(ch_data, (t_ms / 1000.0).tolist(), cfg)
        else:
            # User has panned — only update live values strip, leave plot alone
            pass

        QTimer.singleShot(0, self._rt_update_live_badge)

    # ══════════════════════════════════════════════════════════════════════════
    #  WORKER: REAL TIME
    # ══════════════════════════════════════════════════════════════════════════

    def _worker_realtime(self):
        logging.debug("SCOPE _worker_realtime: starting")
        self._set_status("Real-time running...")
        QTimer.singleShot(0, lambda: self._live_strip.setVisible(True))
        self.serial_manager.scope_active.set()

        try:
            if not self.last_config:
                self._set_status("Not configured")
                return

            cfg = self.last_config
            self._rt_init_buffer(cfg)
            self._rt_incomplete_count = 0

            frame = 0
            while self._realtime_running:
                if not self.serial_manager.is_open:
                    self._set_status("Serial port closed")
                    break

                expected_bytes = cfg['expected_bytes']
                rec_time_s     = cfg['rec_time_s']

                try:
                    t_frame_start = time.time()
                    with self.serial_manager._lock:
                        ser = self.serial_manager._ser
                        ser.write(f"#s recptr {dec_encode(0.0)};\n".encode("ascii"))
                        time.sleep(0.02)
                        time.sleep(rec_time_s + 0.05)
                        ser.reset_input_buffer()
                        ser.write(b"#g recbuf ;\n")
                        logging.info("SCOPE: Sent #g recbuf ; (expecting %d bytes)", expected_bytes)
                        # Expert-style polling loop: up to 5 s, reads in chunks
                        rt_buf_acc = bytearray()
                        timeout_count = 0
                        timeout_max = 500  # 500 × 10 ms = 5 s
                        while len(rt_buf_acc) < expected_bytes and timeout_count < timeout_max:
                            available = ser.in_waiting
                            if available > 0:
                                to_read = min(available, expected_bytes - len(rt_buf_acc))
                                chunk = ser.read(to_read)
                                if chunk:
                                    rt_buf_acc += chunk
                                    timeout_count = 0
                            else:
                                timeout_count += 1
                                time.sleep(0.01)
                        rt_buf = bytes(rt_buf_acc)
                        received = len(rt_buf)
                        leftover = ser.in_waiting
                        if leftover > 0:
                            logging.warning("SCOPE: %d extra bytes cleared", leftover)
                            ser.reset_input_buffer()

                    self._set_bytes(received, expected_bytes)
                    frame += 1
                    # Always push and plot whatever was received — partial is better than nothing
                    if len(rt_buf) > 0:
                        ch_data = self._parse_buffer(bytes(rt_buf), cfg)
                        self._rt_buf_push(ch_data, t_frame_start)
                        self._rt_emit_plot(cfg)
                    if received == expected_bytes:
                        self._set_status(f"Real-time — frame {frame}")
                    else:
                        self._rt_incomplete_count += 1
                        self._set_status(
                            f"Real-time: incomplete frame {self._rt_incomplete_count} "
                            f"({received}/{expected_bytes} B)"
                        )
                        if self._rt_incomplete_count == 1:
                            self._sig_show_warning.emit(
                                "Real-Time: Incomplete Data",
                                f"Frame received only {received} of {expected_bytes} bytes.\n\n"
                                f"The serial link cannot keep up at this data rate. To fix:\n"
                                f"  1. Stop Real Time\n"
                                f"  2. Increase Rec [ms] (e.g. 50 ms or more)\n"
                                f"  3. Lower Freq [Hz] (e.g. {max(100, cfg['samplefreq'] // 2):.0f} Hz)\n"
                                f"  4. Use fewer channels\n"
                                f"  5. Click Configure again, then Real Time"
                            )

                except Exception as e:
                    self._set_status(f"RT error: {e}")
                    break

                if not self._realtime_running:
                    break

        except Exception as e:
            logging.exception("Real-time loop failed")
            self._set_status(f"Real-time error: {e}")
        finally:
            self.serial_manager.scope_active.clear()
            self._realtime_running = False
            self._set_status("Real-time stopped — last frame preserved")
            self._sig_update_buttons.emit()
            QTimer.singleShot(0, lambda: self._live_strip.setVisible(False))

        logging.debug("SCOPE _worker_realtime: stopped")

    # ══════════════════════════════════════════════════════════════════════════
    #  SCROLL MODE  (QTimer-based, matching reference scope.py architecture)
    # ══════════════════════════════════════════════════════════════════════════

    def _scroll_setup_axes(self, ch_codes, t_display, ring_size):
        """Build axes and one animated Line2D per active channel. Capture blit background."""
        p = _get_palette()
        dark = self._is_dark(p)
        grid_color = "#3A3A5C" if dark else "#E8EAF0"

        zeros     = np.zeros(ring_size)
        time_axis = np.linspace(-t_display, 0.0, ring_size)

        self.ax.cla()
        self.ax.set_facecolor(p['input_bg'])
        self.fig.patch.set_facecolor(p['card'])
        self.ax.set_xlabel("Time (s)", color=p['muted'], fontsize=9, fontweight='semibold', labelpad=2)
        self.ax.set_ylabel("Amplitude", color=p['muted'], fontsize=9, fontweight='semibold', labelpad=2)
        self.ax.set_xlim(-t_display, 0.0)
        self.ax.set_ylim(-1.0, 1.0)
        self.ax.tick_params(colors=p['muted'], labelsize=8)
        for spine in self.ax.spines.values():
            spine.set_color(p['border'])
            spine.set_linewidth(0.8)
        self.ax.grid(True, color=grid_color, linewidth=0.8, alpha=0.9, linestyle='--')

        self._scroll_lines = {}
        for ch_idx in range(4):
            if ch_codes[ch_idx] > 0:
                var_name = VARIABLE_NAMES[ch_codes[ch_idx]]
                line, = self.ax.plot(time_axis, zeros,
                                     label=var_name,
                                     color=CHANNEL_COLORS[ch_idx],
                                     linewidth=1.4,
                                     animated=True)
                self._scroll_lines[ch_idx] = line

        self.ax.legend(
            loc='upper right',
            ncol=1, fontsize=9,
            facecolor=p['card'], edgecolor=p['border'], labelcolor=p['text'],
            framealpha=0.85,
            borderpad=0.5, handlelength=1.2, handleheight=0.9,
            handletextpad=0.5,
        )
        self.canvas.draw()
        self._scroll_bg = self.canvas.copy_from_bbox(self.ax.bbox)
        logging.debug("SCOPE _scroll_setup_axes: built with ring_size=%d", ring_size)

    def _scroll_poll_half(self):
        """QTimer slot — called every 5 ms on the main thread.
        Tries to acquire serial lock non-blocking; if busy, reschedules automatically
        (QTimer fires again in 5 ms). When rechalf==1 spawns a background thread to
        read the buffer without blocking the UI."""
        if not self._scroll_running:
            if self._scroll_poll_timer is not None:
                self._scroll_poll_timer.stop()
            return

        if not self.serial_manager._lock.acquire(blocking=False):
            return   # busy — QTimer will try again in 5 ms

        try:
            ser = self.serial_manager._ser
            ser.write(b"#g rechalf;\n")
            try:
                ser.readline()   # discard "->" prompt
                resp = ser.readline().decode("ascii", errors="ignore").strip()
                half = int(float(resp))
            except Exception:
                half = 0
        except Exception:
            half = 0
        finally:
            self.serial_manager._lock.release()

        if half == 1:
            # Stop poll timer while the background read is in progress
            if self._scroll_poll_timer is not None:
                self._scroll_poll_timer.stop()
            threading.Thread(target=self._scroll_read_buffer, daemon=True).start()

    def _scroll_read_buffer(self):
        """Background thread: read binary buffer, clear rechalf, push to ring deques.
        Re-starts the poll timer when done."""
        if not self._scroll_running or self.last_config is None:
            return
        try:
            cfg            = self.last_config
            ch_codes       = cfg['ch_codes']
            ch_datatypes   = cfg['ch_datatypes']
            num_samples    = cfg['n_samples']
            expected_bytes = cfg['expected_bytes']
            sample_rate    = cfg['samplefreq']
            clear_half_cmd = f"#s rechalf {dec_encode(0.0)};\n".encode("ascii")
            t_read         = time.monotonic() - self._scroll_t0

            with self.serial_manager._lock:
                ser = self.serial_manager._ser
                ser.reset_input_buffer()
                ser.write(b"#g recbuf ;\n")

                buf      = bytearray()
                deadline = time.monotonic() + 1.0
                while len(buf) < expected_bytes:
                    available = ser.in_waiting
                    if available:
                        buf += ser.read(min(available, expected_bytes - len(buf)))
                    elif time.monotonic() > deadline:
                        break
                    else:
                        time.sleep(0.001)

                received = len(buf)
                if ser.in_waiting:
                    ser.reset_input_buffer()
                ser.write(clear_half_cmd)

            self._sig_bytes.emit(received, expected_bytes)

            if received == expected_bytes:
                channels_data = self._parse_buffer(bytes(buf), cfg, num_samples)
                # Push NaN separator then new block into deques (matching reference)
                block_times = [float('nan')] + [
                    t_read - (num_samples - 1 - i) / sample_rate
                    for i in range(num_samples)
                ]
                if self._scroll_rings is not None:
                    if self._scroll_t_ring is not None:
                        self._scroll_t_ring.extend(block_times)
                    for ch_idx in range(4):
                        if ch_codes[ch_idx] > 0:
                            self._scroll_rings[ch_idx].extend(
                                [float('nan')] + channels_data[ch_idx])
                self._scroll_frame_count += 1
                self._sig_status.emit(f"Scroll — frame {self._scroll_frame_count}")
            else:
                self._sig_status.emit(f"Scroll — incomplete ({received}/{expected_bytes} B)")

        except Exception:
            logging.exception("Scroll read buffer failed")
        finally:
            # Restart poll timer from main thread
            if self._scroll_running:
                QTimer.singleShot(0, self._restart_scroll_poll)

    def _restart_scroll_poll(self):
        """Restart the poll timer after a buffer read cycle."""
        if self._scroll_running and self._scroll_poll_timer is not None:
            self._scroll_poll_timer.start(5)

    def _scroll_display_tick(self):
        """Called every 20 ms by QTimer. Blits updated line data."""
        if not self._scroll_running:
            if self._scroll_display_timer is not None:
                self._scroll_display_timer.stop()
            return

        try:
            if self._scroll_bg is None or not self._scroll_lines:
                return
            if self._scroll_rings is None:
                return

            ch_codes   = self.last_config['ch_codes']
            t_display  = self._scroll_t_display
            snapshots  = [np.array(ring) for ring in self._scroll_rings]
            t_abs      = np.array(self._scroll_t_ring) if self._scroll_t_ring is not None else None

            if t_abs is not None:
                t_now     = time.monotonic() - self._scroll_t0
                time_axis = t_abs - t_now
            else:
                ring_size = len(snapshots[0]) if snapshots else 1
                time_axis = np.linspace(-t_display, 0.0, ring_size)

            # Auto-scale Y across all active channels
            all_vals = np.concatenate([snapshots[i] for i in range(4)
                                       if ch_codes[i] > 0 and len(snapshots[i])])
            valid = all_vals[np.isfinite(all_vals)]
            if len(valid) > 0:
                ymin, ymax = float(valid.min()), float(valid.max())
                margin = (ymax - ymin) * 0.1 if ymax != ymin else 1.0
                new_ylim = (ymin - margin, ymax + margin)
                cur_ylim = self.ax.get_ylim()
                if (abs(new_ylim[0] - cur_ylim[0]) > margin * 0.5 or
                        abs(new_ylim[1] - cur_ylim[1]) > margin * 0.5):
                    if self._ylim_locked is None:
                        self.ax.set_ylim(new_ylim)
                        self.canvas.draw()
                        self._scroll_bg = self.canvas.copy_from_bbox(self.ax.bbox)

            self.canvas.restore_region(self._scroll_bg)
            for ch_idx, line in self._scroll_lines.items():
                line.set_xdata(time_axis)
                line.set_ydata(snapshots[ch_idx])
                self.ax.draw_artist(line)
            self.canvas.blit(self.ax.bbox)

            self._scroll_frame_count += 1

        except Exception as e:
            logging.debug("SCOPE _scroll_display_tick error: %s", e)

    # ══════════════════════════════════════════════════════════════════════════
    #  DATA PARSING
    # ══════════════════════════════════════════════════════════════════════════

    def _parse_buffer(self, raw: bytes, cfg: dict, n_samples: int = None):
        if n_samples is None:
            n_samples = cfg['n_samples']

        ch_codes     = cfg['ch_codes']
        ch_datatypes = cfg['ch_datatypes']
        channels_data = [[] for _ in range(4)]
        byte_offset   = 0

        for ch_idx in range(4):
            if ch_codes[ch_idx] > 0:
                dt = ch_datatypes[ch_idx]
                fmt, stride = ('<f', 4) if dt == 1 else (('<H', 2) if dt == 2 else ('<h', 2))
                for i in range(n_samples):
                    off = byte_offset + i * stride
                    if off + stride <= len(raw):
                        channels_data[ch_idx].append(float(struct.unpack(fmt, raw[off:off+stride])[0]))
                    else:
                        channels_data[ch_idx].append(0.0)
                byte_offset += n_samples * stride

        return channels_data

    def _parse_and_plot_data(self, raw: bytes, cfg: dict):
        ch_data = self._parse_buffer(raw, cfg)
        n  = cfg['n_samples']
        fs = cfg['samplefreq']
        t_axis = list(np.arange(n) / fs)
        self._sig_plot.emit(ch_data, t_axis, cfg)

    def _do_plot(self, ch_data, t_axis, cfg):
        p = _get_palette()
        dark = self._is_dark(p)
        grid_color = "#3A3A5C" if dark else "#E8EAF0"

        self.ax.cla()
        self.ax.set_facecolor(p['input_bg'])
        self.fig.patch.set_facecolor(p['card'])

        ch_codes    = cfg['ch_codes']
        any_plotted = False

        self._plotted_lines = {}  # ch_idx -> Line2D
        for i, samples in enumerate(ch_data):
            if not samples:
                continue
            code     = ch_codes[i]
            var_name = VARIABLE_NAMES.get(code, f"Ch{i+1}")
            unit     = VARIABLE_UNITS.get(var_name, "")
            label    = f"{var_name} [{unit}]" if unit else var_name
            line, = self.ax.plot(t_axis, samples, color=CHANNEL_COLORS[i], linewidth=1.4,
                                 label=label, zorder=2, picker=5)
            self._plotted_lines[i] = line
            any_plotted = True
            # Update live labels
            if self._live_labels and i < len(self._live_labels):
                last_val = samples[-1] if samples else float('nan')
                unit_str = f" {unit}" if unit else ""
                self._live_labels[i].setText(f"{var_name}: {last_val:.4g}{unit_str}")

        if any_plotted:
            n_plotted = sum(1 for s in ch_data if s)
            ncols = max(1, min(n_plotted, 4))
            leg = self.ax.legend(
                loc='upper right',
                ncol=1,
                fontsize=9,
                facecolor=p['card'],
                edgecolor=p['border'],
                framealpha=0.85,
                borderpad=0.5,
                handlelength=1.2,
                handleheight=0.9,
                handletextpad=0.5,
                columnspacing=1.0,
            )
            self._leg_line_map = {}
            for i, (leg_line, (ch_idx, orig_line)) in enumerate(
                    zip(leg.get_lines(), self._plotted_lines.items())):
                leg_line.set_linewidth(2.0)
                leg_line.set_solid_capstyle('round')
                leg_line.set_picker(8)
                self._leg_line_map[leg_line] = orig_line
                txt = leg.get_texts()[i]
                txt.set_fontsize(8)
                txt.set_fontweight('semibold')
                txt.set_picker(8)
                self._leg_line_map[txt] = orig_line
                if not orig_line.get_visible():
                    txt.set_alpha(0.35)
                    txt.set_color(p['muted'])
                    leg_line.set_alpha(0.25)
            leg.set_title("")
            if self._leg_pick_cid is not None:
                self.canvas.mpl_disconnect(self._leg_pick_cid)
            self._leg_pick_cid = self.canvas.mpl_connect('pick_event', self._on_legend_pick)
            self._legend_obj = leg

        self.ax.set_xlabel("Time (s)", color=p['muted'], fontsize=9, fontweight='semibold', labelpad=2)
        self.ax.set_ylabel("Amplitude", color=p['muted'], fontsize=9, fontweight='semibold', labelpad=2)
        self.ax.tick_params(colors=p['muted'], labelsize=8)
        for spine in self.ax.spines.values():
            spine.set_color(p['border'])
            spine.set_linewidth(0.8)
        self.ax.grid(True, which='major', color=grid_color, linewidth=0.8, alpha=0.9, linestyle='--')
        self.ax.minorticks_on()
        self.ax.grid(True, which='minor', color=grid_color, linewidth=0.3, alpha=0.4, linestyle=':')
        self._crosshair_v = None
        self._blit_bg = None
        self._clear_ab_lines()
        if self._ylim_locked is not None:
            self.ax.set_ylim(self._ylim_locked)
        try:
            self.fig.tight_layout(pad=0.2)
        except Exception:
            pass
        self.canvas.draw()

        # Store auto-scale bounds so zoom-out can be clamped
        self._data_xlim = tuple(self.ax.get_xlim())
        self._data_ylim = tuple(self.ax.get_ylim())

        self._has_plot_data = True
        self._last_plot_data = (ch_data, t_axis, cfg)
        self._btn_export.setEnabled(True)

    # ══════════════════════════════════════════════════════════════════════════
    #  EXPORT
    # ══════════════════════════════════════════════════════════════════════════

    def _on_export_clicked(self):
        if self._last_plot_data is None:
            return
        ch_data, t_axis, cfg = self._last_plot_data
        p = _get_palette()

        path, sel_filter = QFileDialog.getSaveFileName(
            self, "Export Waveform", "",
            "CSV Data (*.csv);;PNG Image (*.png)"
        )
        if not path:
            return

        try:
            if sel_filter.startswith("PNG") or path.lower().endswith(".png"):
                if not path.lower().endswith(".png"):
                    path += ".png"
                self.fig.savefig(path, dpi=150, bbox_inches='tight', facecolor=p['card'])
                self._set_status(f"Saved PNG: {os.path.basename(path)}")
            else:
                if not path.lower().endswith(".csv"):
                    path += ".csv"
                ch_codes = cfg['ch_codes']
                headers  = ["time_s"]
                for i, samples in enumerate(ch_data):
                    if samples:
                        headers.append(VARIABLE_NAMES.get(ch_codes[i], f"Ch{i+1}"))
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(",".join(headers) + "\n")
                    for j, t in enumerate(t_axis):
                        row = [f"{t:.6g}"]
                        for samples in ch_data:
                            if samples:
                                row.append(f"{samples[j]:.6g}" if j < len(samples) else "")
                        f.write(",".join(row) + "\n")
                self._set_status(f"Saved CSV: {os.path.basename(path)}")
        except Exception as e:
            logging.exception("Export failed")
            self._set_status(f"Export error: {e}")

    # ══════════════════════════════════════════════════════════════════════════
    #  CLOSE EVENT
    # ══════════════════════════════════════════════════════════════════════════

    def closeEvent(self, event):
        self._realtime_running = False
        self._scroll_running   = False
        self._save_session_config()
        for t in (self._scroll_poll_timer, self._scroll_display_timer,
                  self._no_port_timer,
                  getattr(self, '_spinner_timer', None)):
            if t is not None:
                t.stop()
        for cid_attr in ('_pan_release_cid', '_leg_pick_cid'):
            cid = getattr(self, cid_attr, None)
            if cid is not None:
                self.canvas.mpl_disconnect(cid)
                setattr(self, cid_attr, None)
        super().closeEvent(event)


# ═══════════════════════════════════════════════════════════════════════════════
#  STANDALONE TEST
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    from PySide6.QtWidgets import QApplication

    class _DummySerial:
        is_open = True
        _lock   = threading.Lock()

        def send(self, cmd, expect_response=True):
            if "rechalf" in cmd:
                return "+0.0000000 "
            return "+0.0000000 "

    app = QApplication(sys.argv)
    app.setFont(QFont("Segoe UI", 10))
    dlg = ScopeWindow(None, _DummySerial(), fpwm=16000.0)
    dlg.show()
    sys.exit(app.exec())
