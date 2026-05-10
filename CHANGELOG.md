# Changelog

All notable changes to AMC Interface are documented here.
Format: `[version] — date — summary`

---

## [1.0.0] — 2026-05-10 — Stabilization & Professionalization Release

### Phase 7 — Combined Split-Screen View
- **New:** "⊞ Combined View" toggle button in header bar (`Ctrl+Shift+M`)
- Embeds the oscilloscope panel (controls + canvas) beside the main interface in a resizable QSplitter
- Standalone scope window still available via Monitoring menu
- View preference persisted across sessions via QSettings
- Acquisition continues uninterrupted during view toggle (no canvas recreation)

### Phase 4 — Threading & Serial Concurrency (expert parity)
- **Fix:** Removed `scope_active` gating from fault/status/GET loops — all three loops now run continuously during scope operations, matching expert Tkinter behavior
- **Fix:** `_scroll_lock` was declared but never used — now protects deque writes (`_scroll_read_buffer`) and deque reads (`_scroll_display_tick`) against the scroll-mode race condition
- Scope acquisition uses expert-style polling read loop (5 s timeout, chunk reads) in both single-shot and real-time modes

### Phase 3 — Connect/Disconnect Hardening
- **Fix:** Double-disconnect guard prevents cable-drop + manual-disconnect race condition
- All four background loops are stopped *before* `serial.disconnect()` — no loop fires a read after the port closes
- `_set_disconnected_ui` is fully idempotent — safe to call from any code path

### Phase 6 — Mechanical Identification Parity
- **Fix:** `open_mechanical_params` now stops fault/GET/status loops and flushes serial RX buffer before launching the identification dialog — matches the approach used by electrical identification
- Identification worker gets exclusive serial access; eliminates intermittent J/friction read failures

### Phase 2 — Monitoring UX
- **New:** Non-blocking toast notifications when adding or removing ELF variables (`"Variable 'X' added to Ch2"` / `"Variable 'X' removed"`)
- **Fix:** Channel dropdown popups are now singleton — opening any channel's popup closes the other three automatically
- **Fix:** Expert-style polling read loop in `_worker_record` and `_worker_realtime` — large buffers no longer truncate

### Phase 1 — Critical Correctness
- **Fix:** Mode-lock duration reduced from 3.5 s to 2.0 s on all four call sites (matches expert Tkinter reference)
- **Fix:** Dark mode Command panel — `_flash_read_entry` and `_update_write_entry` now use palette colors; no more invisible values when dark theme is active
- **Fix:** `QLineEdit[readOnly]` uses `input_bg` + `text2` for proper contrast in dark mode
- **Fix:** Fault label: `setMaximumHeight(56)` prevents word-wrap from squishing the sibling status pill

---

## [0.0-baseline] — 2026-05-07 — Initial port baseline

- PySide6 port of expert Tkinter AMC Interface
- Features: serial connect/disconnect, real-time / scroll / single-shot oscilloscope, ELF variable picker, mechanical + electrical identification, dark/light theme, activity log, command panel
