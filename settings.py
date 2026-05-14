"""settings.py — Typed QSettings accessors for the AMC Interface application.
Single source of truth for all persistent setting keys and their defaults.

All QSettings calls in amc_interface_qt.py and scope_qt.py should go through
this module so key names, org/app strings, and type coercions are defined once.

Copyright: Appcon Technologies © 2025
"""

from PySide6.QtCore import QSettings

ORG = "Appcon Technologies"
APP = "AMC Interface"
APP_SCOPE = "AMC Scope"   # legacy name used by the scope save/load config feature


class Keys:
    # ── Main window ──────────────────────────────────────────────────────────
    LAST_PORT     = "last_port"          # str  — COM port name last connected
    COMBINED_VIEW = "combined_view"      # bool — scope opens in combined view

    # ── Scope channel scale (stored in AMC Interface app, not AMC Scope) ─────
    @staticmethod
    def ch_scale(ch_idx: int) -> str:
        """QSettings key for channel scale combo index (0-3)."""
        return f"scope/ch{ch_idx}_scale"

    # ── Scope session state (stored in AMC Scope app) ─────────────────────
    CH0          = "ch0"                 # str  — channel 0 firmware variable name
    CH1          = "ch1"                 # str  — channel 1
    CH2          = "ch2"                 # str  — channel 2
    CH3          = "ch3"                 # str  — channel 3
    REC_TIME     = "rec_time"            # float — record time in ms
    SAMPLE_FREQ  = "sample_freq"         # float — sample frequency in Hz
    T_DISPLAY    = "t_display"           # float — time-display window in seconds
    HIDE_LABELS  = "hide_labels"         # bool  — hide channel label overlay

    # ── Scope draw style ─────────────────────────────────────────────────────
    DRAW_STYLE   = "scope/drawstyle"     # str   — 'steps-post' or 'default'


def get(key: str, default=None, *, type_=None, app: str = APP) -> object:
    """Read a value from QSettings.

    Args:
        key:     Settings key (use Keys.* constants).
        default: Fallback when the key is absent.
        type_:   Python type for automatic coercion (bool, int, float, str).
                 When omitted, QSettings returns the stored value as-is.
        app:     Application name — use APP (default) or APP_SCOPE.
    """
    s = QSettings(ORG, app)
    if type_ is not None:
        return s.value(key, default, type=type_)
    return s.value(key, default)


def set_(key: str, value, *, app: str = APP) -> None:
    """Write a value to QSettings."""
    QSettings(ORG, app).setValue(key, value)
