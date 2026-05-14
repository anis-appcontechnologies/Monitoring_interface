#!/usr/bin/env python3
"""
Mechanical Parameters (Inertia + Speed PI) — PySide6 edition

Behavior preserved 1:1 from inertia_param.py (Tkinter):
  • Inertia identification: validate accel-current-ratio (10–70%),
    s jacl {ratio}; s jidn; poll g qusize until 0 (timeout 35s);
    g jres / g fric.
  • Speed PI tuning (pole-placement):
      requires J, Dg, t50; reads PsiF, P, Tg, Fpwm;
      validates t50 ≥ ln(2)/(Dy_max) where Dy_max = 1/(2·Dg·Tg);
      Dy = ln(2) / (t50/1000); Km = 45·P·PsiF / (π·J);
      p3 = 1/Tg − 2·Dg·Dy; Kp = (Tg·Dy² + 2·Dg·Dy − 4·Dg²·Dy²·Tg) / Km;
      Tn = Km·Kp / (Tg·Dy²·p3); Ki = Kp·Tpwm / Tn;
      writes mtheta=J, damp=Dg, dyn=Dy, then s susp.
  • Apply: s apspd (no response).
"""

import logging
import math
import threading
import time

from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QFont, QDoubleValidator
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QLineEdit,
    QPushButton, QFrame, QSizePolicy, QApplication,
)

from si_format import si_format, si_format_inertia, si_format_friction

try:
    import qtawesome as qta
    _QTA = True
except ImportError:
    _QTA = False


from protocol import dec_encode, dec_decode

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(message)s")


def _badge(text: str, level: str = "info") -> str:
    """Return HTML for a styled inline badge. level: 'info'|'warn'|'ok'|'muted'"""
    colors = {
        "info":  ("#1565C0", "#E3F2FD"),
        "ok":    ("#1B5E20", "#E8F5E9"),
        "warn":  ("#E65100", "#FFF3E0"),
        "error": ("#B71C1C", "#FFEBEE"),
        "muted": ("#5A5A5A", "#F0F0F0"),
    }
    fg, bg = colors.get(level, colors["info"])
    return (f"<span style='font-size:11px; font-weight:700; color:{fg}; "
            f"background:{bg}; border-radius:3px; padding:2px 6px;'>{text}</span>")


def _get_palette():
    """Return the active palette from amc_interface_qt, falling back to light defaults."""
    try:
        import amc_interface_qt as _amcqt
        return _amcqt.C
    except Exception:
        return {
            "white": "#FFFFFF", "card": "#FFFFFF", "bg": "#F0F0F0", "border": "#D8D8D8",
            "text": "#1A1A1A", "text2": "#3A3A3A", "muted": "#707070",
            "red": "#C0272D", "red_bg": "#F9ECEC", "red_border": "#E8AAAC",
            "orange": "#C07820", "orange_bg": "#FDF3E3", "orange_border": "#E8C87A",
            "blue": "#C0272D", "blue_dark": "#9B1F24", "blue_light": "#F9ECEC",
            "green": "#3D8B37", "green_bg": "#EDF7EC", "green_border": "#A8D5A5",
            "input_bg": "#F7F7F7",
        }


def _modal(parent, title: str, msg: str, icon_name: str, icon_color: str):
    """Palette-aware modal used by both _show_error and _show_warn."""
    p = _get_palette()
    dlg = QDialog(parent)
    dlg.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
    dlg.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    dlg.setModal(True)
    outer = QVBoxLayout(dlg)
    outer.setContentsMargins(14, 14, 14, 14)
    card = QFrame()
    card.setObjectName("modal_card")
    card.setStyleSheet(
        f"QFrame#modal_card {{ background: {p['white']}; border-radius: 10px; border: 1px solid {p['border']}; }}"
        "QFrame#modal_card * { border: none; background: transparent; }")
    lay = QVBoxLayout(card)
    lay.setContentsMargins(24, 20, 24, 16)
    lay.setSpacing(10)
    if _QTA:
        ico = QLabel()
        ico.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ico.setPixmap(qta.icon(icon_name, color=icon_color).pixmap(32, 32))
        lay.addWidget(ico)
    ttl = QLabel(title)
    ttl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    ttl.setStyleSheet(f"font-size: 14px; font-weight: 700; color: {p['text']}; background: transparent;")
    lay.addWidget(ttl)
    body = QLabel(msg)
    body.setWordWrap(True)
    body.setAlignment(Qt.AlignmentFlag.AlignCenter)
    body.setStyleSheet(f"font-size: 12px; color: {p['text2']}; background: transparent;")
    lay.addWidget(body)
    btn_row = QHBoxLayout()
    btn_row.addStretch()
    ok_btn = QPushButton("OK")
    ok_btn.setCursor(Qt.CursorShape.PointingHandCursor)
    ok_btn.setStyleSheet(
        f"QPushButton {{ background: {p['blue']}; color: white; border: none; border-radius: 5px; "
        "padding: 7px 24px; font-size: 12px; font-weight: 700; }"
        f"QPushButton:hover {{ background: {p['blue_dark']}; }}")
    ok_btn.clicked.connect(dlg.accept)
    btn_row.addWidget(ok_btn)
    lay.addLayout(btn_row)
    outer.addWidget(card)
    dlg.setMinimumWidth(340)
    dlg.exec()


def _show_error(parent, title: str, msg: str):
    _modal(parent, title, msg, "fa5s.exclamation-circle", "#C0272D")


def _show_warn(parent, title: str, msg: str):
    _modal(parent, title, msg, "fa5s.exclamation-triangle", "#C07820")


class InertiaIdentification(QDialog):
    """Modern Qt port. Same constructor signature as the Tkinter class."""

    _POLL_INTERVAL_S = 1.0
    _TIMEOUT_S       = 35.0   # 30 s firmware timeout + margin

    _sig_status            = Signal(str)
    _sig_speed_status      = Signal(str)
    _sig_jacl_loaded       = Signal(float)
    _sig_ident_done        = Signal(float, float)              # j, fric
    _sig_ident_warn        = Signal(str)
    _sig_ident_error       = Signal(str)
    _sig_calc_done         = Signal(float, float, float, float)  # Kp, Ki, Dy, Km
    _sig_calc_error        = Signal(str)
    _sig_apply_done        = Signal()
    _sig_apply_error       = Signal(str)

    def __init__(self, parent, serial_manager, cmd_manager=None):
        super().__init__(parent)
        self.serial_manager = serial_manager
        self.cmd_manager    = cmd_manager

        self.setWindowTitle("Mechanical Parameters")
        self.setObjectName("ip_dialog")
        self.resize(820, 520)
        self.setMinimumSize(760, 480)

        self._stop_ident = False

        self._build_ui()
        self._apply_style()
        self._wire_signals()

        threading.Thread(target=self._load_initial_settings, daemon=True).start()

    # ── UI ─────────────────────────────────────────────────────────────────────
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(8)

        title = QLabel("Mechanical Parameters")
        title.setObjectName("ip_title")
        sub = QLabel("Identify motor inertia and tune the speed controller")
        sub.setObjectName("ip_sub")
        root.addWidget(title)
        root.addWidget(sub)

        # ── Two-column split: LEFT = inputs/actions  RIGHT = results ──────────
        cols = QHBoxLayout()
        cols.setSpacing(10)

        # ─── LEFT COLUMN ──────────────────────────────────────────────────────
        left_col = QVBoxLayout()
        left_col.setSpacing(8)

        # Card: Inertia Identification (inputs)
        ident_card = QFrame()
        ident_card.setObjectName("ip_card")
        i_lay = QVBoxLayout(ident_card)
        i_lay.setContentsMargins(14, 12, 14, 12)
        i_lay.setSpacing(8)

        i_hdr = QLabel("Inertia Identification")
        i_hdr.setObjectName("ip_card_title")
        i_lay.addWidget(i_hdr)

        info = QLabel("Motor starts from standstill.\n"
                      "Firmware accelerates then decelerates automatically.\n"
                      "Sequence takes up to 30 s.")
        info.setObjectName("ip_info")
        i_lay.addWidget(info)

        accel_row = QHBoxLayout()
        accel_row.setSpacing(8)
        accel_lbl = QLabel("Identification Current (% of IsqMax)")
        accel_lbl.setObjectName("ip_field")
        accel_lbl.setToolTip("Acceleration current as % of IsqMax (10–70%)")
        accel_row.addWidget(accel_lbl)

        self.accel_ratio_entry = self._mk_entry()
        self.accel_ratio_entry.setText("25")
        self.accel_ratio_entry.setFixedWidth(80)
        accel_row.addWidget(self.accel_ratio_entry)
        accel_row.addWidget(self._unit_lbl("%"))
        accel_row.addStretch()
        i_lay.addLayout(accel_row)

        ident_btn_row = QHBoxLayout()
        ident_btn_row.setSpacing(6)

        self.start_btn = QPushButton("Start Identification")
        self.start_btn.setObjectName("ip_btn_primary")
        self.start_btn.setToolTip("Trigger inertia identification (jidn). "
                                  "Motor must be stopped and sensorless mode active.")
        self.start_btn.setMinimumHeight(34)
        self.start_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.start_btn.clicked.connect(self.on_start_identification)
        if _QTA:
            self.start_btn.setIcon(qta.icon("fa5s.play", color="#FFFFFF"))
            self.start_btn.setIconSize(QSize(13, 13))
        ident_btn_row.addWidget(self.start_btn, 1)

        self.stop_ident_btn = QPushButton("Stop")
        self.stop_ident_btn.setObjectName("ip_btn_secondary")
        self.stop_ident_btn.setToolTip("Abort running identification")
        self.stop_ident_btn.setMinimumHeight(34)
        self.stop_ident_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.stop_ident_btn.setEnabled(False)
        self.stop_ident_btn.clicked.connect(self._on_stop_ident_clicked)
        if _QTA:
            self.stop_ident_btn.setIcon(qta.icon("fa5s.stop", color="#B71C1C"))
            self.stop_ident_btn.setIconSize(QSize(13, 13))
        ident_btn_row.addWidget(self.stop_ident_btn)

        i_lay.addLayout(ident_btn_row)

        self.status_label = QLabel()
        self.status_label.setTextFormat(Qt.TextFormat.RichText)
        self.status_label.setText(_badge("idle", "muted"))
        self.status_label.setObjectName("ip_status")
        i_lay.addWidget(self.status_label)

        left_col.addWidget(ident_card)

        # Card: Speed PI Tuning (inputs)
        speed_card = QFrame()
        speed_card.setObjectName("ip_card")
        s_lay = QVBoxLayout(speed_card)
        s_lay.setContentsMargins(14, 12, 14, 12)
        s_lay.setSpacing(8)

        s_hdr = QLabel("Tune Speed Controller (Pole Placement)")
        s_hdr.setObjectName("ip_card_title")
        s_lay.addWidget(s_hdr)

        form = QGridLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)
        form.setColumnStretch(1, 1)

        self.j_entry = self._mk_entry()
        self.j_entry.setToolTip("Enter inertia in kg·m² or leave empty to use identified value")
        self._add_form_row(form, 0, "Inertia J", self.j_entry, "kg·m²",
                           "Motor inertia (from identification or manual value).\n"
                           "Leave empty to read from firmware, or enter custom value.")

        self.dg_entry = self._mk_entry()
        self.dg_entry.setText("0.707")
        self.dg_entry.setToolTip("Damping ratio (any positive value)\nHigher Dg reduces Dy limit")
        self._add_form_row(form, 1, "Damping ratio Dg", self.dg_entry, "—",
                           "0.707 = Butterworth (good compromise)\n"
                           "1.0 = aperiodic (no overshoot)\n"
                           "Dy limit: Dy < 1 / (2×Dg×Tg)")

        self.t50_entry = self._mk_entry()
        self.t50_entry.setText("50")
        self.t50_entry.setToolTip("Time [ms] to reach 50% of commanded speed step")
        self._add_form_row(form, 2, "Time to 50% speed t50", self.t50_entry, "ms",
                           "Rise time to 50% of target speed.\n"
                           "Recommended: 20..100 ms for sensorless.\n"
                           "Minimum depends on Dg and Tg.")
        s_lay.addLayout(form)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self.calc_btn = QPushButton("Calculate & Preview")
        self.calc_btn.setObjectName("ip_btn_secondary")
        self.calc_btn.setToolTip("Compute Kp/Ki from identified or custom inertia.\n"
                                 "Leave J empty to read from firmware, or enter custom value.")
        self.calc_btn.setMinimumHeight(34)
        self.calc_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.calc_btn.clicked.connect(self.on_calculate_speed_pi)
        if _QTA:
            self.calc_btn.setIcon(qta.icon("fa5s.calculator", color="#1976D2"))
            self.calc_btn.setIconSize(QSize(13, 13))
        btn_row.addWidget(self.calc_btn, 1)

        self.apply_btn = QPushButton("Apply to Controller")
        self.apply_btn.setObjectName("ip_btn_primary")
        self.apply_btn.setToolTip("Apply calculated Kp/Ki to the active speed PI controller.")
        self.apply_btn.setEnabled(False)
        self.apply_btn.setMinimumHeight(34)
        self.apply_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.apply_btn.clicked.connect(self.on_apply_speed_pi)
        if _QTA:
            self.apply_btn.setIcon(qta.icon("fa5s.check", color="#FFFFFF"))
            self.apply_btn.setIconSize(QSize(13, 13))
        btn_row.addWidget(self.apply_btn, 1)

        s_lay.addLayout(btn_row)

        self.speed_status_label = QLabel("")
        self.speed_status_label.setTextFormat(Qt.TextFormat.RichText)
        self.speed_status_label.setObjectName("ip_status")
        s_lay.addWidget(self.speed_status_label)

        left_col.addWidget(speed_card)
        left_col.addStretch(1)

        # ─── RIGHT COLUMN ─────────────────────────────────────────────────────
        right_col = QVBoxLayout()
        right_col.setSpacing(8)

        # Card: Inertia Results
        results_card = QFrame()
        results_card.setObjectName("ip_card")
        r_lay = QVBoxLayout(results_card)
        r_lay.setContentsMargins(14, 12, 14, 12)
        r_lay.setSpacing(6)

        r_hdr = QLabel("Identified Inertia Parameters")
        r_hdr.setObjectName("ip_card_title")
        r_lay.addWidget(r_hdr)

        inertia_row = QHBoxLayout()
        inertia_row.setSpacing(6)
        inertia_name = QLabel("Inertia J")
        inertia_name.setObjectName("ip_res_name")
        inertia_name.setFixedWidth(120)
        inertia_name.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.inertia_label = QLabel("—")
        self.inertia_label.setObjectName("ip_res_val")
        self.inertia_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        inertia_row.addWidget(inertia_name)
        inertia_row.addWidget(self.inertia_label, 1)
        r_lay.addLayout(inertia_row)

        friction_row = QHBoxLayout()
        friction_row.setSpacing(6)
        friction_name = QLabel("Friction coeff B")
        friction_name.setObjectName("ip_res_name")
        friction_name.setFixedWidth(120)
        friction_name.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.friction_label = QLabel("—")
        self.friction_label.setObjectName("ip_res_val")
        self.friction_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        friction_row.addWidget(friction_name)
        friction_row.addWidget(self.friction_label, 1)
        r_lay.addLayout(friction_row)
        right_col.addWidget(results_card)

        # Card: Speed PI Results
        pi_card = QFrame()
        pi_card.setObjectName("ip_card")
        pi_lay = QVBoxLayout(pi_card)
        pi_lay.setContentsMargins(14, 12, 14, 12)
        pi_lay.setSpacing(6)

        pi_hdr = QLabel("Calculated Speed PI Gains")
        pi_hdr.setObjectName("ip_card_title")
        pi_lay.addWidget(pi_hdr)

        _PI_UNITS  = ["rad/s", "RPM/(A·s)", "A/RPM", "A/RPM·tick"]
        _PI_NAMES  = ["Dy", "Km", "Kp", "Ki"]

        def _pi_row(name, unit):
            wrapper = QFrame()
            wrapper.setObjectName("ip_pi_row")
            w_lay = QHBoxLayout(wrapper)
            w_lay.setContentsMargins(10, 6, 10, 6)
            w_lay.setSpacing(6)
            name_lbl = QLabel(name)
            name_lbl.setObjectName("ip_pi_name")
            val_lbl = QLabel("—")
            val_lbl.setObjectName("ip_pi_val")
            val_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            unit_lbl = QLabel(unit)
            unit_lbl.setObjectName("ip_pi_unit")
            unit_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            w_lay.addWidget(name_lbl)
            w_lay.addWidget(val_lbl, 1)
            w_lay.addWidget(unit_lbl)
            pi_lay.addWidget(wrapper)
            return val_lbl

        self._pi_dy_val  = _pi_row(_PI_NAMES[0], _PI_UNITS[0])
        self._pi_km_val  = _pi_row(_PI_NAMES[1], _PI_UNITS[1])
        self._pi_kp_val  = _pi_row(_PI_NAMES[2], _PI_UNITS[2])
        self._pi_ki_val  = _pi_row(_PI_NAMES[3], _PI_UNITS[3])
        self.preview_label = QLabel("")  # kept for compat (not displayed)

        right_col.addWidget(pi_card)
        right_col.addStretch(1)

        # ─── Assemble columns ─────────────────────────────────────────────────
        cols.addLayout(left_col, 1)

        v_sep = QFrame()
        v_sep.setFrameShape(QFrame.Shape.VLine)
        v_sep.setObjectName("ip_v_sep")
        cols.addWidget(v_sep)

        cols.addLayout(right_col, 1)
        root.addLayout(cols, 1)

    def _mk_entry(self) -> QLineEdit:
        e = QLineEdit()
        e.setAlignment(Qt.AlignmentFlag.AlignCenter)
        e.setMinimumWidth(110)
        v = QDoubleValidator()
        v.setNotation(QDoubleValidator.Notation.StandardNotation)
        e.setValidator(v)
        return e

    def _unit_lbl(self, text: str) -> QLabel:
        l = QLabel(text)
        l.setObjectName("ip_unit")
        return l

    def _add_form_row(self, grid: QGridLayout, row: int, label: str,
                      entry: QLineEdit, unit: str, tooltip: str = ""):
        lbl = QLabel(label)
        lbl.setObjectName("ip_field")
        if tooltip:
            lbl.setToolTip(tooltip)
        grid.addWidget(lbl, row, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        grid.addWidget(entry, row, 1)
        u = QLabel(unit)
        u.setObjectName("ip_unit")
        u.setMinimumWidth(60)
        grid.addWidget(u, row, 2, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

    def _wire_signals(self):
        def _set_ident_status(t):
            lvl = "error" if "failed" in t.lower() or "error" in t.lower() or "J = 0" in t else \
                  "ok"    if t in ("complete", "done") else "info"
            self.status_label.setText(_badge(t, lvl))
        self._sig_status.connect(_set_ident_status)
        def _set_speed_status(t):
            if not t:
                self.speed_status_label.setText("")
                return
            lvl = "error" if "error" in t.lower() else "ok" if "done" in t.lower() or "ready" in t.lower() else "info"
            self.speed_status_label.setText(_badge(t, lvl))
        self._sig_speed_status.connect(_set_speed_status)
        self._sig_jacl_loaded.connect(lambda pct: self.accel_ratio_entry.setText(f"{pct:.0f}"))
        self._sig_ident_done.connect(self._on_ident_done)
        self._sig_ident_warn.connect(lambda m: _show_warn(self, "Warning", m))
        self._sig_ident_error.connect(self._on_ident_error)
        self._sig_calc_done.connect(self._on_calc_done)
        self._sig_calc_error.connect(self._on_calc_error)
        self._sig_apply_done.connect(self._on_apply_done)
        self._sig_apply_error.connect(self._on_apply_error)

    def _apply_style(self):
        p = _get_palette()
        d = "#ip_dialog"
        self.setStyleSheet(f"""
            QDialog{d} {{ background: {p['bg']}; }}
            {d} QLabel {{ background: transparent; color: {p['text']}; }}
            {d} QLabel#ip_title      {{ font-size: 16px; font-weight: 700; color: {p['text']}; }}
            {d} QLabel#ip_sub        {{ font-size: 11px; color: {p['muted']}; }}
            {d} QLabel#ip_card_title {{ font-size: 13px; font-weight: 700; color: {p['text']}; }}
            {d} QLabel#ip_subcard_title {{ font-size: 11px; font-weight: 700; color: {p['text2']}; }}
            {d} QLabel#ip_field      {{ font-size: 11px; font-weight: 500; color: {p['text2']}; }}
            {d} QLabel#ip_unit       {{ font-size: 11px; color: {p['muted']}; }}
            {d} QLabel#ip_info       {{ font-size: 10px; color: {p['muted']}; }}
            {d} QLabel#ip_status     {{ font-size: 11px; font-weight: 600; color: {p['text2']}; }}
            {d} QLabel#ip_res_name   {{ font-size: 11px; color: {p['muted']}; font-weight: 500; }}
            {d} QLabel#ip_res_val    {{ font-size: 13px; font-weight: 700; color: {p['text']};
                                       font-family: "Consolas", "Courier New", monospace; }}
            {d} QLabel#ip_preview    {{ font-size: 12px; color: {p['blue']};
                                       font-family: "Consolas", "Courier New", monospace;
                                       background: {p['blue_light']}; border: 1px solid {p['border']};
                                       border-radius: 6px; padding: 10px 12px; line-height: 1.6; }}
            {d} QFrame#ip_card    {{ background: {p['white']}; border: 1px solid {p['border']}; border-radius: 8px; }}
            {d} QFrame#ip_subcard {{ background: {p['input_bg']}; border: 1px solid {p['border']}; border-radius: 6px; }}
            {d} QFrame#ip_v_sep   {{ color: {p['border']}; background: {p['border']}; }}
            {d} QFrame#ip_pi_row  {{ background: {p['input_bg']}; border-left: 3px solid {p['border']}; border-radius: 0px; }}
            {d} QFrame#ip_pi_row * {{ border: none; background: transparent; }}
            {d} QLabel#ip_pi_name {{ font-size: 12px; font-weight: 700; color: {p['text2']}; min-width: 28px; }}
            {d} QLabel#ip_pi_val  {{ font-size: 13px; font-weight: 700; color: {p['text']};
                                     font-family: "Consolas", "Courier New", monospace; }}
            {d} QLabel#ip_pi_unit {{ font-size: 10px; color: {p['muted']}; min-width: 70px; }}
            {d} QLineEdit {{
                background: {p['input_bg']}; color: {p['text']};
                border: 1px solid {p['border']}; border-radius: 5px;
                padding: 4px 8px; font-size: 12px; min-height: 22px;
                selection-background-color: {p['blue']};
            }}
            {d} QLineEdit:focus    {{ border-color: {p['blue']}; }}
            {d} QLineEdit:disabled {{ background: {p['bg']}; color: {p['muted']}; }}
            {d} QPushButton#ip_btn_primary {{
                background: {p['blue']}; color: white; border: none;
                border-radius: 6px; padding: 8px 14px; font-size: 12px; font-weight: 700;
            }}
            {d} QPushButton#ip_btn_primary:hover    {{ background: {p['blue_dark']}; }}
            {d} QPushButton#ip_btn_primary:disabled {{ background: {p['muted']}; color: {p['bg']}; }}
            {d} QPushButton#ip_btn_secondary {{
                background: {p['card']}; color: {p['blue']};
                border: 1.5px solid {p['blue']}; border-radius: 6px;
                padding: 8px 14px; font-size: 12px; font-weight: 600;
            }}
            {d} QPushButton#ip_btn_secondary:hover    {{ background: {p['blue_light']}; }}
            {d} QPushButton#ip_btn_secondary:disabled {{ color: {p['muted']}; border-color: {p['border']}; }}
            QToolTip {{
                background: #1E293B; color: #F1F5F9;
                border: 1px solid #334155; border-radius: 4px;
                padding: 4px 8px; font-size: 11px;
            }}
        """)

    # ── Initial settings load ─────────────────────────────────────────────────
    def _load_initial_settings(self):
        try:
            if not self.serial_manager.is_open:
                return
            jacl_raw = self.serial_manager.send("g jacl  ", expect_response=True)
            jacl_frac = float(jacl_raw.strip())
            jacl_pct = jacl_frac * 100.0
            self._sig_jacl_loaded.emit(jacl_pct)
            logging.info(f"Loaded J accel ratio: {jacl_pct:.0f}%")
        except Exception as e:
            logging.warning(f"Could not load J accel ratio (first run?): {e}")

    # ── Identification flow ───────────────────────────────────────────────────
    def on_start_identification(self):
        if not self.serial_manager.is_open:
            _show_error(self, "Error", "Not connected to serial port.")
            return
        if self.cmd_manager and not self.cmd_manager.is_ready_for_command():
            try:
                current_cmd = self.cmd_manager.get_current_command()
                elapsed     = self.cmd_manager.get_elapsed_time()
                _show_warn(self, "Busy",
                    f"Command '{current_cmd}' is still executing ({elapsed:.1f}s).\n"
                    "Please wait for it to complete.")
            except Exception:
                _show_warn(self, "Busy", "Another command is still executing.")
            return

        self._stop_ident = False
        self.start_btn.setText("Running...")
        self.start_btn.setEnabled(False)
        self.stop_ident_btn.setEnabled(True)
        self.inertia_label.setTextFormat(Qt.TextFormat.RichText)
        self.friction_label.setTextFormat(Qt.TextFormat.RichText)
        self.inertia_label.setText(_badge("measuring...", "info"))
        self.friction_label.setText(_badge("measuring...", "info"))
        threading.Thread(target=self._ident_worker, daemon=True).start()

    def _on_stop_ident_clicked(self):
        self._stop_ident = True
        self.stop_ident_btn.setEnabled(False)

    def _ident_worker(self):
        try:
            try:
                ratio_pct = float(self.accel_ratio_entry.text())
                if not (10 <= ratio_pct <= 70):
                    raise ValueError("Ratio must be between 10 and 70%")
            except ValueError as e:
                self._sig_ident_error.emit(f"Invalid ratio: {e}")
                return

            self._sig_status.emit("setting acceleration ratio...")
            ratio_frac = ratio_pct / 100.0
            ratio_encoded = dec_encode(ratio_frac)
            self.serial_manager.send(f"s jacl  {ratio_encoded}", expect_response=True)
            logging.info(f"Sent: s jacl {ratio_encoded}  ({ratio_pct}%)")

            self._sig_status.emit("starting identification...")
            self.serial_manager.send("s jidn  ", expect_response=False)
            logging.info("Sent: s jidn — waiting for queue to clear...")

            elapsed = 0.0
            stopped = False
            while elapsed < self._TIMEOUT_S:
                time.sleep(0.5)
                elapsed += 0.5
                if self._stop_ident:
                    stopped = True
                    break
                self._sig_status.emit(f"running... {int(elapsed)} / {int(self._TIMEOUT_S)} s")
                try:
                    queue_size_str = self.serial_manager.send("g qusize", expect_response=True)
                    queue_size = int(round(float(queue_size_str)))
                    if queue_size == 0:
                        logging.info(f"Queue cleared after {elapsed:.1f}s — identification complete")
                        break
                except Exception as e:
                    logging.warning(f"Error reading queue size: {e}")

            if stopped:
                self._sig_status.emit("stopped")
                self._sig_ident_error.emit("Identification stopped by user.")
                return

            j_raw    = self.serial_manager.send("g jres  ", expect_response=True)
            j_val    = dec_decode(j_raw)
            fric_raw = self.serial_manager.send("g fric  ", expect_response=True)
            fric_val = dec_decode(fric_raw)

            if j_val <= 0.0:
                self._sig_status.emit("J = 0 — check motor connection")
                self._sig_ident_warn.emit("Identified J = 0.\nIdentification may have timed out or failed.\n"
                                          "Check motor connection and parameters.")
            else:
                self._sig_status.emit("complete")

            self._sig_ident_done.emit(j_val, fric_val)
        except Exception as e:
            self._sig_status.emit("failed")
            self._sig_ident_error.emit(f"Identification failed: {e}")

    def _on_ident_done(self, j_val: float, fric_val: float):
        if j_val > 0:
            self.inertia_label.setText(si_format_inertia(j_val))
            self.friction_label.setText(si_format_friction(fric_val) if fric_val > 0 else "—")
        else:
            self.inertia_label.setTextFormat(Qt.TextFormat.RichText)
            self.inertia_label.setText(_badge("failed", "error"))
            self.friction_label.setText("—")
        logging.info(f"J={j_val:.6f} kg*m^2  B={fric_val:.6f} Nm*s/rad")
        if j_val > 0:
            self.j_entry.setText(f"{j_val:.8f}")
            logging.info(f"Auto-populated inertia field with identified value: {j_val:.8f}")
        self.start_btn.setText("Start Identification")
        self.start_btn.setEnabled(True)
        self.stop_ident_btn.setEnabled(False)

    def _on_ident_error(self, msg: str):
        _show_error(self, "Error", msg)
        self.start_btn.setText("Start Identification")
        self.start_btn.setEnabled(True)
        self.stop_ident_btn.setEnabled(False)

    # ── Speed PI calculate ────────────────────────────────────────────────────
    def on_calculate_speed_pi(self):
        if not self.serial_manager.is_open:
            _show_error(self, "Error", "Not connected to serial port.")
            return

        j_str   = self.j_entry.text().strip()
        dg_str  = self.dg_entry.text().strip()
        t50_str = self.t50_entry.text().strip()

        if not dg_str or not t50_str:
            _show_error(self, "Error", "Enter Dg and t50 before calculating.")
            return
        if not j_str:
            _show_error(self, "Error",
                "Enter inertia J or run inertia identification first.\n"
                "Inertia is required for speed controller calculation.")
            return

        try:
            Dg       = float(dg_str)
            t50      = float(t50_str)
            J_manual = float(j_str)
        except ValueError:
            _show_error(self, "Error", "Dg, t50, and J must be valid numbers.")
            return

        if Dg <= 0.0:
            _show_error(self, "Error", "Dg must be positive (> 0)")
            return
        if J_manual <= 0.0:
            _show_error(self, "Error", "Inertia J must be positive (> 0)")
            return

        self.calc_btn.setText("Reading...")
        self.calc_btn.setEnabled(False)
        self.apply_btn.setEnabled(False)
        self.preview_label.setText("")
        self._pi_dy_val.setText("—")
        self._pi_km_val.setText("—")
        self._pi_kp_val.setText("—")
        self._pi_ki_val.setText("—")
        self._sig_speed_status.emit("reading motor parameters...")

        threading.Thread(
            target=self._calc_worker,
            args=(Dg, t50, J_manual),
            daemon=True).start()

    def _calc_worker(self, Dg: float, t50: float, J_f: float):
        try:
            logging.info(f"Using inertia: J={J_f:.6f} kg·m²")

            PsiF_f = dec_decode(self.serial_manager.send("g mpsif ", expect_response=True))
            P_f    = dec_decode(self.serial_manager.send("g mpole ", expect_response=True))
            Tg_f   = dec_decode(self.serial_manager.send("g itg   ", expect_response=True))
            Fpwm_f = dec_decode(self.serial_manager.send("g fpwm  ", expect_response=True))

            logging.info(f"Motor params: J={J_f:.6f} PsiF={PsiF_f:.6f} P={P_f} "
                         f"Tg={Tg_f:.6f} Fpwm={Fpwm_f}")

            if Tg_f <= 0.0 or Fpwm_f <= 0.0 or PsiF_f <= 0.0 or P_f <= 0.0:
                self._sig_calc_error.emit(
                    "Invalid motor parameters from firmware.\n"
                    "Verify Tg, Fpwm, PsiF, and pole pairs are configured.")
                return
            Tpwm_f = 1.0 / Fpwm_f
            Dy_max = 1.0 / (2.0 * Dg * Tg_f)
            t50_min_ms = 0.6931 / Dy_max * 1000.0

            if t50 <= t50_min_ms:
                msg = (f"t50 = {t50:.1f} ms is too fast.\n"
                       f"Minimum for Dg = {Dg:.3f} is {t50_min_ms:.1f} ms.\n"
                       f"Increase t50 or reduce Dg.")
                self._sig_calc_error.emit(msg)
                return

            Dy = 0.6931 / (t50 / 1000.0)
            Km_f   = 45.0 * P_f * PsiF_f / (math.pi * J_f)
            p3     = 1.0 / Tg_f - 2.0 * Dg * Dy
            Dy2    = Dy * Dy
            Kp     = (Tg_f * Dy2 + 2.0 * Dg * Dy - 4.0 * Dg * Dg * Dy2 * Tg_f) / Km_f
            Tn     = Km_f * Kp / (Tg_f * Dy2 * p3)
            Ki     = Kp * Tpwm_f / Tn

            logging.info(f"Speed PI: Km={Km_f:.4f} Dy={Dy:.2f} Kp={Kp:.6f} Ki={Ki:.8f}")

            # Write J into firmware motor param struct (mtheta), then damp/dyn, then susp
            self.serial_manager.send(f"s mtheta{dec_encode(J_f)}", expect_response=True)
            self.serial_manager.send(f"s damp  {dec_encode(Dg)}",  expect_response=True)
            self.serial_manager.send(f"s dyn   {dec_encode(Dy)}",  expect_response=True)
            self.serial_manager.send("s susp  ", expect_response=False)
            time.sleep(0.5)
            logging.info(f"Sent: damp={Dg:.4f} dyn={Dy:.4f} susp")

            self._sig_calc_done.emit(Kp, Ki, Dy, Km_f)
        except Exception as e:
            logging.error(f"Speed PI calculation failed: {e}")
            self._sig_calc_error.emit(f"Speed PI calculation failed:\n{e}")

    def _on_calc_done(self, Kp: float, Ki: float, Dy: float, Km_f: float):
        kp_si = si_format(Kp, "", precision=3)
        ki_si = si_format(Ki, "", precision=3)
        self._pi_dy_val.setText(f"{Dy:.2f}")
        self._pi_km_val.setText(f"{Km_f:.2f}")
        self._pi_kp_val.setText(kp_si)
        self._pi_ki_val.setText(ki_si)
        self.calc_btn.setText("Calculate & Preview")
        self.calc_btn.setEnabled(True)
        self.apply_btn.setEnabled(True)
        self._sig_speed_status.emit("ready — click Apply to activate")

    def _on_calc_error(self, msg: str):
        _show_error(self, "Error", msg)
        self._sig_speed_status.emit("error")
        self.calc_btn.setText("Calculate & Preview")
        self.calc_btn.setEnabled(True)

    # ── Speed PI apply ────────────────────────────────────────────────────────
    def on_apply_speed_pi(self):
        if not self.serial_manager.is_open:
            _show_error(self, "Error", "Not connected to serial port.")
            return
        self.apply_btn.setText("Applying...")
        self.apply_btn.setEnabled(False)
        self._sig_speed_status.emit("applying...")
        threading.Thread(target=self._apply_worker, daemon=True).start()

    def _apply_worker(self):
        try:
            self.serial_manager.send("s apspd ", expect_response=False)
            time.sleep(0.3)
            logging.info("Sent: apspd — speed PI applied")
            self._sig_apply_done.emit()
        except Exception as e:
            logging.error(f"Apply speed PI failed: {e}")
            self._sig_apply_error.emit(str(e))

    def _on_apply_done(self):
        self.apply_btn.setText("Apply to Controller")
        self.apply_btn.setEnabled(True)
        self._sig_speed_status.emit("done — speed PI active")

    def _on_apply_error(self, msg: str):
        _show_error(self, "Error", f"Failed to apply speed PI:\n{msg}")
        self._sig_speed_status.emit("error")
        self.apply_btn.setText("Apply to Controller")
        self.apply_btn.setEnabled(True)


# ── Standalone testing ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    class DummySerial:
        def __init__(self):
            self.is_open = True
        def send(self, command, expect_response=False):
            if not expect_response:
                return None
            if "jacl"   in command: return "+0.2500000"
            if "contr"  in command: return "+0.0000000"
            if "jres"   in command: return "+0.0001749"
            if "fric"   in command: return "+0.0000032"
            if "mpsif"  in command: return "+0.0053800"
            if "mpole"  in command: return "+7.0000000"
            if "itg"    in command: return "+0.0020000"
            if "fpwm"   in command: return "+16000.000"
            if "qusize" in command: return "+0.0000000"
            return "+0.0000000"

    app = QApplication(sys.argv)
    app.setFont(QFont("Segoe UI", 10))
    dlg = InertiaIdentification(None, DummySerial())
    dlg.show()
    sys.exit(app.exec())
