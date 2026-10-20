# Phase 0 spike: findings

Run 2026-10-20 on Windows 11, Python 3.14.4, pywinauto 0.6.9 (UIA backend),
one 1920x1080 monitor at 125% (120 DPI). Script: `spike/phase0.py`.

## Result: 4 of 4 unrelated apps driven and verified, no app-specific code in the driver

| App | Kind | UIA controls (named) | Tree walk | Actions | Verified by |
|---|---|---|---|---|---|
| Notepad | Win11 packaged Win32 | 45 (32) | 58 ms | SendInput click, Unicode typing, Ctrl+S | UIA TextPattern read-back |
| Calculator | UWP / ApplicationFrameHost | 53 (49) | 42 ms | 3x UIA Invoke (no mouse), 1 SendInput click | UIA Name "Display is 15" |
| Character Map | classic Win32 dialog | 26 (22) | 37 ms | SendInput click, Unicode typing, UIA Invoke | UIA ValuePattern |
| VS Code | Electron / Chromium | 200 (142) | 153 ms | coordinate click, Unicode typing, Ctrl+S | file on disk (UIA editor text not exposed) |

Each run produced before/after PNGs, an MP4 of the window region and a JSONL
action log. I checked the VS Code last frame and the Calculator after-shot by
eye: correct content, no offset.

## Findings that change the design

1. **Literal typing needs our own SendInput (KEYEVENTF_UNICODE).** pywinauto's
   `send_keys` parses `( ) { } + ^ % ~` and spaces as key syntax; the first run
   typed `print'hellofromovershoulder'` into VS Code. The driver now types text
   as Unicode code units and uses `send_keys` only for hotkeys.
2. **Focus guard works and is required.** Every keystroke checks the foreground
   window equals the scope window and raises `FocusLost` otherwise. This is the
   seed of scope enforcement for keyboard input.
3. **Electron hides editor text from UIA** unless screen-reader mode is on
   ("The editor is not accessible at this time..."). Chrome/Electron chrome
   (buttons, tabs, menus) is visible: 142 named controls. Verify for these apps
   needs OCR / pixel diff / side effects. This confirms the locator chain
   UIA -> OCR -> vision; consider enabling `--force-renderer-accessibility`
   when the gateway launches the app itself.
4. **Desktop-region capture leaks other windows.** `gdigrab` of the window
   rect records whatever is on top of or behind it: the Win11 invisible resize
   border (about 8 px) shows the neighbouring window's text, and any overlapping
   window would be recorded. For `window` scope, phase 1 should use
   Windows.Graphics.Capture (per-window capture) and
   `DWMWA_EXTENDED_FRAME_BOUNDS` for tight bounds; keep `gdigrab` for `screen` scope.
5. **Packaged apps change the PID.** Calculator's window belongs to
   `ApplicationFrameHost`, not the launched process. Scope binding must use the
   window handle plus the hosted app's process, not the launcher PID.
6. **Per-monitor DPI awareness v2 must be set before any UI import.** With it,
   UIA rects, mss and gdigrab agree in physical pixels at 125%. Multi-monitor
   and mixed-DPI behaviour is untested: this PC has one monitor.
7. **Speed is fine.** Full tree walks take 37-153 ms; capture and verify add
   well under a second per step.

## Reuse decision (from a read-only review of both repos)

- **CursorTouch/Windows-MCP (MIT, Python): reference only.** Worth adapting:
  secure-desktop probe (`control_win32.py`), low-level hook that tells injected
  from physical input (`control_hooks.py`) for "human took over",
  held-key ledger (`control_ledger.py`). Avoid: its 11k-line vendored UIA fork,
  deprecated `mouse_event`/`keybd_event`, posthog telemetry, and its
  PowerShell/Registry/FileSystem tools. It cannot sit behind our gateway
  without bypassing scope.
- **sbroenne/mcp-windows (MIT, C#/.NET 10): reference only.** Ideas to port:
  semantic-pattern-first actions with a physical click only after a foreground
  check, `IsPassword` and elevation checks (ours fail closed; theirs fail open),
  SendInput coordinate normalisation, snapshot diffing.
- Neither has window-bound scoping, hard password denies, approvals or an
  audit log, so the gateway is ours to write.

## Not covered by this spike

Drag, multi-monitor / mixed DPI, elevated windows and UAC (deny path),
password-field detection, human-input auto-pause, OCR, Windows.Graphics.Capture.

## Phase 1 adjustments

- Driver: pywinauto for UIA find + patterns; our own ctypes SendInput for
  mouse and Unicode text; `send_keys` only for hotkeys.
- Prefer UIA patterns (Invoke/Value/Toggle) over physical input; physical
  input only after the focus/foreground check.
- Window-scope capture via Windows.Graphics.Capture; screen scope via gdigrab.
- Port the injected-vs-physical input hook for auto-pause.
