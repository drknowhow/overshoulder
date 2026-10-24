# Phase 5 end to end: an app profile from use, shared and used by another agent (2026-10-24)

`spike/phase5_e2e.py` ran Overshoulder daemons with their own data directories, so the real setup on this
PC was left alone. Input mode was "never", so nothing could touch the person's mouse or keyboard. For
each agent the script opened a new Calculator window and gave a headless Claude agent
(`claude -p --model sonnet`, only the overshoulder MCP tools) the task in [prompts.md](prompts.md):
switch to Scientific mode, do two calculations with the buttons, read the results, and switch back to
Standard mode.

- **A** worked in data directory 1, where Calculator had no profile. At the end it left a how-to for
  switching modes and a quirk with `profile(op="add")`, as its prompt asked.
- **B**, a fresh agent with a related task, worked in data directory 1 after A. `take_scope` gave it
  the profile's brief.
- The profile was then exported with `overshoulder profile export` and imported into an empty data
  directory 2 with `overshoulder profile import`.
- **C**, a third agent with a related task, worked in data directory 2 with only the imported profile.

## Results ([result.json](result.json))

| Agent | Profile at take_scope | Tool calls | Failed tries | Turns | Time | Cost | Results read |
|---|---|---|---|---|---|---|---|
| A | none | 29 (2 of them `profile` adds) | 0 | 30 | 39.8 s | $0.29 | 413, 12 |
| B | revision 3, 18-line brief | 26 | 0 | 27 | 60.2 s | $0.33 | 4,359, 169 |
| C | imported, 14-line brief | 27 | 0 | 28 | 35.6 s | $0.28 | 1,041, 13 |

An earlier run with the same tasks had the same calls (29, 26, 27) and no failed tries, with times of
49.7 s, 44.6 s and 64.1 s. Its evidence was not saved because the script crashed while printing B's
report (a minus sign the Windows console could not encode).

All six results were right. There were no approval boxes, no injected input events and no physical
input events during any run.

**On Calculator, the profile did not make the work smaller.** A made no failed tries, so there was
nothing for B and C to avoid. All three used `observe` twice and `find` three times, and pressed the
buttons by the refs `observe` gave them. Their finds were by a name pattern (`name_re`), which walks the
window by design, or by the automation id of the display header. No agent had acted on the header, so
the profile did not know it. No find was answered from the profile. The times vary more between the two
runs than between the agents.

What the profile did do:

- B and C both said the brief matched what they saw and that they followed it. B: "The profile brief
  from earlier sessions was accurate and I followed it."
- The how-to A wrote was followed by B as well: its `used` count is 2 in
  [profile-after-B.osp](profile-after-B.osp).
- The profile learned 18 controls, 17 of them with automation ids, and that `invoke` worked on every
  button (29 of 29) and `select` on the mode list (4 of 4).

The test that would show a difference is an app where a first agent fails before it finds what works,
such as painted controls a vision model has to find (measured in phase 5 step 4: a second describe of
the same button took 0.32 s instead of 38 s) or a method that keeps failing.

## Sharing ([calculator.osp](calculator.osp))

The export kept 13 items. It left out 10 controls seen in one session only and 4 revisions of history.
It also dropped the bookkeeping: which session saw an item last, name hashes and the program's file
name. The import added the same 13 items, each marked `src=imported`, and printed the agent notes for
the person to read before saying yes. After C, data directory 2's profile had C's own use on top of the
imported items ([profile-after-C.osp](profile-after-C.osp)).

## Files

- [result.json](result.json): the numbers above, per run.
- [agent-A-report.md](agent-A-report.md), [agent-B-report.md](agent-B-report.md),
  [agent-C-report.md](agent-C-report.md): each agent's own report.
- [profile-after-A.osp](profile-after-A.osp), [profile-after-B.osp](profile-after-B.osp),
  [profile-after-C.osp](profile-after-C.osp): the profiles as the daemons saved them.
- [actions-home1.jsonl](actions-home1.jsonl), [actions-home2.jsonl](actions-home2.jsonl): the daemons'
  action logs.
