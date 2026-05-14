"""
elf_reader.py — Standalone ELF symbol extractor
================================================
Appcon Technologies — AMC Interface project

Author  : DAGBAGI Mohamed (Python port from Octave/GDB pipeline)
Purpose : Extract global variable names, RAM addresses and sizes from a
          compiled ARM Cortex-M ELF or AXF file, without requiring GDB or
          any external tool.  Pure Python, depends only on pyelftools.

Background
----------
The original workflow used two Octave scripts that called proprietary
executables:
  SCAN_SYMBOL.m        → APPCON_COMMAND_SCAN_SYMBOL.exe  → SYMBOL.txt
  SCAN_GLOBAL_VAR.m   → APPCON_COMMAND_SCAN_GLOBAL_VAR  → GLOBAL_VAR.m

This module replaces that chain with direct ELF parsing via pyelftools.
It produces identical information (name, address, byte-size, C type) and
adds a DWARF fallback for partially-stripped ELF files.

Public API
----------
  read_symbols(elf_path)          -> dict[str, SymbolInfo]
  load(elf_path)                  -> ElfModule
  find_elf_in_folder(folder, ...) -> list[str]

  class SymbolInfo           — dataclass: name, address, size, c_type, format_code
  class ElfModule            — loaded state: vars, symbol_info, path

Run standalone (demo / verification)
-------------------------------------
  python elf_reader.py path/to/firmware.elf
  python elf_reader.py path/to/project/folder --folder

"""

from __future__ import annotations

import os
import struct
import time
import logging
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Optional dependency — silently disabled when pyelftools is not installed
# ---------------------------------------------------------------------------
try:
    from elftools.elf.elffile import ELFFile as _ELFFile
    _HAS_ELFTOOLS = True
except ImportError:
    _HAS_ELFTOOLS = False
    logging.warning(
        "elf_reader: pyelftools not installed — ELF parsing disabled. "
        "Install with:  pip install pyelftools"
    )

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class SymbolInfo:
    """All information about one global variable symbol extracted from ELF.

    Attributes
    ----------
    name         : C identifier as it appears in the source (e.g. ``FOCVars``)
    address      : Absolute RAM address (e.g. ``0x200009d0``).
                   Flash addresses (< 0x20000000) are included but are *not*
                   suitable for live read-back over UART.
    size         : Symbol size in bytes (struct size, not element count).
    c_type       : C type string from DWARF debug info, or ``""`` when not
                   available in the symbol table.
    format_code  : Suggested data format for display / serial decode:
                   ``'single'`` (float32), ``'uint16'``, ``'int16'``,
                   ``'uint32'``, ``'int32'``, or ``'bytes'``.
    source       : ``'symtab'`` when read from ``.symtab``/``.dynsym``;
                   ``'dwarf'`` when obtained from DWARF debug info.
    """
    name:        str
    address:     int
    size:        int
    c_type:      str  = ""
    format_code: str  = "single"
    source:      str  = "symtab"

    @property
    def is_ram(self) -> bool:
        """True when the symbol lives in MCU RAM (address ≥ 0x20000000)."""
        return self.address >= 0x2000_0000

    def __repr__(self) -> str:  # noqa: D105
        return (
            f"SymbolInfo(name={self.name!r}, addr=0x{self.address:08X}, "
            f"size={self.size}, type={self.c_type!r}, fmt={self.format_code!r})"
        )


@dataclass(slots=True)
class ElfModule:
    """Loaded state of one ELF file.

    Attributes
    ----------
    path         : Absolute path of the source ``.elf`` / ``.axf`` file.
    symbol_info  : Mapping  name → SymbolInfo  for every extracted symbol.
    vars         : Sorted list of symbol names (case-insensitive order).
    """
    path:        str
    symbol_info: dict[str, SymbolInfo] = field(default_factory=dict)
    vars:        list[str]             = field(default_factory=list)

    @property
    def ram_vars(self) -> list[str]:
        """Names of symbols whose address is in MCU RAM (≥ 0x20000000)."""
        return [n for n in self.vars if self.symbol_info[n].is_ram]

    def get(self, name: str) -> Optional[SymbolInfo]:
        """Return SymbolInfo for *name*, or None if not found."""
        return self.symbol_info.get(name)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _bad_name(name: str) -> bool:
    """Return True for names that are compiler internals, not user variables.

    Keeps:  plain names (``Isq``) and dotted struct members (``FOCVars.Isq``)
    Drops:  names starting with ``_``, purely numeric, containing ``<``, ``>``,
            ``@``, ``$``, or a dot-only segment (compiler temporaries like
            ``..LCPI0_0`` or ``__compound_literal``).
    """
    if not name:
        return True
    if name.startswith("_"):
        return True
    # Reject any compiler/linker artifact characters
    if any(ch in name for ch in ("<", ">", "@", "$", " ", "\t")):
        return True
    # Allow dotted struct names (FOCVars.Isq) but reject degenerate dots
    # (.rodata.str, ..text, or a segment that is purely numeric / empty)
    if "." in name:
        parts = name.split(".")
        # Leading/trailing dots, consecutive dots, or any empty/numeric segment
        if any(not p or p.isdigit() for p in parts):
            return True
        # Section-style names start with a dot  (e.g. ".bss.FooBar")
        if name.startswith("."):
            return True
    return False


def _type_to_format(c_type: str, byte_size: int) -> str:
    """Map a C type string + byte size to a format code.

    Mirrors the logic in the original Octave ``type_to_format.m``:
      float            → 'single'
      uint / unsigned  → 'uint16' or 'uint32'
      (default signed) → 'int16' or 'int32'
    Returns ``'bytes'`` for structs and other composite types.
    """
    t = c_type.lower().strip()
    if "float" in t:
        return "single"
    if "uint" in t or "unsigned" in t:
        return "uint16" if byte_size == 2 else "uint32"
    if "int" in t or "short" in t or "long" in t or "char" in t:
        return "int16" if byte_size == 2 else "int32"
    # Struct, union, array, or unknown composite
    return "bytes"


# ---------------------------------------------------------------------------
# DWARF helpers
# ---------------------------------------------------------------------------

def _dwarf_decode_name(raw) -> str:
    return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw


def _dwarf_extract_type(die, cu) -> tuple[str, int]:
    """Return (c_type_string, byte_size) by following DW_AT_type chains.

    Handles:  base types, typedefs, const/volatile qualifiers, pointers,
              and structs/unions (returns struct name + byte_size).
    Returns ("", 4) when the type cannot be resolved.
    """
    c_type = ""
    byte_size = 0
    try:
        type_attr = die.attributes.get("DW_AT_type")
        if type_attr is None:
            return c_type, byte_size

        # Follow typedef / const / volatile chains (max 8 hops to avoid loops)
        type_die = die.dwarfinfo.get_DIE_from_refaddr(type_attr.value, cu)
        for _ in range(8):
            if type_die.tag in ("DW_TAG_typedef", "DW_TAG_const_type",
                                "DW_TAG_volatile_type", "DW_TAG_restrict_type"):
                inner = type_die.attributes.get("DW_AT_type")
                if inner is None:
                    break
                type_die = die.dwarfinfo.get_DIE_from_refaddr(inner.value, cu)
                continue

            if "DW_AT_name" in type_die.attributes:
                c_type = _dwarf_decode_name(type_die.attributes["DW_AT_name"].value)
            sz = type_die.attributes.get("DW_AT_byte_size")
            if sz is not None:
                byte_size = sz.value
            break
    except Exception:
        pass
    return c_type, byte_size


def _dwarf_walk_cu(cu, result: dict) -> None:
    """Walk every DIE in *cu* (including nested children) looking for
    DW_TAG_variable entries with a static DW_OP_addr location.

    Only top-level variables (children of the CU or of a namespace/class) are
    considered.  Local stack variables inside functions are skipped because
    they have no fixed address.
    """
    try:
        top_die = cu.get_top_DIE()
    except Exception:
        return

    def _visit(die, depth: int):
        if die.tag == "DW_TAG_variable":
            attrs = die.attributes
            if "DW_AT_name" not in attrs or "DW_AT_location" not in attrs:
                return
            raw  = attrs["DW_AT_name"].value
            name = _dwarf_decode_name(raw)
            if _bad_name(name):
                return

            loc = attrs["DW_AT_location"].value
            # Accept DW_OP_addr (0x03) with a 4-byte LE address
            if isinstance(loc, list) and len(loc) >= 5 and loc[0] == 0x03:
                addr = struct.unpack_from("<I", bytes(loc[1:5]))[0]
            else:
                return

            # Size: from the variable's own DW_AT_byte_size, or resolved type
            sz_attr = attrs.get("DW_AT_byte_size")
            if sz_attr is not None:
                size = sz_attr.value
                c_type, _ = _dwarf_extract_type(die, cu)
            else:
                c_type, size = _dwarf_extract_type(die, cu)
                if size == 0:
                    size = 4  # conservative fallback

            fmt = _type_to_format(c_type, size)
            if name not in result:
                result[name] = SymbolInfo(
                    name=name, address=addr, size=size,
                    c_type=c_type, format_code=fmt, source="dwarf",
                )
            return

        # Recurse into namespaces, structs, translation units, etc.
        # Skip function bodies — their variables are stack-local
        if die.tag in ("DW_TAG_subprogram", "DW_TAG_inlined_subroutine"):
            return
        for child in die.iter_children():
            _visit(child, depth + 1)

    for child in top_die.iter_children():
        _visit(child, 0)


# ---------------------------------------------------------------------------
# Core symbol extraction
# ---------------------------------------------------------------------------

def read_symbols(elf_path: str) -> dict[str, SymbolInfo]:
    """Parse an ELF / AXF file and return all usable global variable symbols.

    Strategy
    --------
    1. **Primary — symbol table** (``.symtab`` / ``.dynsym``):
       Iterates every ``STT_OBJECT`` entry.  These entries exist in
       non-stripped ELF files and give name + address + size with no type
       information.  The type is inferred from size via :func:`_type_to_format`.

    2. **Fallback — DWARF debug info** (``DW_TAG_variable`` with
       ``DW_OP_addr`` location expression):
       Used automatically when the symbol table is empty (e.g. ELF stripped
       with ``--strip-all`` but debug info retained via ``-g``).  DWARF
       provides the C type string, so format codes are more accurate.

    Parameters
    ----------
    elf_path : str
        Absolute or relative path to the ``.elf`` or ``.axf`` file.

    Returns
    -------
    dict[str, SymbolInfo]
        Mapping  name → SymbolInfo.  Empty dict if pyelftools is missing or
        the file contains no usable symbols.

    Raises
    ------
    FileNotFoundError
        When *elf_path* does not exist.
    """
    if not _HAS_ELFTOOLS:
        log.error("read_symbols: pyelftools not available.")
        return {}
    if not os.path.isfile(elf_path):
        raise FileNotFoundError(f"ELF file not found: {elf_path}")

    result: dict[str, SymbolInfo] = {}

    try:
        with open(elf_path, "rb") as fh:
            elf = _ELFFile(fh)

            # ── 1. Symbol table ─────────────────────────────────────────────
            for sec in elf.iter_sections():
                if sec.name not in (".symtab", ".dynsym"):
                    continue
                for sym in sec.iter_symbols():
                    if sym.entry["st_info"]["type"] != "STT_OBJECT":
                        continue
                    name = sym.name
                    size = sym.entry["st_size"]
                    addr = sym.entry["st_value"]
                    if size == 0 or _bad_name(name):
                        continue
                    fmt = _type_to_format("", size)   # no type from symtab
                    result[name] = SymbolInfo(
                        name=name, address=addr, size=size,
                        c_type="", format_code=fmt, source="symtab",
                    )

            # ── 2. DWARF fallback ────────────────────────────────────────────
            if not result and elf.has_dwarf_info():
                di = elf.get_dwarf_info()
                for cu in di.iter_CUs():
                    _dwarf_walk_cu(cu, result)

    except Exception as exc:
        log.warning("ELF symbol read failed (%s): %s", elf_path, exc)

    log.info("elf_reader: extracted %d symbols from %s", len(result), elf_path)
    return result


# ---------------------------------------------------------------------------
# High-level load / folder-scan
# ---------------------------------------------------------------------------

def load(elf_path: str) -> ElfModule:
    """Load an ELF file and return a ready-to-use :class:`ElfModule`.

    Parameters
    ----------
    elf_path : str
        Path to the ``.elf`` or ``.axf`` file.

    Returns
    -------
    ElfModule
        Populated module object.  ``module.vars`` is empty if no symbols were
        found or pyelftools is missing.

    Raises
    ------
    FileNotFoundError
        When *elf_path* does not exist.
    """
    info = read_symbols(elf_path)
    return ElfModule(
        path=os.path.abspath(elf_path),
        symbol_info=info,
        vars=sorted(info.keys(), key=str.lower),
    )


def find_elf_in_folder(folder: str, timeout_s: float = 12.0) -> list[str]:
    """Walk *folder* recursively and return paths of all ``.elf`` / ``.axf`` files.

    Files under ``Debug/`` or ``Release/`` subdirectories are returned first,
    sorted by modification time (newest first), so that the most recent build
    naturally appears at index 0.

    Parameters
    ----------
    folder : str
        Root directory to search (e.g. an STM32CubeIDE project root).
    timeout_s : float
        Maximum seconds for the filesystem walk (default 12).  Raises
        :class:`TimeoutError` if exceeded — prompts the user to pick a
        narrower folder.

    Returns
    -------
    list[str]
        Absolute paths; Debug/Release first, then everything else.

    Raises
    ------
    TimeoutError
        When the walk exceeds *timeout_s*.
    """
    deadline   = time.monotonic() + timeout_s
    preferred  = []   # under Debug/ or Release/
    other      = []

    for root, _, files in os.walk(folder):
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"Folder scan exceeded {timeout_s:.0f} s — "
                "select a more specific subfolder (e.g. Debug or Release)."
            )
        for fn in files:
            if fn.lower().endswith((".elf", ".axf")):
                full  = os.path.join(root, fn)
                mtime = os.path.getmtime(full)
                bucket = (preferred
                          if (os.sep + "Debug"   + os.sep in full or
                              os.sep + "Release" + os.sep in full)
                          else other)
                bucket.append((mtime, full))

    preferred.sort(reverse=True)
    other.sort(reverse=True)
    return [p for _, p in preferred] + [p for _, p in other]


# ---------------------------------------------------------------------------
# Compatibility shim — keeps scope_qt.py working without changes
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class _ElfState:
    vars:         tuple              = ()
    symbol_info:  dict               = None  # type: ignore[assignment]
    loaded:       bool               = False
    path:         str                = ""

    def __post_init__(self):
        # Allow None → empty dict on init
        if self.symbol_info is None:
            object.__setattr__(self, "symbol_info", {})


_elf_state: _ElfState = _ElfState()


def __getattr__(name: str):
    """Module-level attribute hook — exposes _ELF_* names for scope_qt.py."""
    if name == "_ELF_VARS":        return list(_elf_state.vars)
    if name == "_ELF_SYMBOL_INFO": return _elf_state.symbol_info
    if name == "_ELF_LOADED":      return _elf_state.loaded
    if name == "_ELF_PATH":        return _elf_state.path
    raise AttributeError(f"module 'elf_reader' has no attribute {name!r}")


def _elf_read_symbols(elf_path: str) -> dict[str, tuple]:
    """Legacy shim: returns {name: (address, size)} as expected by scope_qt.py."""
    info = read_symbols(elf_path)
    return {name: (sym.address, sym.size) for name, sym in info.items()}


def _elf_load(elf_path: str) -> list[str]:
    """Legacy shim: atomically updates module state and returns sorted name list."""
    global _elf_state
    if not os.path.isfile(elf_path):
        log.warning("ELF path does not exist: %s", elf_path)
        return []
    info = _elf_read_symbols(elf_path)
    if not info:
        log.warning(
            "No usable symbols found in %s. "
            "Rebuild with -g debug info and ensure the ELF is not stripped. "
            "In STM32CubeIDE: Properties → C/C++ Build → Settings → MCU Post build — uncheck Strip.",
            elf_path,
        )
        return []
    _elf_state = _ElfState(
        vars=tuple(sorted(info.keys(), key=str.lower)),
        symbol_info=info,
        loaded=True,
        path=os.path.abspath(elf_path),
    )
    return list(_elf_state.vars)


def _elf_find_in_folder(folder: str, timeout_s: float = 12.0) -> list[str]:
    """Legacy shim: wraps find_elf_in_folder for scope_qt.py."""
    return find_elf_in_folder(folder, timeout_s)


# ---------------------------------------------------------------------------
# Standalone demo / verification entry point
# ---------------------------------------------------------------------------

def _print_table(module: ElfModule, max_rows: int = 40) -> None:
    """Pretty-print the first *max_rows* symbols of a loaded ElfModule."""
    w_name = max((len(n) for n in module.vars[:max_rows]), default=10)
    w_name = max(w_name, 10)
    header = f"{'NAME':<{w_name}}  {'ADDRESS':>10}  {'SIZE':>6}  {'RAM':>3}  {'FMT':>7}  {'SRC':>6}  TYPE"
    print(header)
    print("-" * len(header))
    for name in module.vars[:max_rows]:
        sym = module.symbol_info[name]
        ram = "YES" if sym.is_ram else "no "
        print(f"{sym.name:<{w_name}}  0x{sym.address:08X}  {sym.size:>6}  {ram:>3}"
              f"  {sym.format_code:>7}  {sym.source:>6}  {sym.c_type}")
    if len(module.vars) > max_rows:
        print(f"  ... and {len(module.vars) - max_rows} more symbols")
    print()
    ram_count = len(module.ram_vars)
    print(f"Total symbols : {len(module.vars)}")
    print(f"RAM symbols   : {ram_count}  (address >= 0x20000000 — usable for live read-back)")
    print(f"Flash symbols : {len(module.vars) - ram_count}")


if __name__ == "__main__":
    import sys
    import argparse

    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

    parser = argparse.ArgumentParser(
        description="AMC Interface — ELF symbol extractor (standalone demo)"
    )
    parser.add_argument("path", help="Path to .elf/.axf file, or project folder when --folder is set")
    parser.add_argument("--folder", action="store_true",
                        help="Search path recursively for .elf/.axf files and load the newest one")
    parser.add_argument("--ram-only", action="store_true",
                        help="Print only RAM-resident symbols (address >= 0x20000000)")
    parser.add_argument("--max-rows", type=int, default=60,
                        help="Maximum symbol rows to print (default 60)")
    args = parser.parse_args()

    if not _HAS_ELFTOOLS:
        print("ERROR: pyelftools is not installed.")
        print("       Run:  pip install pyelftools")
        sys.exit(1)

    target_path = args.path

    if args.folder:
        print(f"Scanning folder: {target_path}")
        try:
            candidates = find_elf_in_folder(target_path)
        except TimeoutError as e:
            print(f"ERROR: {e}")
            sys.exit(1)
        if not candidates:
            print("No .elf or .axf files found in that folder.")
            sys.exit(1)
        print(f"Found {len(candidates)} ELF file(s). Loading newest: {candidates[0]}")
        target_path = candidates[0]

    print(f"\nLoading: {target_path}\n")
    try:
        module = load(target_path)
    except FileNotFoundError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    if not module.vars:
        print("WARNING: No usable symbols found in this ELF file.")
        print()
        print("  Possible causes and fixes:")
        print("  1. ELF was stripped — rebuild with  -g  (keep debug info) and do NOT run")
        print("     arm-none-eabi-strip on the output.  In STM32CubeIDE: Properties →")
        print("     C/C++ Build → Settings → MCU Post build outputs — uncheck 'Strip'.")
        print("  2. Using a Release build — switch to Debug or add -g to Release CFLAGS.")
        print("  3. File has DWARF but all variables are local (stack) — make the globals")
        print("     you want to monitor non-static at file scope.")
        print("  4. Only want RAM variables?  Re-run with  --ram-only  to check how many")
        print("     symbols are in flash vs RAM.")
        print()
        print("  Quick check:  arm-none-eabi-nm --print-size --size-sort firmware.elf | head")
        print("  If that returns nothing, the ELF is stripped.")
        sys.exit(0)

    if args.ram_only:
        # Filter to RAM-only for display
        filtered = ElfModule(
            path=module.path,
            symbol_info={n: module.symbol_info[n] for n in module.ram_vars},
            vars=module.ram_vars,
        )
        _print_table(filtered, args.max_rows)
    else:
        _print_table(module, args.max_rows)