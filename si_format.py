"""
SI-compliant formatting utilities for displaying measurement results and parameters.

All displayed values use SI prefixes with exponents as multiples of 3:
  10^3  = kilo (k)
  10^0  = base unit
  10^-3 = milli (m)
  10^-6 = micro (μ)
  10^-9 = nano (n)
"""

import math


def si_format(value: float, unit: str = "", precision: int = 2) -> str:
    """Format a numeric value with SI prefix notation.

    Args:
        value: The numeric value to format
        unit: The unit string (e.g., "V", "A", "Ω", "H", "kg·m²")
        precision: Number of decimal places (default 2)

    Returns:
        Formatted string with SI prefix (e.g., "4.00 μA", "1.75 mH", "34 mΩ")

    Examples:
        si_format(0.000004, "A") -> "4.00 μA"
        si_format(0.034, "Ohm") -> "34 mOhm"
        si_format(0.00007, "H") -> "70 μH"
        si_format(0.090, "") -> "90 m"
        si_format(0.0, "A") -> "0 A"
    """
    if not math.isfinite(value) or value == 0.0:
        return f"0 {unit}".strip()

    # Handle negative values
    sign = "" if value >= 0 else "-"
    abs_value = abs(value)

    # Calculate the SI exponent (multiple of 3)
    # Use floor to round DOWN to the nearest lower SI exponent
    # This keeps mantissa in range 1-999 instead of 0.1-99
    exponent = math.log10(abs_value)
    exponent_si = math.floor(exponent / 3) * 3

    # Calculate mantissa
    mantissa = abs_value / (10 ** exponent_si)

    # Format mantissa with specified precision
    # If mantissa is whole number or very close, use 0 decimals
    if abs(mantissa - round(mantissa)) < 0.001:
        mantissa_str = f"{mantissa:.0f}"
    else:
        mantissa_str = f"{mantissa:.{precision}f}"

    # Determine SI prefix
    prefix_map = {
        9: "G",      # giga
        6: "M",      # mega
        3: "k",      # kilo
        0: "",       # base unit
        -3: "m",     # milli
        -6: "μ",     # micro
        -9: "n",     # nano
        -12: "p",    # pico
    }

    prefix = prefix_map.get(exponent_si, f"10^{exponent_si}")

    # Format result
    if unit:
        if isinstance(prefix, str) and len(prefix) == 1:
            # For single-character prefixes, attach to unit
            formatted_unit = f"{prefix}{unit}"
        else:
            # For composite prefixes (like "10^-15"), separate with space
            formatted_unit = f"{prefix} {unit}"
        return f"{sign}{mantissa_str} {formatted_unit}".strip()
    else:
        # For dimensionless values, use SI prefix map same as unit path
        prefix = prefix_map.get(exponent_si, f"×10^{exponent_si}")
        return f"{sign}{mantissa_str} {prefix}".strip()


def si_format_resistance(value: float) -> str:
    """Format resistance in Ω with SI prefix."""
    return si_format(value, "Ω", precision=3)


def si_format_inductance(value: float) -> str:
    """Format inductance in H with SI prefix."""
    return si_format(value, "H", precision=2)


def si_format_voltage(value: float) -> str:
    """Format voltage in V with SI prefix."""
    return si_format(value, "V", precision=3)


def si_format_current(value: float) -> str:
    """Format current in A with SI prefix."""
    return si_format(value, "A", precision=3)


def si_format_inertia(value: float) -> str:
    """Format inertia J in kg·m² with SI prefix."""
    return si_format(value, "kg·m²", precision=2)


def si_format_flux(value: float) -> str:
    """Format flux linkage in Wb with SI prefix."""
    return si_format(value, "Wb", precision=3)


def si_format_friction(value: float) -> str:
    """Format friction coefficient in Nm·s/rad with SI prefix."""
    return si_format(value, "Nm·s/rad", precision=3)
