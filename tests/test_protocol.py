"""
tests/test_protocol.py — Unit tests for protocol.py

Run from the project root:
    python -m pytest tests/test_protocol.py -v

Or without pytest:
    python tests/test_protocol.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protocol import dec_encode, dec_decode, encode_set, encode_get


# ── dec_encode ────────────────────────────────────────────────────────────────

def test_encode_positive_integer():
    result = dec_encode(16000.0)
    assert result == "+16000.000", repr(result)

def test_encode_positive_float():
    result = dec_encode(3.14)
    assert result == "+3.1400000", repr(result)

def test_encode_negative():
    result = dec_encode(-0.5)
    assert result == "-0.5000000", repr(result)

def test_encode_zero():
    result = dec_encode(0.0)
    assert result == "+0.0000000", repr(result)

def test_encode_exactly_10_chars():
    for v in [0.0, 1.0, -1.0, 3.14, 16000.0, 999999999.0]:
        result = dec_encode(v)
        assert len(result) == 10, f"len={len(result)} for {v}: {result!r}"

def test_encode_clamp_overflow():
    result = dec_encode(1e12)
    assert result == "+999999999", repr(result)

def test_encode_negative_clamp():
    result = dec_encode(-1e12)
    assert result == "-999999999", repr(result)


# ── dec_decode ────────────────────────────────────────────────────────────────

def test_decode_positive():
    assert dec_decode("+3.1400000") == 3.14

def test_decode_negative():
    assert dec_decode("-0.5000000") == -0.5

def test_decode_with_whitespace():
    assert dec_decode("  +16000.000  ") == 16000.0

def test_decode_empty_raises():
    try:
        dec_decode("")
        assert False, "should have raised"
    except ValueError:
        pass

def test_decode_whitespace_only_raises():
    try:
        dec_decode("   ")
        assert False, "should have raised"
    except ValueError:
        pass

def test_roundtrip():
    for v in [0.0, 1.0, -1.0, 3.14159, 16000.0, -500.25, 0.001]:
        encoded = dec_encode(v)
        decoded = dec_decode(encoded)
        assert abs(decoded - v) < 1e-4, f"roundtrip failed for {v}: {encoded!r} -> {decoded}"


# ── encode_set ────────────────────────────────────────────────────────────────

def test_encode_set_format():
    result = encode_set("rcptr", 0.0)
    assert result == b"#s rcptr +0.0000000;\n"

def test_encode_set_returns_bytes():
    assert isinstance(encode_set("fpwm", 16000.0), bytes)

def test_encode_set_negative_value():
    result = encode_set("iref", -5.0)
    assert result.startswith(b"#s iref -")
    assert result.endswith(b";\n")


# ── encode_get ────────────────────────────────────────────────────────────────

def test_encode_get_format():
    result = encode_get("fpwm")
    assert result == b"#g fpwm ;\n"

def test_encode_get_returns_bytes():
    assert isinstance(encode_get("contr"), bytes)

def test_encode_get_various_vars():
    for var in ["fpwm", "err", "contr", "recbuf", "rechalf"]:
        result = encode_get(var)
        assert result == f"#g {var} ;\n".encode("ascii"), repr(result)


# ── Standalone runner ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    import traceback
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except Exception:
            print(f"  FAIL  {fn.__name__}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
