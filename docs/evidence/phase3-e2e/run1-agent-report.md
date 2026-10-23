**CAVEAT** Every step ran and every refusal was honored, but the drag in step 5 had no visible effect: the box didn't move.

1. **status:** ok. `input_mode` is `never`.
2. **windows, read-only scope, click:** ok. The hwnds are drag test `5902520` and Character Map `1839242`. The read-only scope was granted, and `act(click, 589, 358)` returned `narrowed`. The limit named was `read_only`, set by `session`. `release_scope` ok.
3. **Character Map scope with `deny_processes=["charmap.exe"]`:** `narrowed`, limit `deny_processes`, set by `session`. No scope was granted, so there was nothing to release.
4. **Drag-test scope with `log_text=true` and `deny_processes=["charmap.exe"]`:**
   - Ok, and the scope's `hidden` list is `["charmap.exe"]`.
   - The limits were `read_only` false, `no_typing` false, `deny_processes` `["charmap.exe"]`, and nothing set by the person.
   - `windows()` does not list Character Map. I also ran `windows(process="charmap.exe")` first, which returned an empty list.
5. **Recording and drag:**
   - `record_start("drag-box")` ok.
   - The drag `(589,358)` to `(829,498)` with `method="background"` returned ok. `result.method` is `posted_drag` and `result.background` is `true`. The verify result is ok with `changed: false`, `pixels_changed: 0.0`, `method: posted_drag`, `background: true`.
   - The note says the app may only take drags from the real mouse. I did not retry with `method="input"`, because input mode is `never`.
   - `record_stop(narrate=true, gif=false, mlt=true)` ok. The tutorial's video is `<test home>\recordings\20261023-162812-drag-box\tutorial\tutorial.mp4`.
   - Its project is `...\tutorial\tutorial.mlt` in the same folder. It has 58 frames, rendered from 81 source frames, with 1 step.
6. **release_scope:** ok.
