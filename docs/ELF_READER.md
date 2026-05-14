# ELF Reader — Module Documentation

**File:** `elf_reader.py`  
**Author:** DAGBAGI Mohamed (Python port — Appcon Technologies)  
**Copyright:** Appcon © 2025

---

## 1. Purpose

`elf_reader.py` extracts the names, RAM addresses, byte sizes and C types of
every global variable from a compiled ARM Cortex-M firmware file (`.elf` or
`.axf`).  This information drives the **ELF Variable channel** feature in
the AMC Interface scope: the user loads an ELF file, picks variable names
from a list, and the scope plots their live values read from the MCU over UART.

---

## 2. Background — what replaced what

The original workflow at Appcon used two Octave scripts that called
proprietary Windows executables:

```
SCAN_SYMBOL.m       → APPCON_COMMAND_SCAN_SYMBOL.exe  → SYMBOL.txt
SCAN_GLOBAL_VAR.m  → APPCON_COMMAND_SCAN_GLOBAL_VAR (= arm-none-eabi-gdb) → GLOBAL_VAR.m
```

`SCAN_SYMBOL.m` ran the symbol-scan tool over the ELF and saved a text file
containing one line per `STT_OBJECT` symbol with its address and size.  
`SCAN_GLOBAL_VAR.m` fed those symbols to GDB (`ptype /o`) to recover C type
information, then wrote a MATLAB/Octave script (`GLOBAL_VAR.m`) assigning
one `struct(Name, Address, Size, Type, Format)` per symbol.

**`elf_reader.py` replaces the entire chain** using the pure-Python library
`pyelftools`.  No external executables (GDB, proprietary scanners) are
required.  The output is a Python `dict` of `SymbolInfo` objects that carry
the same fields: name, address, size, C type, and format code.

| Old output field | `SymbolInfo` attribute |
|---|---|
| `Name` | `name` |
| `Adress` (sic) | `address` |
| `Size` | `size` |
| `Type` | `c_type` |
| `Format` | `format_code` |

---

## 3. How it works — step by step

### 3.1 Primary path: Symbol Table (`.symtab`)

Every non-stripped ELF built by STM32CubeIDE / Keil / IAR contains a section
called `.symtab` (symbol table).  Each entry has a **type tag**.  We keep only
entries tagged `STT_OBJECT` — these are statically-allocated data objects
(global and static variables).

For each kept entry we record:
- **name** — the C identifier
- **address** — `st_value`, the runtime address in MCU memory
- **size** — `st_size`, the number of bytes the variable occupies

Names are filtered: any name that starts with `_` (compiler-internal) or
contains `.` (anonymous or compiler-generated) is discarded.  Zero-size
entries are also discarded.

This matches exactly what `SCAN_SYMBOL.m` did when it ran the scan tool and
grepped for the keyword `OBJECT`.

### 3.2 Fallback path: DWARF debug info

Some ELF files are stripped of the symbol table (e.g. a release build with
`--strip-all` but debug info kept with `-g`).  In that case `.symtab` is
empty and the primary path returns nothing.

The fallback iterates every DWARF Compilation Unit and every DIE
(`DW_TAG_variable`) that has:
- `DW_AT_name` — the variable name
- `DW_AT_location` — a `DW_OP_addr` (opcode `0x03`) expression containing the
  absolute address as 4-byte little-endian

These are the global variables that the compiler placed at a fixed address.
When `DW_AT_type` is also present we follow the type reference to recover the
C type string (e.g. `"float"`, `"uint16_t"`), which allows an accurate format
code to be assigned.

### 3.3 Format code assignment

Mirrors `type_to_format.m`:

| C type contains | byte size | format_code |
|---|---|---|
| `float` | any | `single` |
| `uint` / `unsigned` | 2 | `uint16` |
| `uint` / `unsigned` | other | `uint32` |
| `int` / `short` / `char` | 2 | `int16` |
| `int` / `short` / `char` | other | `int32` |
| anything else (struct, union, …) | — | `bytes` |

When no C type is available (symbol-table-only path) the format code is
derived from size alone, defaulting to `single` for 4-byte symbols.

### 3.4 RAM vs Flash

MCU RAM on STM32/NXP Cortex-M parts starts at `0x20000000`.  Any symbol with
`address >= 0x20000000` lives in RAM and **can be read back live** via the
`recvar` UART command.  Flash symbols (`address < 0x20000000`) are included in
the list for completeness but are marked non-RAM and cannot be used for live
oscilloscope channels.

---

## 4. Public API

### `read_symbols(elf_path: str) → dict[str, SymbolInfo]`

Parse a single ELF/AXF file.  Returns a dict mapping symbol name to a
`SymbolInfo` object.  Raises `FileNotFoundError` if the file does not exist.
Returns an empty dict if pyelftools is unavailable or the file has no symbols.

### `load(elf_path: str) → ElfModule`

Convenience wrapper.  Calls `read_symbols` and returns an `ElfModule` with:
- `module.path` — absolute path of the loaded file
- `module.symbol_info` — the full name→SymbolInfo dict
- `module.vars` — symbol names sorted case-insensitively
- `module.ram_vars` — subset of `vars` where `address >= 0x20000000`
- `module.get(name)` — look up one SymbolInfo by name

### `find_elf_in_folder(folder: str, timeout_s: float = 12.0) → list[str]`

Walk a project folder recursively for `.elf` / `.axf` files.  Returns paths
sorted by modification time with `Debug/` and `Release/` subdirectories
first.  Raises `TimeoutError` if the scan takes longer than `timeout_s`
seconds (prompts user to pick a narrower subfolder).

### `SymbolInfo` dataclass

| Field | Type | Description |
|---|---|---|
| `name` | `str` | C identifier |
| `address` | `int` | Absolute MCU address |
| `size` | `int` | Byte size |
| `c_type` | `str` | C type string (may be empty) |
| `format_code` | `str` | `single` / `uint16` / `int16` / `uint32` / `int32` / `bytes` |
| `source` | `str` | `symtab` or `dwarf` |
| `is_ram` | `bool` | `address >= 0x20000000` |

### `ElfModule` dataclass

| Field | Type | Description |
|---|---|---|
| `path` | `str` | Absolute ELF file path |
| `symbol_info` | `dict` | name → SymbolInfo |
| `vars` | `list[str]` | Sorted symbol names |
| `ram_vars` | `list[str]` | RAM-only subset |

---

## 5. Compatibility shim for scope_qt.py

`scope_qt.py` uses module-level globals and three private functions that were
originally defined inline.  `elf_reader.py` exposes identical shims so
`scope_qt.py` can import from it without any structural change:

```python
# In scope_qt.py (unchanged)
from elf_reader import (
    _ELF_VARS, _ELF_SYMBOL_INFO, _ELF_LOADED, _ELF_PATH,
    _elf_read_symbols, _elf_load, _elf_find_in_folder,
)
```

The shims:
- `_elf_read_symbols(path)` → `dict[name, (address, size)]`
- `_elf_load(path)` → `list[str]` and populates the four module globals
- `_elf_find_in_folder(folder)` → `list[str]`

---

## 6. Running standalone

Verify the module against a real ELF file without launching the full GUI:

```
# Show all symbols (up to 60 rows)
python elf_reader.py path/to/firmware.elf

# Show only RAM-resident symbols (usable for live channels)
python elf_reader.py path/to/firmware.elf --ram-only

# Scan a project folder and auto-select the newest ELF
python elf_reader.py path/to/stm32_project --folder

# Combine folder scan with RAM filter
python elf_reader.py path/to/stm32_project --folder --ram-only --max-rows 100
```

Example output:

```
Loading: C:\project\Debug\firmware.elf

NAME               ADDRESS       SIZE  RAM     FMT    SRC  TYPE
------------------------------------------------------------------
APBPrescTable    0x08008B98       8   no   int32  symtab
bMCBootCompleted 0x20000924       1  YES   int32  symtab
FOCVars          0x200009D0      38  YES   bytes  symtab
hadc1            0x200008AC     108  YES   bytes  symtab
PIDSpeedHandle_M1 0x200005B8     44  YES   bytes  symtab
...

Total symbols : 87
RAM symbols   : 64  (address >= 0x20000000 — usable for live read-back)
Flash symbols : 23
```

---

## 7. Integration with the scope (how the ELF channel works end-to-end)

```
User clicks "Load ELF"
      │
      ▼
elf_reader.load(path)          ← this module
      │  returns ElfModule
      ▼
scope_qt: populates channel dropdowns with module.vars
      │
User selects variable, presses Configure
      │
      ▼
scope_qt._worker_configure
  - looks up SymbolInfo.address in _ELF_SYMBOL_INFO
  - if address >= 0x20000000:  sends  #s rcva1 <address>  over UART
  - firmware reads *(float*)address from RAM and streams it in recbuf
      │
      ▼
scope_qt._do_plot: renders the sampled waveform
```

When the firmware is reflashed, `QFileSystemWatcher` detects the ELF file
change and calls `elf_reader._elf_load` again after an 800 ms delay,
refreshing the variable list automatically.

---

## 8. Dependencies

| Package | Version | Purpose |
|---|---|---|
| `pyelftools` | ≥ 0.29 | ELF / DWARF parsing |
| Python stdlib | 3.10+ | `dataclasses`, `struct`, `os`, `time` |

Install: `pip install pyelftools`

No GUI toolkit, no serial library, no NumPy — this module is intentionally
self-contained so it can be tested or used in any Python environment.

---

## 9. Changing the symbol filter

If a project uses a different naming convention (e.g. variables that
legitimately start with `_` or contain `.`), edit the `_bad_name()` function
at the top of `elf_reader.py`:

```python
def _bad_name(name: str) -> bool:
    return not name or name.startswith("_") or "." in name
```

This is the only place that controls which symbols are included or excluded.
No changes to `scope_qt.py` are needed.
