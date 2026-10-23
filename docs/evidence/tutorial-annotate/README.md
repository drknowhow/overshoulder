# Tutorial with zoom, keycaps, a redaction, an arrow, a GIF and narration (2026-10-23)

A headless Claude agent (`claude -p --model sonnet`, only the overshoulder MCP tools, a client of its
own) recorded a Notepad tutorial against the live daemon, following [prompt.md](prompt.md). It worked
in the background while the person's own window covered Notepad. It took the scope, started
recording, redacted the tab strip by ref, typed a note, saved it, opened a second tab, typed in it and
went back to the first. Then it stopped the recording, listed the steps with `annotate`, added an
arrow, a label, a narration line and a caption, and rendered again with `narrate=true, voice="Zira"`.
It needed no second tries and got no refusals ([agent-report.md](agent-report.md)).

## Files

- [tutorial.mp4](tutorial.mp4): 27.4 s, h264 1906x904 with an AAC narration track. The source
  recording was 44.7 s; idle time is cut.
- [tutorial.gif](tutorial.gif): 640 px wide, 274 frames, 457 KB.
- `step-01.png` to `step-07.png`, [steps.md](steps.md), [steps.json](steps.json).
- [narration.srt](narration.srt): the seven spoken lines with their times.
- [annotations.json](annotations.json): the layer the render read. `a1` came from `redact` during
  the recording; `a2` to `a5` from `annotate` afterwards.
- [contact-sheet.png](contact-sheet.png): frames of the final video. The red time labels were added
  for this sheet and are not in the video.

## What the frames show

- Zoom: the camera moves in on the tab strip for step 4 (frames at 12.8 and 13.6 s) and step 7
  (24.2 s), and back out to the whole window in between (15.2 s) and afterwards (26.8 s).
- Keycaps: `Ctrl` `+` `S` for step 3; the typed text appears as a caption while it is typed.
- Redaction: the file name in the tab strip is blurred in every frame checked, including the
  zoomed ones, the step images and the GIF.
- Arrow and label: on step 4 only, pointing at the new-tab button.
- Narration: step 3 says the line from `a4`; the other steps say their captions. The video holds
  still for 1.8 s in total so that no line is cut off.

## Bugs this run found first

An earlier attempt at the same tutorial, before two fixes, found two problems:

- Each ffmpeg that the gateway started (it runs under `pythonw`) opened a console window. One of
  them covered Notepad. Every subprocess now starts with `CREATE_NO_WINDOW`, and
  `tests/test_no_console_windows.py` scans `src` for any call without it.
- `observe` and `screenshot` captured the screen rectangle of the scope, so the agent saw the
  person's window that covered Notepad. A window scope's screenshot now uses that window's own
  pixels and is refused when they can't be read (`tests/test_screenshots.py`).

It also showed that the new Notepad's Find and Replace (Ctrl+H) does not open from posted keys, and
its panel is missing from the scope's UI Automation tree, so this tutorial uses tabs instead.
