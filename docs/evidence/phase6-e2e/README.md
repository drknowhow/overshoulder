# Phase 6 end to end: seed profiles on the registry, found and installed from the site (2026-10-24)

Two scripts, each with Overshoulder daemons on data directories of their own, so the real setup on this
PC was left alone. Input mode was "never". Every agent was a headless Claude agent
(`claude -p --model sonnet`, only the overshoulder MCP tools), and each script opened and closed its own
app windows.

1. `spike/phase6_seed.py` gave each of Calculator, Notepad, Character Map and Paint two sessions by two
   fresh agents, because an export keeps a control only once two sessions have seen it. It exported
   each profile and ran the registry's own check on it at `apps/<app id>/yepgent.osp`.
2. @yepgent published the four files to
   [drknowhow/overshoulder-profiles](https://github.com/drknowhow/overshoulder-profiles) through pull
   requests: [#1](https://github.com/drknowhow/overshoulder-profiles/pull/1) (Calculator) and
   [#3](https://github.com/drknowhow/overshoulder-profiles/pull/3) (the other three). The checks passed,
   the merges signed each file and the catalog, and the site at
   [overshoulder.yepgent.com](https://overshoulder.yepgent.com) was rebuilt from them.
   [#2](https://github.com/drknowhow/overshoulder-profiles/pull/2) held six bad files, one problem each;
   its check failed with one message per problem and it was closed unmerged.
3. `spike/phase6_e2e.py` started a daemon on an empty data directory and did what a person does on the
   settings page, over the page's own HTTP routes: Load the catalog, Preview, Import. Agent A worked in
   Calculator before the import, agent B after it.

## Seed profiles ([seed-result.json](seed-result.json))

| App | Session | Profile at take_scope | Tool calls | Failed tries | Time | Cost | Read |
|---|---|---|---|---|---|---|---|
| Calculator | 1 | none | 30 | 0 | 42.5 s | $0.31 | 413, 12 |
| Calculator | 2 | revision 3, 18-line brief | 27 | 0 | 36.9 s | $0.28 | 4,359, 169 |
| Notepad | 1 | none | 24 | 1 | 63.0 s | $0.40 | `fox`: line and column |
| Notepad | 2 | revision 3, 10-line brief | 47 | 8 | 115.0 s | $0.74 | 110% zoom, `shoulder`: Ln 1 and Ln 3 |
| Character Map | 1 | none | 25 | 2 | 64.2 s | $0.34 | U+00A9: Copyright Sign |
| Character Map | 2 | revision 3, 15-line brief | 32 | 0 | 71.2 s | $0.41 | U+02C2: Modifier Letter Left Arrowhead |
| Paint | 1 | none | 69 | 1 | 236.6 s | $1.07 | 1341 × 806 px, tool names |
| Paint | 2 | revision 5, 57-line brief | 45 | 1 | 117.1 s | $0.68 | 110% zoom, size from the status bar |

Seven of the eight sessions did their whole task. Paint 2 could not open the Resize and skew dialog in
the background: ctrl+w posted four times opened nothing, and the Image flyout closed when focus went
back to the person's window. It read the image size from the status bar instead and said so. Every
window was closed without a question. The failed tries:

- Notepad 2 tried eight names for the plus key in `ctrl+plus` (`plus`, `=`, `add`, `oem_plus` and four
  more); `hotkey` knew none of them. Fixed after this run: `plus`, `minus`, the punctuation keys and the
  number pad have names now.
- Notepad 1 and Character Map 1 each wrote a how-to with a step `profile` refused (`find`, `read`: a
  how-to step must be an action). Both rewrote it and the second try was stored.
- Character Map 1 invoked Reset, which the default rules put to the person. The script stood in for the
  person and answered No (the one approval box, recorded in seed-result.json).
- Paint 1 asked for a vision ref, which is off on a new install. Paint 2 wrote a `wait_for` condition
  as `{"window_appears": {...}}`, a shape `wait_for` doesn't take.

The exports kept 13 (Calculator), 10 (Notepad), 14 (Character Map) and 17 (Paint) items, 1.1 to 1.8 KB
each, and the registry's check found no problems and nothing to flag in any of them. The files are in
[seeds/](seeds); the signed copies are on the registry.

## Find and install on a clean data directory ([e2e-result.json](e2e-result.json))

- Load: the catalog came from https://overshoulder.yepgent.com, its signature checked with the key this
  Overshoulder ships, and the page listed all four profiles ([page-find.html](page-find.html)).
- Agent A, with no profile of Calculator on this PC, got this line in its take_scope brief, which it
  quoted word for word in [its report](e2e-agent-A-report.md): "A shared profile of this app is on the
  registry at https://overshoulder.yepgent.com, by @yepgent (13 items). The person can install it from
  Find profiles on the settings page; agents can't install profiles." It computed 7 × 8 = 56 in 10 tool
  calls and installed nothing.
- Preview downloaded the Calculator profile, checked it against the catalog's hash and its signature,
  and opened the import page, which said it was reviewed on the registry and showed both agent notes
  ([page-import.html](page-import.html)). Import added 8 items; the 5 others were already in the
  profile A's session had learned. Find profiles then tagged it installed ([page-after.html](page-after.html)).
- Agent B got revision 2, a 16-line brief with the shared how-to for Scientific mode. It found 2^10 =
  1,024 and √81 = 9 in 26 tool calls with no failed tries, and [its report](e2e-agent-B-report.md) quotes
  the how-to and three quirks it used. It says the profile saved a few steps: the path to Scientific
  mode and the lookups for Open Navigation and Clear (`find` answered Clear from the profile).

In both agents' runs some `invoke` calls on Calculator reported `focus: {stolen: true, restored: false,
person_active: true}`: Calculator came forward, and because someone was using the PC at that moment,
the person's window was not put back. This is the UWP limit in the README.

## Files

- [seed-prompts.md](seed-prompts.md), [e2e-prompts.md](e2e-prompts.md): every prompt as sent.
- `seed-<app>-<session>-report.md`, [e2e-agent-A-report.md](e2e-agent-A-report.md),
  [e2e-agent-B-report.md](e2e-agent-B-report.md): each agent's own report.
- [seed-actions.jsonl](seed-actions.jsonl), [e2e-actions.jsonl](e2e-actions.jsonl): the daemons' action
  logs.
- `page-*.html`: the settings pages the person saw, with the form token and the data directory's path
  taken out.
