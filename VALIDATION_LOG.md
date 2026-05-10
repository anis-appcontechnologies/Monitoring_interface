# Validation Log

Each entry records a phase merge validation.
Format: `## Phase X — YYYY-MM-DD — PASS/FAIL — notes`

---

## Phase 1+2 — 2026-05-10 — PENDING HARDWARE TEST
Commit: `dbc03d3`, `7ad1b0c`
- [ ] Mode-lock 2.0 s confirmed on hardware
- [ ] Single-shot large buffer — bytes match
- [ ] Dark mode Command panel legible
- [ ] Toast on variable add/remove
- [ ] Singleton channel popup
- [ ] Fault label no longer squishes status pill

## Phase 3+4+6 — 2026-05-10 — PENDING HARDWARE TEST
Commit: `c2215fe`
- [ ] Cable-drop + manual disconnect race — no crash
- [ ] Fault label updates during real-time scope
- [ ] Status updates during single-shot capture
- [ ] Scroll mode 60 s — no RuntimeError
- [ ] Start Identification completes reliably 3/3 runs

## Phase 7 — 2026-05-10 — PENDING HARDWARE TEST
Commit: `b73c416`
- [ ] Combined view opens with scope embedded
- [ ] Splitter drag resizes both panels
- [ ] Toggle back to standalone — dialog appears
- [ ] Real-time running during toggle — no data loss
- [ ] Preference restores on relaunch
