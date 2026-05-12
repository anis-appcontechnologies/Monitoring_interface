#!/usr/bin/env python3
"""
Electrical Parameters Identification — PySide6 edition

Behavior preserved 1:1 from electrical_params.py (Tkinter):
  • Form: Pole Pair, Nominal Voltage (auto-suggested from Udc), Nominal Current, No Load Speed
  • Current Control Setup: Tg d-axis (ms), Tg q-axis (ms) — required for Tune step
  • PsiF Identification setup: Acceleration Torque %
  • Three actions: Start Identification, Tune Current Controller, Ident PsiF
  • Identified Parameters card: Rs, Lsd, Lsq, Psif (with SI formatting)
  • Current PI Gains card: Kp/Ki Isd/Isq
  • All command strings, timings, and math identical to the original.

Key flows (DO NOT change):
  • Start Identification:
      s mpole {pp}; s miqmx {is}; s msmnl {nl}; s msmax {nl};
      then s elid (no response); poll g qusize until 0;
      g rsi/lsdi/lsqi; compute psif_theory; s mpsif {psif_theory}
  • Tune Current Controller:
      validate Tg_d, Tg_q (0.6–3.0 ms);
      g rsi/lsdi/lsqi; s mrs/mlsd/mlsq;
      s itd {Tg_d_s}; s itq {Tg_q_s};
      s suidq; sleep 0.1; read kpisd/kiisd/kpisq/kiisq.
  • Ident PsiF:
      validate accel_torque 10..70%; compute isd_ratio;
      s pisq {isq}; s pisd {isd}; sleep 0.05; s psiid;
      poll g contr until mode==0 (timeout 60s); g mpsif.
"""

import logging
import threading
import time
import math

from PySide6.QtCore import Qt, Signal, QSize, QTimer
from PySide6.QtGui import QFont, QDoubleValidator, QIcon
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QLineEdit,
    QPushButton, QFrame, QSizePolicy, QApplication, QScrollArea, QWidget,
)

from si_format import (
    si_format, si_format_resistance, si_format_inductance, si_format_flux,
)

try:
    import qtawesome as qta
    _QTA = True
except ImportError:
    _QTA = False


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


def dec_decode(s: str) -> float:
    s = s.strip()
    if not s:
        raise ValueError("Empty decimal string")
    return float(s)


logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(message)s")


def _badge(text: str, level: str = "info") -> str:
    """Return HTML for a styled inline badge. level: 'info'|'warn'|'ok'|'error'|'muted'"""
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


class ElectricalParametersIdentification(QDialog):
    """Modern Qt port. Same constructor signature as the Tkinter class."""

    # Thread-safe UI updates
    _sig_voltage_hint     = Signal(float)
    _sig_torque_loaded    = Signal(float)
    _sig_results_ready    = Signal(float, float, float, float)   # rs, lsd, lsq, psif_theory_wb
    _sig_pi_gains         = Signal(float, float, float, float)   # kp_isd, ki_isd, kp_isq, ki_isq
    _sig_psif_running     = Signal(int)
    _sig_psif_done        = Signal(float)                         # psif_wb
    _sig_psif_error       = Signal(str)
    _sig_start_done       = Signal()
    _sig_start_error      = Signal(str)
    _sig_tune_done        = Signal()
    _sig_tune_error       = Signal(str)
    _sig_warn             = Signal(str)
    _sig_info             = Signal(str)

    _PSIF_TIMEOUT_S = 60.0
    _PSIF_POLL_S    = 1.0
    _ELID_TIMEOUT_S = 60.0

    def __init__(self, parent, serial_manager, cmd_manager=None, defaults=None):
        super().__init__(parent)
        self.serial_manager = serial_manager
        self.cmd_manager    = cmd_manager
        self._defaults      = defaults or {}

        self.setWindowTitle("Electrical Parameters Identification")
        self.setObjectName("ep_dialog")
        self.resize(860, 560)
        self.setMinimumSize(820, 520)

        self._build_ui()
        self._apply_style()
        self._wire_signals()

        # Pre-populate from defaults
        if "pole" in self._defaults:
            self.pole_pair_entry.setText(str(self._defaults["pole"]))
        if "current" in self._defaults:
            self.current_entry.setText(str(self._defaults["current"]))
        if "speed" in self._defaults:
            self.speed_entry.setText(str(self._defaults["speed"]))

        # Load Udc-based voltage suggestion + current torque from firmware
        threading.Thread(target=self._load_initial_settings, daemon=True).start()

    # ── UI ─────────────────────────────────────────────────────────────────────
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(8)

        title = QLabel("Electrical Parameters Identification")
        title.setObjectName("ep_title")
        sub = QLabel("Identify Rs, Lsd, Lsq and tune current controller")
        sub.setObjectName("ep_sub")
        root.addWidget(title)
        root.addWidget(sub)

        # ── Two-column split: LEFT = inputs/actions  RIGHT = results ──────────
        cols = QHBoxLayout()
        cols.setSpacing(10)

        # ─── LEFT COLUMN ──────────────────────────────────────────────────────
        left_col = QVBoxLayout()
        left_col.setSpacing(8)

        # Card: Motor Parameters (inputs)
        params_card = QFrame()
        params_card.setObjectName("ep_card")
        p_lay = QVBoxLayout(params_card)
        p_lay.setContentsMargins(14, 12, 14, 12)
        p_lay.setSpacing(8)

        p_hdr = QLabel("Motor Parameters")
        p_hdr.setObjectName("ep_card_title")
        p_lay.addWidget(p_hdr)

        form = QGridLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)
        form.setColumnStretch(1, 1)
        form.setColumnMinimumWidth(0, 130)

        self.pole_pair_entry = self._mk_entry(int_only=False)
        self._add_form_row(form, 0, "Pole Pair Number", self.pole_pair_entry, "")

        self.voltage_entry = self._mk_entry()
        self._add_form_row(form, 1, "Nominal Voltage", self.voltage_entry, "VRMS")

        self.voltage_hint_label = QLabel("Suggested from Udc: N/A")
        self.voltage_hint_label.setObjectName("ep_hint")
        form.addWidget(self.voltage_hint_label, 2, 0, 1, 3)

        self.current_entry = self._mk_entry()
        self._add_form_row(form, 3, "Nominal Current", self.current_entry, "ARMS")

        self.speed_entry = self._mk_entry()
        self._add_form_row(form, 4, "No Load Speed", self.speed_entry, "RPM")

        p_lay.addLayout(form)

        # Step flow: [Step 1: Start ID] → [Step 2: Tune]  (Step 3 is in psif_card below)
        step_flow = QVBoxLayout()
        step_flow.setSpacing(4)

        step_labels_row = QHBoxLayout()
        step_labels_row.setSpacing(0)
        for step_txt, stretch in [("Step 1", 1), ("", 0), ("Step 2", 1)]:
            if step_txt:
                sl = QLabel(step_txt)
                sl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                sl.setObjectName("ep_step_lbl")
                step_labels_row.addWidget(sl, stretch)
            else:
                step_labels_row.addSpacing(24)
        step_flow.addLayout(step_labels_row)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(0)

        self.start_btn = QPushButton("Start Identification")
        self.start_btn.setObjectName("ep_btn_primary")
        self.start_btn.setToolTip("Step 1 — Identify Rs, Lsd, Lsq by injecting test signals")
        self.start_btn.setMinimumHeight(34)
        self.start_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.start_btn.clicked.connect(self.on_start_identification)
        if _QTA:
            self.start_btn.setIcon(qta.icon("fa5s.play", color="#FFFFFF"))
            self.start_btn.setIconSize(QSize(13, 13))
        btn_row.addWidget(self.start_btn, 1)

        arrow_lbl = QLabel("›")
        arrow_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        arrow_lbl.setFixedWidth(24)
        arrow_lbl.setObjectName("ep_arrow_lbl")
        btn_row.addWidget(arrow_lbl)

        self.tune_btn = QPushButton("Tune Current Controller")
        self.tune_btn.setObjectName("ep_btn_secondary")
        self.tune_btn.setToolTip("Step 2 — Apply identified parameters to tune Id/Iq current PI controllers")
        self.tune_btn.setEnabled(False)
        self.tune_btn.setMinimumHeight(34)
        self.tune_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.tune_btn.clicked.connect(self.on_tune_current_controller)
        if _QTA:
            self.tune_btn.setIcon(qta.icon("fa5s.sliders-h", color="#1976D2"))
            self.tune_btn.setIconSize(QSize(13, 13))
        btn_row.addWidget(self.tune_btn, 1)

        step_flow.addLayout(btn_row)
        p_lay.addLayout(step_flow)
        left_col.addWidget(params_card)

        # Card: Current Control Setup (Tg d/q — required for Tune step)
        tg_card = QFrame()
        tg_card.setObjectName("ep_card")
        tg_lay = QVBoxLayout(tg_card)
        tg_lay.setContentsMargins(14, 12, 14, 12)
        tg_lay.setSpacing(8)

        tg_hdr = QLabel("Current Control Setup")
        tg_hdr.setObjectName("ep_card_title")
        tg_lay.addWidget(tg_hdr)

        tg_form = QGridLayout()
        tg_form.setHorizontalSpacing(10)
        tg_form.setVerticalSpacing(8)
        tg_form.setColumnStretch(1, 1)

        self.tg_d_entry = self._mk_entry()
        self.tg_d_entry.setText("1.0")
        self.tg_d_entry.setToolTip("Time constant for d-axis current controller (0.6–3.0 ms)")
        self._add_form_row(tg_form, 0, "Tg d-axis", self.tg_d_entry, "ms")

        self.tg_q_entry = self._mk_entry()
        self.tg_q_entry.setText("1.0")
        self.tg_q_entry.setToolTip("Time constant for q-axis current controller (0.6–3.0 ms)")
        self._add_form_row(tg_form, 1, "Tg q-axis", self.tg_q_entry, "ms")

        tg_lay.addLayout(tg_form)
        left_col.addWidget(tg_card)

        # Card: PsiF Identification Setup (inputs)
        psif_card = QFrame()
        psif_card.setObjectName("ep_card")
        psif_lay = QVBoxLayout(psif_card)
        psif_lay.setContentsMargins(14, 12, 14, 12)
        psif_lay.setSpacing(8)

        psif_hdr = QLabel("PsiF Identification Setup")
        psif_hdr.setObjectName("ep_card_title")
        psif_lay.addWidget(psif_hdr)

        psif_row = QHBoxLayout()
        psif_row.setSpacing(8)
        psif_lbl = QLabel("Acceleration Torque")
        psif_lbl.setObjectName("ep_field")
        psif_lbl.setToolTip("Acceleration torque as % of max (10–70%)\nIsd will be calculated automatically")
        psif_row.addWidget(psif_lbl)

        self.psif_accel_torque_entry = self._mk_entry()
        self.psif_accel_torque_entry.setText("25")
        self.psif_accel_torque_entry.setFixedWidth(80)
        psif_row.addWidget(self.psif_accel_torque_entry)
        psif_row.addWidget(self._unit_lbl("%"))
        psif_row.addStretch()
        psif_lay.addLayout(psif_row)

        step3_lbl = QLabel("Step 3")
        step3_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft)
        step3_lbl.setObjectName("ep_step_lbl")
        psif_lay.addWidget(step3_lbl)

        self.psif_ident_btn = QPushButton("Ident PsiF")
        self.psif_ident_btn.setObjectName("ep_btn_primary")
        self.psif_ident_btn.setToolTip("Step 3 — Identify flux linkage (PsiF) at operating speed.\n"
                                       "Motor will start automatically.")
        self.psif_ident_btn.setMinimumHeight(34)
        self.psif_ident_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.psif_ident_btn.clicked.connect(self.on_ident_psif)
        if _QTA:
            self.psif_ident_btn.setIcon(qta.icon("fa5s.bolt", color="#FFFFFF"))
            self.psif_ident_btn.setIconSize(QSize(13, 13))
        psif_lay.addWidget(self.psif_ident_btn)

        psif_result_row = QHBoxLayout()
        psif_result_row.setSpacing(6)
        psif_name_lbl = QLabel("PsiF identified (Wb)")
        psif_name_lbl.setObjectName("ep_res_name")
        psif_name_lbl.setFixedWidth(150)
        psif_name_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.psif_live_label = QLabel("—")
        self.psif_live_label.setTextFormat(Qt.TextFormat.RichText)
        self.psif_live_label.setObjectName("ep_res_val")
        self.psif_live_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        psif_result_row.addWidget(psif_name_lbl)
        psif_result_row.addWidget(self.psif_live_label, 1)
        psif_lay.addLayout(psif_result_row)

        left_col.addWidget(psif_card)

        # ─── RIGHT COLUMN ─────────────────────────────────────────────────────
        right_col = QVBoxLayout()
        right_col.setSpacing(8)

        def _result_row(parent_layout, name, obj_name_val):
            row = QHBoxLayout()
            row.setSpacing(6)
            name_lbl = QLabel(name)
            name_lbl.setObjectName("ep_res_name")
            name_lbl.setFixedWidth(100)
            name_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            val_lbl = QLabel("—")
            val_lbl.setObjectName(obj_name_val)
            val_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            row.addWidget(name_lbl)
            row.addWidget(val_lbl, 1)
            parent_layout.addLayout(row)
            return val_lbl

        # Card: Identified Parameters (results)
        results_card = QFrame()
        results_card.setObjectName("ep_card")
        r_lay = QVBoxLayout(results_card)
        r_lay.setContentsMargins(14, 12, 14, 12)
        r_lay.setSpacing(6)

        r_hdr = QLabel("Identified Parameters")
        r_hdr.setObjectName("ep_card_title")
        r_lay.addWidget(r_hdr)

        self.rs_label   = _result_row(r_lay, "Rs (Ω)",   "ep_res_val")
        self.lsd_label  = _result_row(r_lay, "Lsd (H)",  "ep_res_val")
        self.lsq_label  = _result_row(r_lay, "Lsq (H)",  "ep_res_val")
        self.psif_label = _result_row(r_lay, "Psif (Wb)", "ep_res_val")

        right_col.addWidget(results_card)

        # Card: Current PI Gains (results)
        pi_card = QFrame()
        pi_card.setObjectName("ep_card")
        pi_lay = QVBoxLayout(pi_card)
        pi_lay.setContentsMargins(14, 12, 14, 12)
        pi_lay.setSpacing(6)

        pi_hdr = QLabel("Current PI Gains")
        pi_hdr.setObjectName("ep_card_title")
        pi_lay.addWidget(pi_hdr)

        self.kp_isd_label = _result_row(pi_lay, "Kp Isd (A/V)", "ep_res_val_green")
        self.ki_isd_label = _result_row(pi_lay, "Ki Isd (A/Vs)", "ep_res_val_green")
        self.kp_isq_label = _result_row(pi_lay, "Kp Isq (A/V)", "ep_res_val_green")
        self.ki_isq_label = _result_row(pi_lay, "Ki Isq (A/Vs)", "ep_res_val_green")

        right_col.addWidget(pi_card)
        right_col.addStretch(1)

        # ─── Assemble columns ─────────────────────────────────────────────────
        # Wrap left column in a scroll area so all cards are visible on small screens
        left_col.addStretch(1)
        left_widget = QWidget()
        left_widget.setObjectName("ep_scroll_inner")
        left_widget.setLayout(left_col)

        left_scroll = QScrollArea()
        left_scroll.setObjectName("ep_scroll")
        left_scroll.setWidget(left_widget)
        left_scroll.setWidgetResizable(True)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        left_scroll.setFrameShape(QFrame.Shape.NoFrame)
        left_scroll.setMinimumWidth(340)
        cols.addWidget(left_scroll, 1)

        v_sep = QFrame()
        v_sep.setFrameShape(QFrame.Shape.VLine)
        v_sep.setObjectName("ep_v_sep")
        cols.addWidget(v_sep)

        cols.addLayout(right_col, 1)
        root.addLayout(cols, 1)

    def _mk_entry(self, int_only=False) -> QLineEdit:
        e = QLineEdit()
        e.setAlignment(Qt.AlignmentFlag.AlignCenter)
        e.setMinimumWidth(110)
        v = QDoubleValidator()
        v.setNotation(QDoubleValidator.Notation.StandardNotation)
        e.setValidator(v)
        return e

    def _unit_lbl(self, text: str) -> QLabel:
        l = QLabel(text)
        l.setObjectName("ep_unit")
        return l

    def _add_form_row(self, grid: QGridLayout, row: int, label: str,
                      entry: QLineEdit, unit: str):
        lbl = QLabel(label)
        lbl.setObjectName("ep_field")
        grid.addWidget(lbl, row, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        grid.addWidget(entry, row, 1)
        u = QLabel(unit)
        u.setObjectName("ep_unit")
        u.setMinimumWidth(50)
        grid.addWidget(u, row, 2, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

    def _wire_signals(self):
        self._sig_voltage_hint.connect(self._on_voltage_hint)
        self._sig_torque_loaded.connect(self._on_torque_loaded)
        self._sig_results_ready.connect(self._on_results_ready)
        self._sig_pi_gains.connect(self._on_pi_gains)
        self._sig_psif_running.connect(self._on_psif_running)
        self._sig_psif_done.connect(self._on_psif_done)
        self._sig_psif_error.connect(self._on_psif_error)
        self._sig_start_done.connect(self._on_start_done)
        self._sig_start_error.connect(self._on_start_error)
        self._sig_tune_done.connect(self._on_tune_done)
        self._sig_tune_error.connect(self._on_tune_error)
        self._sig_warn.connect(lambda m: _show_warn(self, "Warning", m))
        self._sig_info.connect(lambda m: _show_warn(self, "Info", m))

    def _apply_style(self):
        p = _get_palette()
        d = "#ep_dialog"
        self.setStyleSheet(f"""
            QDialog{d} {{ background: {p['bg']}; }}
            {d} QLabel {{ background: transparent; color: {p['text']}; }}
            {d} QLabel#ep_title      {{ font-size: 16px; font-weight: 700; color: {p['text']}; }}
            {d} QLabel#ep_sub        {{ font-size: 11px; color: {p['muted']}; }}
            {d} QLabel#ep_card_title {{ font-size: 13px; font-weight: 700; color: {p['text']}; }}
            {d} QLabel#ep_field      {{ font-size: 11px; font-weight: 500; color: {p['text2']}; }}
            {d} QLabel#ep_unit       {{ font-size: 11px; color: {p['muted']}; }}
            {d} QLabel#ep_hint {{
                font-size: 10px; font-weight: 600; color: {p['blue']};
                background: {p['blue_light']}; border-radius: 4px;
                padding: 2px 7px; font-style: normal;
            }}
            {d} QLabel#ep_status     {{ font-size: 11px; font-weight: 600; color: {p['text2']}; }}
            {d} QLabel#ep_res_name   {{ font-size: 11px; color: {p['muted']}; font-weight: 500; }}
            {d} QLabel#ep_res_val {{
                font-size: 13px; font-weight: 700; color: {p['text']};
                font-family: "Consolas", "Courier New", monospace; letter-spacing: 0.3px;
                background: {p['bg']}; border-radius: 4px; padding: 3px 8px;
                border-left: 3px solid {p['border']};
            }}
            {d} QLabel#ep_res_val_green {{
                font-size: 13px; font-weight: 700; color: {p['green']};
                font-family: "Consolas", "Courier New", monospace; letter-spacing: 0.3px;
                background: {p['bg']}; border-radius: 4px; padding: 3px 8px;
                border-left: 3px solid {p['green']};
            }}
            {d} QFrame#ep_card  {{ background: {p['white']}; border: 1px solid {p['border']}; border-radius: 8px; }}
            {d} QFrame#ep_v_sep {{ color: {p['border']}; background: {p['border']}; }}
            {d} QScrollArea#ep_scroll {{ background: {p['bg']}; border: none; }}
            {d} QWidget#ep_scroll_inner {{ background: {p['bg']}; }}
            {d} QLabel#ep_step_lbl  {{ font-size: 10px; font-weight: 700; color: {p['blue']}; background: transparent; }}
            {d} QLabel#ep_arrow_lbl {{ font-size: 18px; font-weight: 700; color: {p['muted']}; background: transparent; }}
            {d} QLineEdit {{
                background: {p['input_bg']}; color: {p['text']};
                border: 1px solid {p['border']}; border-radius: 5px;
                padding: 4px 8px; font-size: 12px; min-height: 22px;
                selection-background-color: {p['blue']};
            }}
            {d} QLineEdit:focus    {{ border-color: {p['blue']}; }}
            {d} QLineEdit:disabled {{ background: {p['bg']}; color: {p['muted']}; }}
            {d} QPushButton#ep_btn_primary {{
                background: {p['blue']}; color: white; border: none;
                border-radius: 6px; padding: 8px 14px; font-size: 12px; font-weight: 700;
            }}
            {d} QPushButton#ep_btn_primary:hover    {{ background: {p['blue_dark']}; }}
            {d} QPushButton#ep_btn_primary:disabled {{ background: {p['muted']}; color: {p['bg']}; }}
            {d} QPushButton#ep_btn_secondary {{
                background: {p['card']}; color: {p['blue']};
                border: 1.5px solid {p['blue']}; border-radius: 6px;
                padding: 8px 14px; font-size: 12px; font-weight: 600;
            }}
            {d} QPushButton#ep_btn_secondary:hover    {{ background: {p['blue_light']}; }}
            {d} QPushButton#ep_btn_secondary:disabled {{ color: {p['muted']}; border-color: {p['border']}; }}
            QToolTip {{
                background: #1E293B; color: #F1F5F9;
                border: 1px solid #334155; border-radius: 4px;
                padding: 4px 8px; font-size: 11px;
            }}
        """)

    # ── Initial settings load ─────────────────────────────────────────────────
    def _load_initial_settings(self):
        """Background load of Udc-based voltage suggestion + current accel-torque %."""
        try:
            if not self.serial_manager.is_open:
                return
            try:
                udc_raw = self.serial_manager.send("g gudc  ", expect_response=True)
                udc = dec_decode(udc_raw)
                pp_str = self.pole_pair_entry.text().strip()
                # pp not actually used here — preserved for parity with original
                _pp = float(pp_str) if pp_str else 1.0
                vrms = udc / 1.732
                self._sig_voltage_hint.emit(vrms)
                logging.info(f"Calculated nominal voltage from Udc: {vrms:.1f} VRMS")
            except Exception as e:
                logging.warning(f"Could not load Udc: {e}")

            try:
                psif_isq_raw = self.serial_manager.send("g pisq  ", expect_response=True)
                psif_isq_frac = dec_decode(psif_isq_raw)
                psif_isq_pct = psif_isq_frac * 100.0
                self._sig_torque_loaded.emit(psif_isq_pct)
                logging.info(f"Loaded PsiF acceleration torque: {psif_isq_pct:.0f}%")
            except Exception as e:
                logging.warning(f"Could not load PsiF acceleration torque (first run?): {e}")
        except Exception as e:
            logging.error(f"Error loading initial settings: {e}")

    def _on_voltage_hint(self, vrms: float):
        self.voltage_entry.setText(f"{vrms:.1f}")
        self.voltage_hint_label.setText(f"Calculated from Udc: {vrms:.1f} VRMS")

    def _on_torque_loaded(self, pct: float):
        self.psif_accel_torque_entry.setText(f"{pct:.0f}")

    # ── PI gains ──────────────────────────────────────────────────────────────
    def _read_and_emit_pi_gains(self):
        """Read Kp/Ki of Isd/Isq from firmware and emit signal."""
        try:
            kp_isd = dec_decode(self.serial_manager.send("g kpisd", expect_response=True))
            ki_isd = dec_decode(self.serial_manager.send("g kiisd", expect_response=True))
            kp_isq = dec_decode(self.serial_manager.send("g kpisq", expect_response=True))
            ki_isq = dec_decode(self.serial_manager.send("g kiisq", expect_response=True))
            self._sig_pi_gains.emit(kp_isd, ki_isd, kp_isq, ki_isq)
        except Exception as e:
            logging.warning(f"Could not read PI gains: {e}")

    def _on_pi_gains(self, kp_isd, ki_isd, kp_isq, ki_isq):
        self.kp_isd_label.setText(si_format(kp_isd, '', precision=3))
        self.ki_isd_label.setText(si_format(ki_isd, '', precision=3))
        self.kp_isq_label.setText(si_format(kp_isq, '', precision=3))
        self.ki_isq_label.setText(si_format(ki_isq, '', precision=3))

    # ── Start identification ──────────────────────────────────────────────────
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

        self.start_btn.setText("Waiting...")
        self.start_btn.setEnabled(False)
        self.tune_btn.setEnabled(False)

        threading.Thread(target=self._start_worker, daemon=True).start()

    def _start_worker(self):
        try:
            pp_str = self.pole_pair_entry.text().strip()
            uv_str = self.voltage_entry.text().strip()
            is_str = self.current_entry.text().strip()
            nl_str = self.speed_entry.text().strip()

            if not all([pp_str, uv_str, is_str, nl_str]):
                self._sig_start_error.emit("All fields are required.")
                return

            pp = float(pp_str)
            uv = float(uv_str)
            is_ = float(is_str)
            nl = float(nl_str)

            if nl <= 0:
                self._sig_start_error.emit("No Load Speed must be greater than 0.")
                return

            self.serial_manager.send(f"s mpole {dec_encode(pp)}",  expect_response=True)
            self.serial_manager.send(f"s miqmx {dec_encode(is_)}", expect_response=True)
            self.serial_manager.send(f"s msmnl {dec_encode(nl)}",  expect_response=True)
            self.serial_manager.send(f"s msmax {dec_encode(nl)}",  expect_response=True)
            logging.info(f"SET commands sent: mpole={pp}, miqmx={is_}, "
                         f"msmnl={nl}, msmax={2.0 * nl} — QUEUE UNLOCKED")

            self._read_and_emit_pi_gains()

            self.serial_manager.send("s elid", expect_response=False)
            logging.info("Sent: s elid — waiting for queue to clear...")

            elapsed = 0.0
            while elapsed < self._ELID_TIMEOUT_S:
                time.sleep(0.5)
                elapsed += 0.5
                try:
                    queue_size_str = self.serial_manager.send("g qusize", expect_response=True)
                    queue_size = int(round(float(queue_size_str)))
                    if queue_size == 0:
                        logging.info(f"Queue cleared after {elapsed:.1f}s — identification complete")
                        break
                except Exception as e:
                    logging.warning(f"Error reading queue size: {e}")

            raw_rs  = self.serial_manager.send("g rsi  ", expect_response=True)
            raw_lsd = self.serial_manager.send("g lsdi ", expect_response=True)
            raw_lsq = self.serial_manager.send("g lsqi ", expect_response=True)
            rs_val  = dec_decode(raw_rs)
            lsd_val = dec_decode(raw_lsd)
            lsq_val = dec_decode(raw_lsq)

            omega_e        = 2.0 * math.pi * pp * nl / 60.0
            psif_theory_wb = (uv * math.sqrt(2.0 / 3.0)) / omega_e

            try:
                response_psif = self.serial_manager.send(
                    f"s mpsif {dec_encode(psif_theory_wb)}", expect_response=True)
                logging.info(f"Sent theoretical Psif: {psif_theory_wb:.6f} Wb → Response: {response_psif}")
            except Exception as e:
                logging.warning(f"Failed to send mpsif value to device: {e}")
                self._sig_warn.emit(f"Parameters identified but failed to send Psif to device:\n{e}")

            self._sig_results_ready.emit(rs_val, lsd_val, lsq_val, psif_theory_wb)
            self._sig_start_done.emit()

        except ValueError as e:
            self._sig_start_error.emit(f"Invalid response from device: {e}")
        except Exception as e:
            self._sig_start_error.emit(f"Identification failed: {e}")

    def _on_results_ready(self, rs, lsd, lsq, psif_theory_wb):
        self.rs_label.setText(si_format_resistance(rs))
        self.lsd_label.setText(si_format_inductance(lsd))
        self.lsq_label.setText(si_format_inductance(lsq))
        self.psif_label.setTextFormat(Qt.TextFormat.RichText)
        self.psif_label.setText(
            f"{si_format_flux(psif_theory_wb)}"
            f"&nbsp;&nbsp;<span style='font-size:11px; font-weight:700; color:#1565C0; "
            f"background:#E3F2FD; border-radius:3px; padding:2px 6px;'>theory</span>"
        )
        logging.info(f"Identification completed: Rs={rs:.6f} Ohm, "
                     f"Lsd={lsd:.6f} H, Lsq={lsq:.6f} H, "
                     f"PsifTheory={psif_theory_wb:.6f} Wb")

    def _on_start_done(self):
        self.start_btn.setText("Start Identification")
        self.start_btn.setEnabled(True)
        self.tune_btn.setEnabled(True)

    def _on_start_error(self, msg: str):
        _show_error(self, "Error", msg)
        self.start_btn.setText("Start Identification")
        self.start_btn.setEnabled(True)

    # ── Tune Current Controller ──────────────────────────────────────────────
    def on_tune_current_controller(self):
        if not self.serial_manager.is_open:
            _show_error(self, "Error", "Not connected to serial port.")
            return
        self.tune_btn.setText("Tuning...")
        self.tune_btn.setEnabled(False)
        threading.Thread(target=self._tune_worker, daemon=True).start()

    def _tune_worker(self):
        try:
            # Validate Tg values (must be read from UI on the main thread before worker starts,
            # but Qt allows reading QLineEdit.text() from a non-main thread safely here since
            # no write is occurring — consistent with expert version pattern)
            try:
                tg_d_ms = float(self.tg_d_entry.text().strip())
                if not (0.6 <= tg_d_ms <= 3.0):
                    raise ValueError("Tg d-axis must be between 0.6 and 3.0 ms")
                tg_q_ms = float(self.tg_q_entry.text().strip())
                if not (0.6 <= tg_q_ms <= 3.0):
                    raise ValueError("Tg q-axis must be between 0.6 and 3.0 ms")
            except ValueError as e:
                self._sig_tune_error.emit(str(e))
                return

            raw_rs  = self.serial_manager.send("g rsi  ", expect_response=True)
            raw_lsd = self.serial_manager.send("g lsdi ", expect_response=True)
            raw_lsq = self.serial_manager.send("g lsqi ", expect_response=True)
            rs_val  = dec_decode(raw_rs)
            lsd_val = dec_decode(raw_lsd)
            lsq_val = dec_decode(raw_lsq)
            logging.info(f"Staged ident results: Rs={rs_val:.6g} Ohm, "
                         f"Lsd={lsd_val*1e6:.1f} µH, Lsq={lsq_val*1e6:.1f} µH")

            self.serial_manager.send(f"s mrs   {dec_encode(rs_val)}",  expect_response=True)
            self.serial_manager.send(f"s mlsd  {dec_encode(lsd_val)}", expect_response=True)
            self.serial_manager.send(f"s mlsq  {dec_encode(lsq_val)}", expect_response=True)
            logging.info("Wrote Rs/Lsd/Lsq to firmware motor parameter struct")

            # Send Tg values (convert from ms to seconds) — required before suidq
            tg_d_s = tg_d_ms / 1000.0
            tg_q_s = tg_q_ms / 1000.0
            self.serial_manager.send(f"s itd   {dec_encode(tg_d_s)}", expect_response=True)
            self.serial_manager.send(f"s itq   {dec_encode(tg_q_s)}", expect_response=True)
            logging.info(f"Sent Tg values: Tg_d={tg_d_ms:.2f} ms, Tg_q={tg_q_ms:.2f} ms")

            response = self.serial_manager.send("s suidq", expect_response=True)
            logging.info(f"Current controller tuning command sent. Response: {response}")

            time.sleep(0.1)
            self._read_and_emit_pi_gains()
            self._sig_tune_done.emit()
        except Exception as e:
            logging.error(f"Failed to send suidq command: {e}")
            self._sig_tune_error.emit(str(e))

    def _on_tune_done(self):
        self.tune_btn.setText("Tune Current Controller")
        self.tune_btn.setEnabled(True)

    def _on_tune_error(self, msg: str):
        _show_error(self, "Error", f"Failed to tune current controller:\n{msg}")
        self.tune_btn.setText("Tune Current Controller")
        self.tune_btn.setEnabled(True)

    # ── Ident PsiF ────────────────────────────────────────────────────────────
    def on_ident_psif(self):
        if not self.serial_manager.is_open:
            _show_error(self, "Error", "Not connected to serial port.")
            return

        self.psif_ident_btn.setText("Identifying...")
        self.psif_ident_btn.setEnabled(False)
        self.psif_live_label.setText(_badge("starting...", "info"))
        threading.Thread(target=self._psif_worker, daemon=True).start()

    def _psif_worker(self):
        try:
            try:
                accel_torque_pct = float(self.psif_accel_torque_entry.text())
                if not (10 <= accel_torque_pct <= 70):
                    raise ValueError("Acceleration torque must be between 10 and 70%")
            except ValueError as e:
                self._sig_psif_error.emit(f"Invalid torque: {e}")
                return

            isd_ratio_pct = accel_torque_pct if accel_torque_pct <= 50.0 else 50.0
            isq_ratio_frac = accel_torque_pct / 100.0
            isd_ratio_frac = isd_ratio_pct  / 100.0

            isq_encoded = dec_encode(isq_ratio_frac)
            isd_encoded = dec_encode(isd_ratio_frac)

            self.serial_manager.send(f"s pisq  {isq_encoded}", expect_response=True)
            logging.info(f"Sent: s pisq {isq_encoded}  ({accel_torque_pct}%)")
            self.serial_manager.send(f"s pisd  {isd_encoded}", expect_response=True)
            logging.info(f"Sent: s pisd {isd_encoded}  ({isd_ratio_pct}%)")
            time.sleep(0.05)

            response = self.serial_manager.send("s psiid ", expect_response=True)
            logging.info(f"PsiF identification triggered: s psiid → Response: {response}")

            elapsed = 0.0
            while elapsed < self._PSIF_TIMEOUT_S:
                time.sleep(self._PSIF_POLL_S)
                elapsed += self._PSIF_POLL_S
                self._sig_psif_running.emit(int(elapsed))
                try:
                    raw_mode = self.serial_manager.send("g contr ", expect_response=True)
                    mode = int(round(dec_decode(raw_mode)))
                except Exception:
                    mode = -1
                if mode == 0:
                    break

            raw_psif = self.serial_manager.send("g mpsif ", expect_response=True)
            psif_wb  = dec_decode(raw_psif)
            logging.info(f"PsiF identified: {psif_wb:.6f} Wb")
            self._sig_psif_done.emit(psif_wb)
        except Exception as e:
            logging.error(f"PsiF identification failed: {e}")
            self._sig_psif_error.emit(str(e))

    def _on_psif_running(self, t: int):
        self.psif_live_label.setText(_badge(f"running… {t} s", "info"))

    def _on_psif_done(self, psif_wb: float):
        psif_mwb = psif_wb * 1000.0
        display = f"{psif_mwb:.3f} mWb"
        self.psif_live_label.setText(display)
        self.psif_label.setText(display)
        self.psif_ident_btn.setText("Ident PsiF")
        self.psif_ident_btn.setEnabled(True)

    def _on_psif_error(self, msg: str):
        _show_error(self, "Error", f"PsiF identification failed:\n{msg}")
        self.psif_live_label.setText(_badge("failed", "error"))
        self.psif_ident_btn.setText("Ident PsiF")
        self.psif_ident_btn.setEnabled(True)


# ── Standalone testing ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    class DummySerial:
        def __init__(self):
            self.is_open = True
        def send(self, cmd, expect_response=False):
            logging.info(f"Dummy send: {cmd}")
            if expect_response:
                if "gudc"  in cmd: return "+48.00000"
                if "pisq"  in cmd: return "+0.2500000"
                if "pisd"  in cmd: return "+0.2500000"
                if "mrs"   in cmd: return "+0.0150000"
                if "mlsd"  in cmd: return "+0.0003200"
                if "mlsq"  in cmd: return "+0.0003350"
                if "mpsif" in cmd: return "OK"
                if "elid"  in cmd: return "OK"
                if "suidq" in cmd: return "OK"
                if "kpisd" in cmd: return "+0.0012345"
                if "kiisd" in cmd: return "+0.0000456"
                if "kpisq" in cmd: return "+0.0012345"
                if "kiisq" in cmd: return "+0.0000456"
                if "psiid" in cmd: return "OK"
                if "contr" in cmd: return "+0.0000000"
                if "qusize" in cmd: return "+0.0000000"
                if "rsi"   in cmd: return "+0.0150000"
                if "lsdi"  in cmd: return "+0.0003200"
                if "lsqi"  in cmd: return "+0.0003350"
                return "+0.0000000"
            return ""

    app = QApplication(sys.argv)
    app.setFont(QFont("Segoe UI", 10))
    dlg = ElectricalParametersIdentification(
        None, DummySerial(),
        defaults={"pole": 7, "current": 5.0, "speed": 3000, "voltage": 27.7})
    dlg.show()
    sys.exit(app.exec())
