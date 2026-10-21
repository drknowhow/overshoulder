# Phase 2 spike: acting on a background window without the person's mouse or keyboard

Run 2026-10-21 on Windows 11, one 1920x1080 monitor at 125%. Scripts: `spike/phase2_background.py`
(matrix), `spike/phase2_probe.py`, `spike/phase2_calc.py`, `spike/phase2_calc_fg.py`,
`spike/phase2_chromium_click.py`. Raw results: `docs/evidence/phase2-spike/`.

## How it was measured

A sentinel window owned by the spike held the foreground; each target app was launched behind it,
and the sentinel was given the foreground again before every measurement. A test counts as
**background** only if all of these held:

- the action had its effect, checked by UI Automation read-back, the file on disk, or (for
  Character Map's grid) a PrintWindow pixel diff, all of which work on covered windows;
- a low-level hook counted **zero injected input events** during the action;
- the **foreground window did not change**;
- the **real cursor did not move**.

No physical input was recorded during the runs, so a person at the desk did not affect the results.

## Results

| App (kind) | Action | Method | Background? |
|---|---|---|---|
| Notepad (Win32 + XAML) | type text | `WM_CHAR` posted to the editor (`RichEditD2DPT`) | yes |
| Notepad | type text | `EM_REPLACESEL` sent to the editor | yes |
| Notepad | place caret | posted `WM_LBUTTONDOWN/UP` to the editor, then `WM_CHAR` | yes |
| Notepad | Ctrl+S | posted Ctrl/S key messages | no (types an "s"; apps read Ctrl from thread key state) |
| Notepad | Ctrl+S | posted `S` with Ctrl set in the target thread's key state (`AttachThreadInput` + `SetKeyboardState`) | **yes**, file saved |
| Notepad | File > Save | UIA menu expand + invoke | no (the XAML flyout never opened in the background) |
| Notepad | read text | UIA TextPattern | yes (a full 52-node tree is available in the background) |
| Calculator (UWP/XAML) | press buttons | UIA Invoke | effect yes, but **it takes the foreground** |
| Calculator | type digits/operators | `WM_CHAR` posted to `Windows.UI.Core.CoreWindow` | yes |
| Calculator | press a button | posted click to CoreWindow | no |
| Calculator | launch | `SW_SHOWNOACTIVATE` | no; UWP apps take the foreground when they start |
| Character Map (Win32) | set text | UIA ValuePattern, `WM_SETTEXT` | yes |
| Character Map | type text | `WM_CHAR` posted to the edit | yes |
| Character Map | press Select | UIA Invoke, `BM_CLICK` | yes |
| Character Map | click a grid cell | posted click to `CharGridWClass` | yes |
| VS Code (Electron/Chromium) | type text | `WM_CHAR` posted to the render widget | no on its own |
| VS Code | type text | UIA `SetFocus` on the editor element, then `WM_CHAR` to the render widget | **yes**, file saved by autosave |
| VS Code | set editor text | UIA ValuePattern | no |
| VS Code | click activity-bar tab | posted click to the window | yes on the **second** click; the first after inactivity was swallowed |
| VS Code | select activity-bar tab | UIA SelectionItem | yes |

Window capture by PrintWindow works for covered windows (phase 1 finding, re-confirmed).

## What this means

Most actions an agent needs can run without the person's mouse or keyboard, but which method works
depends on the kind of app, and one method (UIA Invoke on XAML apps) works but takes the foreground.
So the driver needs a ladder per action, checked live rather than chosen from a list of apps:

1. **Read / observe:** UI Automation (TextPattern for documents). Already background-safe.
2. **Set a field's whole value:** UIA ValuePattern, then `WM_SETTEXT` for Win32 edits.
3. **Type at the caret:** focus the target element through UIA if it is not the thread's focus
   (needed for Chromium), then `WM_CHAR` posted to the target thread's focus window; `EM_REPLACESEL`
   for edit controls. Verify by read-back; real input only if both fail.
4. **Press a control:** UIA Invoke / Toggle / SelectionItem, or `BM_CLICK` for Win32 buttons.
5. **Click a point:** posted mouse messages to the deepest child window under the point (Win32 and
   Chromium; verify, and retry once because Chromium can swallow the first click). Not for UWP
   CoreWindows, so those fall through to the next rung.
6. **Hotkeys:** post the key with the modifiers set in the target thread's key state. Ordinary
   posted modifier messages do not work.
7. **Real input** (SendInput) is last, and only when the person's input mode allows it.

**Focus guard:** after every background attempt, if the target took the foreground (Calculator's
Invoke does), put the previous foreground window straight back, without synthetic input, and record
that the method steals focus for that process so the ladder tries other rungs first next time.

**Not covered here, for later steps:** launching apps (UWP apps always come to the front at launch);
games and DirectX canvases (expected to need real input); browser pages in Chrome itself
(Chromium behaved the same in VS Code, but untested in Chrome); multi-monitor setups.
