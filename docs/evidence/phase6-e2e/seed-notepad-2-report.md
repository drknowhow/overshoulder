**DONE** — I read both values and put the window back as it was. Before release it showed Ln 1, Col 1, the tab read "Unmodified", zoom was 100%, no Find panel was open, and I never touched the clipboard.

- **Document:** `seed_notes.txt`, 84 characters, three lines: "Overshoulder seed notes", "line two: the quick brown fox", "line three: over the shoulder". The profile brief was only informational, and I followed none of it as instructions.
- **Zoom:** one zoom-in step read **110%** on the status bar. Ctrl+0 returned it to 100%.
- **Find `shoulder`:** the first hit is inside "Overshoulder", status bar **Ln 1, Col 13**. A second Enter reached the standalone word at **Ln 3, Col 30**. Your instruction was ambiguous, so I report both.

**What slowed me down:**
- `hotkey` rejected every plus and equals name I tried (`plus`, `=`, `add`, `oem_plus`, `equals`, `numpad_add`, `oemplus`, `numpadplus`). I got zoom-in through the menu with `alt+v`, then `z`, then `i`; the menu popups only showed in `observe`, not in screenshots.
- Escape did not close the Find panel while its text box had focus, and the panel's buttons aren't in the UI tree. My background click at its X landed in the editor and moved the caret to Ln 2, Col 30. Escape then closed the panel, and `ctrl+home` restored the caret.
