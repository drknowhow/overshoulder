# Phase 3 end to end: narrowing, a background drag and a Shotcut project (2026-10-23)

`spike/phase3_e2e.py` ran a separate Overshoulder daemon with its own data directory and the input mode
set to "never". It opened the WinForms drag test window (`tests/dragapp.ps1`) and Character Map, then
gave a headless Claude agent (`claude -p --model sonnet`, only the overshoulder MCP tools) the tasks in
[prompt.md](prompt.md). As in phase 2, a sentinel window stood in for the person's work and a sampler
recorded the foreground window and the cursor every 50 ms.

The agent:

1. took the drag window read-only and tried a click, which was refused with `narrowed` (`read_only`);
2. tried to take Character Map with `deny_processes=["charmap.exe"]`, which was refused with `narrowed`
   (`deny_processes`);
3. took the drag window with Character Map denied; `windows()` no longer listed Character Map;
4. recorded a drag of the box with `method="background"` and stopped with `narrate=true, mlt=true`.

## Run 3, the one that passed ([result.json](result.json), [agent-report.md](agent-report.md))

- Character Map was on top of the drag's start point. The drag went to the drag window anyway, in the
  background (`posted_drag`), and the box landed exactly on the drop point.
- 1035 samples over 52 s: the sentinel stayed in front, the cursor did not move, and there were no
  injected or physical input events.
- The agent's `tutorial.mlt` ([copy](tutorial.mlt)) rendered with Shotcut's `melt` matches its
  `tutorial.mp4` on all 49 frames: PSNR at least 45.7 dB, 47.8 dB on average.
  [melt-vs-tutorial.png](melt-vs-tutorial.png) shows four frames, melt on the left.

## Earlier runs

- **Run 2** ([run2-agent-report.md](run2-agent-report.md)) found two bugs, both fixed in 2bdb020. The
  drag was refused with `narrowed` because Character Map covered its start point. The check looked at
  the window on top of the point, but in a window scope the action goes to the scope's own window. With
  the drag refused, the recording had no steps, and rendering it failed.
- **Run 1** ([run1-result.json](run1-result.json), [run1-agent-report.md](run1-agent-report.md)): the
  drag returned `posted_drag` with no visible change. The app saw the press and the release but none of
  the moves with the button down. Someone was using this PC during that run (1770 physical input
  events in 43 s), and the sentinel never held the foreground. The cause is not known. A further 34
  posted drags in the same window landed. They were run directly, through the service, through a
  separate daemon with and without a recording and `deny_processes`, and with zero-distance mouse moves
  injected over the window. Nobody was using the mouse during any of them. The agent saw
  `changed: false` and reported that the box did not move.

Phone approvals are checked against the live gateway instead, because they need the person's phone.
