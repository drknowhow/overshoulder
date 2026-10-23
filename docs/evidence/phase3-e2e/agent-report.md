**DONE** All six steps ran, and the background drag worked without the person's mouse or keyboard.

1. `status()`: ok, `input_mode` = `never`.
2. `windows()`: ok. Drag test is hwnd 14028152 (`powershell.exe`) and Character Map is hwnd 1970670 (`charmap.exe`). `take_scope(..., read_only=true)` was ok. `act(click, 589, 358)` returned error `narrowed`, limit `read_only`, set_by `session`. `release_scope()` ok.
3. `take_scope("window:1970670", deny_processes=["charmap.exe"])`: error `narrowed`, limit `deny_processes`, set_by `session` ("that window belongs to charmap.exe, which this session may not touch"). No scope was granted, so there was nothing to release.
4. `take_scope("window:14028152", log_text=true, deny_processes=["charmap.exe"])`: ok. Limits were `read_only` false, `no_typing` false, `deny_processes` ["charmap.exe"], and `set_by_person` all empty. The result also had `hidden: ["charmap.exe"]`. A plain `windows()` call no longer listed Character Map. Filtering by process or title also returned nothing.
5. Recording `drag-box`:
   - `act(drag, 589,358 → 829,498, method="background")` was ok: `result.method` = `posted_drag`, `result.background` = true, and the one attempt reported "visible change".
   - The verify block read `ok` true, `changed` true, `pixels_changed` 0.05615, method `posted_drag`, `background` true.
   - `record_stop(narrate=true, gif=false, mlt=true)` was ok.
   - Video: `<test home>\recordings\20261023-164233-drag-box\tutorial\tutorial.mp4`
   - Project: `<test home>\recordings\20261023-164233-drag-box\tutorial\tutorial.mlt`
   - Frames: 49 in the tutorial (55 source frames), with 1 step and 1 narration line.
6. `release_scope()`: ok.

**CAVEAT:** The verify block only says pixels changed (about 5.6%). I did not check that the box landed at (829, 498).
