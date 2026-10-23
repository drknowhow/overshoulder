**CAVEAT**: Four steps behaved as expected. The drag was refused, and the tutorial render failed.

- **1. status():** ok. `input_mode` = `"never"`; nothing active, queue empty.
- **2. read-only scope:** ok. The drag test window is hwnd 6492264 and Character Map is hwnd 1315182. `act(click, 589, 358)` returned `narrowed` with limit `read_only`, set by `session`. `release_scope()` ok.
- **3. Character Map scope with `deny_processes=["charmap.exe"]`:** `narrowed`, limit `deny_processes`, set by `session` ("that window belongs to charmap.exe"). No scope was granted, so there was nothing to release.
- **4. drag-test scope with `log_text=true` and `deny_processes`:** ok. The result lists `hidden: ["charmap.exe"]`, `read_only: false`, `no_typing: false`, `deny_processes: ["charmap.exe"]`, and the person's own limits as all false/empty. Character Map was not listed: `windows(process="charmap.exe")` and `windows(title_re="Character Map")` both returned `[]`. I did not run an unfiltered `windows()` call here.
- **5. record, drag, stop:**
  - **`record_start`:** ok. It wrote to `<test home>\recordings\20261023-163724-drag-box`.
  - **Drag:** `act(drag, method="background")` returned `narrowed`, limit `deny_processes`, set by `session`: "the target belongs to charmap.exe". So there is no `result.method`, `result.background` or verify result, and the box was not moved. The start (589, 358) is outside Character Map's listed rectangle, so I can't explain why the target resolved to `charmap.exe`. I didn't retry.
  - **`record_stop(narrate=true, gif=false, mlt=true)`:** the recording saved with 49 frames and 2 actions, but the tutorial render failed with `bad_request` ("zip() argument 2 is longer than argument 1"). So there is no video, no project and no per-step frames. The raw recording folder above is all that exists.
- **6. release_scope():** ok.

The input mode was "never", so no real mouse or keyboard was used. The only method named in any result was `background`, and the drag that requested it was refused.
