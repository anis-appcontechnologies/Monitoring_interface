"""protocol.py — AMC firmware UART command codec.
Single source of truth for the #s/#g serial dialect used by the AMC firmware.

Encoding format (10-char fixed-width decimal):
  [sign][integer.fraction]  padded/truncated to exactly 10 characters.
  Example: 3.14  → '+3.1400000'
           -0.5  → '-0.5000000'
           16000 → '+16000.000'

Command wire format:
  Set: b'#s <var> <value>;\n'   e.g. b'#s rcptr +0.0000000;\n'
  Get: b'#g <var> ;\n'          e.g. b'#g fpwm ;\n'

Copyright: Appcon Technologies © 2025
"""


def dec_encode(value: float) -> str:
    """Encode a float into the 10-char AMC decimal format."""
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
    """Decode an AMC decimal response string to float. Raises ValueError on bad input."""
    s = s.strip()
    if not s:
        raise ValueError("Empty decimal string")
    return float(s)


def encode_set(var: str, value: float) -> bytes:
    """Build a '#s' (set) command frame: b'#s <var> <encoded_value>;\n'"""
    return f"#s {var} {dec_encode(value)};\n".encode("ascii")


def encode_get(var: str) -> bytes:
    """Build a '#g' (get) command frame: b'#g <var> ;\n'"""
    return f"#g {var} ;\n".encode("ascii")
