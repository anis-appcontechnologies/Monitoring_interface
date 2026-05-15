# AMC Interface — UX Polish, Scope Bug Fixes, Full Responsive Pass

## Context

This is a follow-up to the now-completed expert-parity work. The user identified
**11 issues** during real usage and asked for them to be addressed in one batch:

- Errors currently surface only in the activity log — the user wants them as modal
  panels with information (visible, not buried).
- Neutral-grey mode buttons (recent ISA-101 change) make controls feel disabled —
  the user wants the white background restored.
- The "Use ELF symbol addresses (advanced)" submenu is unused noise — remove it.
- Multiple scope bugs vs the expert reference:
  - Realtime mode shows a fault after stop/action transitions.
  - Realtime is not as smooth as the expert version.
  - Zooming with cursors enabled corrupts the cursor lines.
  - Toggle staircase / toggle plot misbehaves in scroll & realtime.
- Cursor X/Y readout pill looks black/unwanted — needs a lighter design in a new
  position.
- Scope window has no connection LED — the user wants a small LED at the top of
  the oscilloscope panel.
- The GUI does not adapt well to different screen sizes — the user wants a full
  responsive pass (scope + all dialogs + QSS literals converted to `_px()`).

Reference for smooth scope behaviour:
`c:\Users\Aanis\Downloads\AMC_Interface_osciloscope\AMC_Interface\scope.py`.

**Constraints:** 19 protocol tests must remain green. No backwards-compatibility
shims. Do not touch `protocol.py`, `elf_reader.py`, `si_format.py`, `settings.py`.

---

## Implementation Order

Each block is independently testable. Run `py_compile` + `pytest tests/` after
each block.

### Block 1 — Quick UX wins (`amc_interface_qt.py` only)

1. **Errors as modals** — `_on_error_signal` (L3014–3015): keep the log call, add
   `_ModernModal.warn(self, "Error", message, buttons=(("OK", MODAL_CANCELLED),), level="error")`.
   Add a 2-second dedupe (`self._last_error_modal_ts`) to prevent twin modals when
   an error is paired with a disconnect. Init `self._last_error_modal_ts = 0.0` in
   `__init__`.

2. **Restore white mode buttons** — revert the QSS blocks changed in the previous
   ISA-101 pass:
   - L324–332 `QLabel#modes_active_pill` default: `background: {p['bg']}` →
     `{p['white']}`. Selected `[mode="Stop"]` / `[mode="active"]` keep their tints.
   - L506–511 `QPushButton#radio_btn, #radio_btn_stop, #radio_btn_active`:
     `background: {p['bg']}` → `{p['white']}`, `color: {p['muted']}` → `{p['text2']}`.
   - L532–543 `QFrame#mode_group_ctrl, #mode_group_sens`: `background: {p['bg']}`
     → `{p['white']}`. Keep neutral border.
   - L562–570 disabled blocks: `background: {p['bg']}` → `{p['white']}`.
     `color: {p['faint']}` stays.

3. **Remove ELF advanced submenu**:
   - L2417–2426: delete the entire `_elf_addr_action` block + the preceding
     separator (`monitor_menu.addSeparator()`) if it only exists for that item.
   - L3935: replace `dlg._use_elf_addresses = self._elf_addr_action.isChecked()`
     with `dlg._use_elf_addresses = False`.
   - L3942–3947: delete `_on_elf_addr_toggled` method.

### Block 2 — Scope behavioural bugs (`scope_qt.py`)

4. **Realtime → fault on start/stop** — `_on_realtime_clicked` (L3086–3101):
   inject a `recmod=0` write under lock both before starting realtime (clears
   any leftover scroll mode) and on stop. Also append the same `recmod=0` write
   in the `finally:` of `_worker_realtime` (L4123–4128).

   ```python
   def _send_recmod_zero():
       try:
           with self.serial_manager._lock:
               self.serial_manager._ser.write(
                   f"#s recmod {dec_encode(0.0)};\n".encode("ascii"))
       except Exception:
           logging.debug("recmod=0 write failed", exc_info=True)
   ```

5. **Realtime smoothness — warm-path Line2D update + draw_idle** — in `_do_plot`
   (L4389):

   - On first call (cold path): keep `ax.cla()` + full rebuild, store
     `self._drawstyle_cache = self._drawstyle` and `self._plotted_lines = {idx: Line2D}`.
   - On subsequent realtime calls with same channel topology: call
     `line.set_data(t_axis, ys)` per channel, then `self.canvas.draw_idle()`.
     Skip `tight_layout`. Honour `self._ylim_locked` for Y autoscale.

   This matches the expert pattern and gives ~5–10× smoother frames without
   touching `_worker_realtime`.

6. **Cursor zoom corruption** — `_do_plot` cold path (right after `self.ax.cla()`):
   null `self._cursor_a_line = None` and `self._cursor_b_line = None` (the
   existing code already nulls `_trigger_line` and `_hint_ax_text`).

   Also find the scroll-zoom mouse handler in `scope_qt.py` (Grep for
   `mpl_connect.*scroll` or `_on_canvas_scroll`). At the end of that handler,
   after `set_xlim/set_ylim`, call a new helper:

   ```python
   def _redraw_ab_lines(self):
       self._clear_ab_lines()
       if self._cursor_a is not None:
           self._cursor_a_line = self.ax.axvline(
               self._cursor_a[0], color='#F0A000',
               linewidth=1.0, linestyle='--', zorder=5)
       if self._cursor_b is not None:
           self._cursor_b_line = self.ax.axvline(
               self._cursor_b[0], color='#40C0A0',
               linewidth=1.0, linestyle='--', zorder=5)
       self.canvas.draw_idle()
   ```

7. **Toggle staircase / toggle plot live update** — `_on_drawstyle_toggled`
   (L4571–4579): apply `line.set_drawstyle(self._drawstyle)` to every line in
   `self._scroll_lines` and `self._plotted_lines`. If scroll is running,
   `canvas.draw()` + recapture `self._scroll_bg` (blit background invalidated by
   drawstyle change). If realtime, `canvas.draw_idle()`. If single-shot, replot
   from `self._last_plot_data`.

   Apply the same pattern to the toggle-plot button handler if it exists with
   the same staleness problem (Grep `toggle_plot` to confirm).

### Block 3 — Scope UX (`scope_qt.py`)

8. **Cursor X/Y overlay redesign** — QSS at L1958–1975 + repositioning at
   L1740–1746:

   ```css
   #sc_coords_overlay {
       background: rgba(255,255,255,0.92);
       border: 1px solid rgba(120,130,150,0.30);
       border-radius: 8px;
   }
   #sc_coords_ico  { color: {p['blue']};  font-size: 12px; }
   #sc_coords_val  { color: {p['text']}; font-size: 11px;
                     font-family: "Consolas","Cascadia Code",monospace; }
   #sc_coords_sep  { background: rgba(120,130,150,0.30); max-width: 1px; }
   ```

   `_reposition_coords`: move to top-right (`canvas.width() - width - 10, 10`).

   Cursor toggle button (`sc_btn_tool`, L2244–2268): replace red theme with
   minimal white-with-blue-on-hover/checked.

9. **Small "Connected" LED at top of scope panel** — in scope `__init__`
   where the channels header (`ch_hdr`) is built:

   ```python
   self._sc_led = QLabel("⬤  Disconnected")
   self._sc_led.setObjectName("led_disconnected")
   self._sc_led.setToolTip("Serial connection status")
   ch_hdr.insertWidget(1, self._sc_led)
   ```

   Add `led_connected` / `led_disconnected` styles scoped to `#sc_dialog`.
   500 ms `QTimer` calls `_refresh_sc_led()` to read
   `self.serial_manager.is_open` and update label + objectName + style polish.
   Stop the timer in the scope's close handler.

### Block 4 — Full responsive pass (every Qt file, every QSS literal)

User requested **the full treatment**: scope conversion + all dialogs + QSS
literal conversion to `_px()`.

10. **Expose `_px()` to every module** — in `amc_interface_qt.py` confirm `_px`
    is module-level (L210–218). In every other Qt file, add at the top:

    ```python
    try:
        from amc_interface_qt import _px
    except Exception:
        def _px(n: int) -> int: return n
    ```

11. **`scope_qt.py` — convert ~45 hardcoded sizes**. Grep targets:
    - `setFixedWidth(<int>)` / `setFixedHeight(<int>)` / `setFixedSize(<int>, <int>)`
    - `setMinimumWidth(<int>)` / `setMinimumHeight(<int>)` / `setMinimumSize(<int>, <int>)`
    - `setMaximumWidth(<int>)` / `setMaximumHeight(<int>)`
    - `resize(<int>, <int>)`
    - `QSize(<int>, <int>)`

    Wrap every literal int with `_px(...)`. Concrete line list from the audit
    (non-exhaustive; the actual edit pass will Grep):
    L629, 636, 670, 683, 694, 747, 748, 826, 902, 903, 1060, 1071, 1080, 1089,
    1120, 1127, 1142, 1157, 1243, 1262, 1282, 1321–1363, 1380, 1392, 1423,
    1432, 1447, 1481, 1724, 2590, 2665.

12. **`scope_qt.py` — add `QSplitter`** between the control panel and the
    canvas in `__init__`. Replace the parent `QHBoxLayout` with
    `QSplitter(Qt.Horizontal)`:

    ```python
    splitter = QSplitter(Qt.Orientation.Horizontal)
    splitter.addWidget(control_panel)
    splitter.addWidget(canvas_widget)
    splitter.setStretchFactor(0, 0)
    splitter.setStretchFactor(1, 1)
    splitter.setSizes([_px(280), _px(900)])
    layout.addWidget(splitter)
    # Persist split sizes
    settings = QSettings("Appcon Technologies", "AMC Interface")
    saved = settings.value("scope/splitter_sizes")
    if saved: splitter.restoreState(saved)
    splitter.splitterMoved.connect(
        lambda *_: settings.setValue("scope/splitter_sizes", splitter.saveState()))
    ```

13. **Canvas + log size policies** — set
    `self.canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)`
    and matching for the log panel (if visible).

14. **Dialog files — `electrical_params_qt.py`, `inertia_param_qt.py`,
    `save_params_qt.py`, `terminal_qt.py`**:

    - Replace hardcoded `self.resize(W, H)` and `self.setMinimumSize(W, H)` with
      a screen-ratio pattern:

      ```python
      _scr = QApplication.primaryScreen().availableGeometry()
      self.resize(min(_px(W), int(_scr.width()*0.8)),
                  min(_px(H), int(_scr.height()*0.8)))
      self.setMinimumSize(_px(W_min), _px(H_min))
      ```

    - Wrap every dialog's left/main column in `QScrollArea(setWidgetResizable=True)`
      where missing. `electrical_params_qt.py` already has this on the left
      column (L458–465) — extend to inertia and save_params dialogs.

    - Convert every `setFixedWidth/Height`, `setMinimumWidth/Height` literal
      with `_px(...)`.

15. **QSS literal conversion — every Qt file** — wherever a QSS block contains
    raw `px` literals (`min-height: 22px`, `padding: 7px 24px`, `font-size: 12px`,
    `border-radius: 8px`, etc.), convert to f-string interpolation with `_px()`:

    ```python
    # before
    qss = "QLineEdit { min-height: 22px; padding: 4px 8px; }"
    # after
    qss = f"QLineEdit {{ min-height: {_px(22)}px; padding: {_px(4)}px {_px(8)}px; }}"
    ```

    Concrete blocks to touch:
    - `amc_interface_qt.py`: every f-string QSS block in `_build_qss` that
      currently has bare `px` literals — there are ~30 blocks. Already partial
      via `fs()`; extend the pattern to `min-height`, `padding`,
      `border-radius`.
    - `scope_qt.py`: every QSS string in `__init__` (L1958–2700 range).
    - `electrical_params_qt.py`, `inertia_param_qt.py`, `save_params_qt.py`,
      `terminal_qt.py`: every inline QSS in those files.

    Mechanical pass: for each QSS string, regex `(\d+)px` → `{_px(\1)}px`,
    review each match, accept if it's a size primitive (margins, padding,
    radii, min-height/width, font-size, border-width). Skip `border: 1.5px`
    (fractional widths are usually intentional aesthetic choices).

---

## Files Modified

| File | Blocks |
|------|--------|
| `amc_interface_qt.py` | 1, 4 (responsive: QSS literals) |
| `scope_qt.py`         | 2, 3, 4 (responsive: 45+ sizes, QSplitter, QSS, LED) |
| `electrical_params_qt.py` | 4 (responsive: ratios, QScrollArea, QSS) |
| `inertia_param_qt.py`     | 4 (responsive: ratios, QScrollArea, QSS) |
| `save_params_qt.py`       | 4 (responsive: ratios, QScrollArea, QSS) |
| `terminal_qt.py`          | 4 (responsive: ratios, QSS) |

Reference (read-only): `c:\Users\Aanis\Downloads\AMC_Interface_osciloscope\AMC_Interface\scope.py`

---

## Verification

After **each block**:

```powershell
cd 'e:\apcon_web\AMC_Interface_260424\AMC_Interface'
python -m py_compile amc_interface_qt.py scope_qt.py electrical_params_qt.py inertia_param_qt.py save_params_qt.py terminal_qt.py
python -m pytest tests/ -v
```

All 19 tests must remain green throughout.

After **all blocks** — manual hardware checks:

| Item | Check |
|------|-------|
| 1 | Disconnect cable mid-poll → error modal appears once (dedupe holds for 2 s). |
| 2 | Disconnected app → mode buttons have white backgrounds and feel clickable. |
| 3 | Monitor menu has no "Use ELF symbol addresses" entry; ELF basic load still works. |
| 5 | Scroll → Stop → Realtime → no fault flag appears in status. |
| 6 | Realtime running for 30 s → smooth redraw, no canvas flicker. |
| 7 | Single-shot → place A/B cursors → scroll-zoom → cursors stay anchored. |
| 8 | Realtime running → toggle drawstyle → lines switch staircase/smooth without restart. |
| 9 | Cursor overlay sits top-right with white pill; cursor toggle button is blue-tinted. |
| 10 | Scope window header shows green "⬤ Connected" when port open; goes grey on disconnect within 500 ms. |
| 11 | Set Windows display scale 100 % / 125 % / 150 % → all windows scale proportionally; QSplitter drag persists. |

---

## What Is NOT Changing

- `protocol.py`, `elf_reader.py`, `si_format.py`, `settings.py`
- Tests in `tests/` (19 must remain green)
- Connect button colour (already green from previous work)
- Header `_conn_led` LED in main window (already correct)
- `_sig_connect_fail` modal path (already correct)
- Mode-button selected states (amber/red tints stay)
- The existing `_px()` helper at L210–218 of `amc_interface_qt.py` — used as-is
