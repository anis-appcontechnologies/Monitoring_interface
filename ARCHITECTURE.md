# Architecture — AMC Interface (PySide6)

> Reference for developers. Describes threading contracts, serial-lock rules,
> scope state machine, and intentional divergences from the expert Tkinter version.

---

## Module Map

| File | Purpose | Key Classes |
|------|---------|-------------|
| `amc_interface_qt.py` | Main window, serial connection, background loops, command dispatch | `AMCMainWindow`, `SerialManager`, `QueuedCommandManager` |
| `scope_qt.py` | Oscilloscope — real-time, scroll, single-shot | `ScopeWindow`, `_ChannelCombo`, `_ElfVarPickerDialog` |
| `electrical_params_qt.py` | Electrical parameter identification (Rs, Ls, Psif) | `ElectricalParametersIdentification` |
| `inertia_param_qt.py` | Mechanical (inertia J) identification + speed PI tuning | `InertiaIdentification` |
| `save_params_qt.py` | Save parameters to device flash | `SaveParameters` |
| `load_params_qt.py` | Load parameters from device | `LoadParameters` |
| `terminal_qt.py` | Raw serial REPL terminal | `Terminal` |
| `si_format.py` | SI prefix formatting utility | — |

---

## Threading Model

### Background Polling Loops (start on connect, stop on disconnect)

All four loops are daemon threads. They share `SerialManager._lock`.

| Loop | Thread | Interval | Reads | scope_active gate |
|------|--------|----------|-------|-------------------|
| `_fault_loop` | daemon | 1 s | `g err` | **None** (removed Phase 4) |
| `_status_loop` | daemon | 1 s | `g contr`, `g sens` | **None** (removed Phase 4) |
| `_get_loop` | daemon | 1 s (10 × 100 ms) | `g {var}` × 3 | **None** (removed Phase 4) |
| `_cmd_loop` | daemon | 1 s (10 × 100 ms) | `g qusize` | Never had gate |

All loops post results to `response_q` (thread-safe `queue.Queue`). The main thread drains it every 80 ms via `_drain_response_queue` (QTimer).

### Scope Capture Threads

| Worker | Spawned by | Holds `_lock` | Duration |
|--------|-----------|---------------|---------|
| `_worker_record` | `_on_single_clicked` | Yes (whole read) | rec_time + up to 5 s polling |
| `_worker_realtime` | `_on_realtime_clicked` | Yes (per frame) | rec_time + up to 5 s polling |
| `_scroll_read_buffer` | `_scroll_poll_half` QTimer | Yes (whole read) | ~1 s + transfer |

### Serial Lock Contract

**Rule:** Every `_ser.write()`, `_ser.read()`, `_ser.readline()`, `_ser.reset_input_buffer()`, and `_ser.in_waiting` MUST be inside `with self.serial_manager._lock:`.

**Ordering rule (deadlock prevention):** Serial lock is always the outermost lock. The scroll ring-buffer lock (`_scroll_lock`) is always inner — never acquire serial lock while holding `_scroll_lock`.

### Scroll Ring Buffer

`_scroll_rings` (list of 4 `deque`) and `_scroll_t_ring` (`deque`) are written by `_scroll_read_buffer` (background thread) and read by `_scroll_display_tick` (main thread QTimer).

**Protection:** `_scroll_lock` (threading.Lock) must be held for all writes and for snapshot copies in the display tick.

---

## Scope State Machine

```
IDLE
  │  [Configure]
  ▼
CONFIGURED ──────────────────────────────────┐
  │  [Single Shot]   [Real-Time]   [Scroll]  │
  ▼        ▼              ▼                  │
RECORDING  RT_RUNNING   SCROLL_RUNNING       │
  │        │              │                  │
  └────────┴──────────────┘                  │
           │ [Stop / complete]               │
           ▼                                 │
      CONFIGURED ──────────────────────────────┘
           │ [Config changed]
           ▼
        UNCONFIGURED
```

- `scope_active` Event was used to gate background loops during scope ops — **removed in Phase 4**. The serial mutex alone provides serialization.
- Trigger checkbox (single-shot only): edge detection runs sample-by-sample inside `_worker_record` before arming.

---

## Combined View

The "⊞ Combined View" button embeds `ScopeWindow._scope_body` (a `QWidget` wrapping the controls panel + matplotlib canvas) into a right pane of the main window's outer `QSplitter`.

**API:**
- `ScopeWindow.detach_body()` → removes body from dialog, hides dialog, returns widget
- `ScopeWindow.attach_body()` → re-inserts body, shows dialog

**Rule:** Only one view at a time — combined OR standalone, never both. `open_monitoring()` exits combined view before showing standalone.

**Persistence:** Stored in `QSettings("Appcon Technologies", "AMC Interface")` key `combined_view`.

---

## Intentional Divergences from Expert Tkinter Version

| # | Expert behavior | Port behavior | Rationale |
|---|-----------------|---------------|-----------|
| 1 | Real-time: full `ax.clear()` per frame | 30 s rolling buffer, pan/zoom history | Feature addition — user can review recent history |
| 2 | No trigger | Trigger checkbox + threshold polling | Feature addition |
| 3 | Mode-lock: 2.0 s | **Fixed to 2.0 s** (was 3.5 s, now matches) | Phase 1 fix |
| 4 | No ELF picker UI | `_ElfVarPickerDialog` with search + add/remove | Feature addition |
| 5 | No dark mode | Full dark/light theme with `_toggle_theme` | Feature addition |
| 6 | No toast notifications | `_show_toast` overlay for events | Feature addition |
| 7 | No combined view | `⊞ Combined View` split-screen toggle | Feature addition |

---

## Validation Checklist (run after every merge to main)

- [ ] App launches < 3 s, no console exceptions
- [ ] Connect → green pill, all 4 loops running
- [ ] Mode switch 3× rapid — accepted after 2 s each
- [ ] Single-shot (max buffer) — bytes match, green status
- [ ] Real-time 60 s — no freeze, fault label updates during scope
- [ ] Scroll 60 s — ring buffer stable, pan/zoom works
- [ ] Disconnect mid real-time — graceful, no orphan thread
- [ ] Reconnect — loops resume, fault clears
- [ ] Dark → Light → Dark theme — Command panel legible both modes
- [ ] Add 3 ELF variables → 3 toasts appear
- [ ] Open Ch1 popup → Ch2 popup → only one open at a time
- [ ] Start Identification (mechanical) → completes, populates fields
- [ ] Combined view toggle mid-acquisition — no data loss
- [ ] 30 min real-time soak — memory stable
