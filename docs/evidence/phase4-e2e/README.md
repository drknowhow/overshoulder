# Phase 4 end to end: painted buttons, text refs, vision refs and the area check (2026-10-23)

`spike/phase4_e2e.py` ran a separate Overshoulder daemon with its own data directory. Its settings were
input mode "never", OCR on, and the local vision model (qwen3-vl:8b-instruct in Ollama) on. It opened
`tests/drawnapp.ps1`, a WinForms window whose Save, Open and Delete buttons are painted pixels, then
gave a headless Claude agent (`claude -p --model sonnet`, only the overshoulder MCP tools) the tasks in
[prompt.md](prompt.md). The script played the person at the desk: it screenshotted each approval box
the daemon showed and answered No.

## Run 2 ([result.json](result.json), [agent-report.md](agent-report.md), [actions.jsonl](actions.jsonl))

The agent finished in 80 s.

- `observe(text=true)` listed 7 UI Automation elements and none of the painted buttons. OCR read the
  three button labels, the status line, Apply and the title, plus "o" and "x" from the title bar.
- Save, by text ref: the click ran in the background (`posted_click`). `verify.area` reported 94% of
  the pixels around the button changed (it turns blue when clicked), and `text_appears: "Clicked Save"`
  was met.
- Open, by description: the vision model's box was confirmed by the OCR text "Open" inside it, so the
  click went ahead without a question. 30% of its area changed.
- The green square, by description: nothing on screen confirmed the model's box, so the click was put
  to the person. The box on the desk showed the picture with the square outlined
  ([box-1.png](box-1.png)). Answered No, the agent got `approval_denied`.
- Delete, by text ref: the consequential label asked the person in the standard message box
  ([box-2.png](box-2.png)). Answered No, the agent got `approval_denied`, and the app's own record
  shows Delete was never clicked.

The app recorded two clicks, Save and Open. There were no injected or physical input events during the
run, and the cursor did not move.

## Run 1 ([run1-result.json](run1-result.json), [run1-agent-report.md](run1-agent-report.md))

Run 1 had the same results. The text of the picture box did not wrap, so the long scope line made the window 1210 px
wide ([run1-box-1.png](run1-box-1.png)). The text now wraps to the picture's width. The agent also noted
that `text_appeared` gave only a rect; it now carries the words read as well.
