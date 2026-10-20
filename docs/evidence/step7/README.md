# Step 7 evidence: recorder + renderer v0 (2026-10-20)

Recorded by `tests/test_desktop_record.py` (in-process gateway, real Notepad), rendered with
`overshoulder render`. Files here: `tutorial.mp4`, `step-01..04.png`, `steps.md` and a 2x2
`video-contact-sheet.png` of frames 13/22/30/40.

- Video is a clean PrintWindow capture of the scope window; the cursor, click ripple, numbered
  badge, highlight and caption are all drawn afterwards from `frames.jsonl` + the action log.
- Each step's annotations end before the next step starts, and each still shows only its own step
  (the first render showed step 3's caption on step 2's still; fixed and re-rendered).
- `occlusion-last-frame.png`: Notepad recorded while Calculator was opened on top of it (Notepad's
  greyed title-bar buttons show it had lost focus). No Calculator pixels appear: window-scope
  recordings contain only the scope window.
- Typed text appears in captions only because the lease used `log_text=true`; otherwise captions say
  "Type N characters".
