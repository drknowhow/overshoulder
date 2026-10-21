# Phase 2 end to end: a headless agent in "never" mode (2026-10-21)

`spike/phase2_e2e_never.py` ran a separate Overshoulder daemon with its own data directory and the input
mode set to "never", then gave a headless Claude agent (`claude -p --model sonnet`, only the
overshoulder MCP tools) the tasks in [prompt.md](prompt.md): type and save in Notepad, compute 12 * 12
in Calculator, try one coordinate click there, and fill in Character Map's text box and press Select.
A sentinel window stood in for the person's own work and held the foreground. Every 50 ms a sampler
recorded the foreground window and the cursor; low-level hooks counted injected and physical input.

## What the sampler saw ([sampler.json](sampler.json))

- 1124 samples over 56.5 s.
- The person's window was in front in every sample; no other window took the foreground.
- The cursor stayed at one position the whole time.
- Injected input events: 0. Physical input events: 0.
- The file on disk afterwards: `Typed in the background by an agent (6 * 7 = 42)`.

## What the agent did ([agent-report.md](agent-report.md), [actions.jsonl](actions.jsonl))

- Notepad: typed with `replace_selection` and saved with `posted_keys`, both in the background.
- Calculator: typed `12*12=` with `posted_chars`; the display read 144. The coordinate click was
  refused with `needs_real_input`, because a posted click cannot reach Calculator and the mode forbids
  the real one.
- Character Map: `set_text_message` for the text box and `button_message` for Select.

An earlier run of the same script used UI Automation on Character Map, because the ladder then tried
UI Automation before window messages. The sampler caught Character Map taking the foreground twice,
for about 0.1 s each time, and the foreground guard put the sentinel back both times. Since that run,
`BM_CLICK` and `WM_SETTEXT` come before UI Automation for native Win32 controls.
