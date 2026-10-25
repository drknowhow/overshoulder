<h1 align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/brand/header-dark.png">
    <img src="docs/brand/header-light.png" alt="Overshoulder" width="420">
  </picture>
</h1>

<p align="center"><b>Your agent works in its window. Your desk stays yours.</b></p>

<p align="center">
  <img alt="Platform: Windows 10/11" src="https://img.shields.io/badge/platform-Windows%2010%2F11-2563eb">
  <img alt="Protocol: MCP" src="https://img.shields.io/badge/protocol-MCP-111827">
  <img alt="Status: phase 6" src="https://img.shields.io/badge/status-phase%206-f5a623">
  <img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-22c55e">
</p>

A desktop hand for AI agents on Windows. An agent leases one window (or a whole screen), sees its
controls, clicks and types inside it, and can record what it did as a tutorial with numbered steps.
Any agent that speaks MCP can use it: Claude Code, Codex, Gemini CLI, or a plain script.

![Calculator tutorial made by an agent](docs/demo/calculator-tip/contact-sheet.png)

*An agent recorded this on its own, through MCP: "How to work out a 15% tip on a $48.50 bill in
Windows Calculator". Video, step images and `steps.md` are in [docs/demo/calculator-tip](docs/demo/calculator-tip).*

Agents work on their window in the background, so the person at the desk keeps their own cursor and
keyboard. See [Working beside you](#working-beside-you).

What agents learn about an app goes into its profile, and people share profiles at
[overshoulder.yepgent.com](https://overshoulder.yepgent.com). See [Sharing a profile](#sharing-a-profile).

Status: phase 6. Windows 10/11 only.

## Install

Needs Python 3.11+ and `ffmpeg` on `PATH` (for recording).

```powershell
git clone https://github.com/drknowhow/overshoulder
cd overshoulder
python -m venv .venv
.venv\Scripts\pip install -e .
```

Run Overshoulder from a normal user session. It refuses to start elevated.

## Quick start

Start the gateway and the watchdog, each in its own terminal:

```powershell
overshoulder serve      # MCP at http://127.0.0.1:47621/mcp
overshoulder watchdog   # panic key, human-input pause, scope outline, agent pointer
```

To start both at every login instead (per-user scheduled tasks, not elevated, restarted if they
crash; logs in `%LOCALAPPDATA%\Overshoulder\logs`):

```powershell
overshoulder autostart install     # also: status, uninstall
```

Create a client for each agent. The token is printed once; only its hash is stored.

```powershell
overshoulder client add claude-code
```

Register it with your agent, for example Claude Code:

```powershell
claude mcp add --transport http overshoulder http://127.0.0.1:47621/mcp --header "Authorization: Bearer <token>"
```

Then ask the agent something like "take the Notepad window as your scope and type a shopping list".

## What an agent can do

| Tool | Does |
|---|---|
| `windows` | List windows it could take as a scope |
| `take_scope` | Lease `window:<hwnd>`, `screen:<n>` or `screen:all` |
| `observe`, `find`, `read` | See the scope's controls (with refs like `e12`), filter them, read values; find painted buttons by their text or an icon by a description ([below](#apps-that-paint-their-own-buttons)) |
| `act` | `click`, `double_click`, `right_click`, `drag`, `invoke`, `toggle`, `select`, `set_value`, `type`, `hotkey`, `focus`, `activate`, each verified afterwards |
| `screenshot` | Image of the scope |
| `record_start`, `record_stop` | Record the scope and render a tutorial |
| `redact` | While recording, hide an element in every frame of the tutorial |
| `annotate`, `render` | Edit one of its recordings' annotation layer and render it again |
| `profile` | What earlier sessions learned about the scope's app; add a note for the next agent ([below](#app-profiles)) |
| `release_scope`, `status` | Give the desktop back; see who is driving |

Agents should prefer element refs and `invoke`/`set_value`. Coordinates work as well, for canvas
apps and other apps whose controls Windows can't see. `act` takes `method`. `auto` (the default) works
in the background and uses the person's mouse and keyboard only when nothing else reaches the app and
their settings allow it; `background` never uses them; `input` uses them straight away. Each result
says which method ran and whether it was a background one.

A drag goes from `ref` or `x`/`y` to `to_ref` or `to_x`/`to_y`. Both ends must be in scope, and a drop
onto something that discards or sends (a Recycle Bin, a "Delete" target) asks the person first.

### Apps that paint their own buttons

Some apps draw their controls as pixels (games, canvases, many older apps), so Windows tells an agent
nothing about them. Overshoulder then finds them by how the screen looks:

- `find(text="Save")` reads the scope with the OCR built into Windows and returns where those words
  are as text refs (`t1`, `t2`...). `observe(text=true)` lists all the text it reads. A find by name
  that matches no control looks for the name on screen the same way.
- `find(describe="the gear icon")` asks a vision model where that is and returns a vision ref (`v1`...).
  It is off until you choose a model in `overshoulder settings`: Qwen3-VL 8B in Ollama on this PC
  (about 13 GB of graphics memory while loaded, unloaded after 5 idle minutes), or an
  OpenAI-compatible cloud model, which means screenshots of the agent's window leave the PC.
- A vision ref is confirmed when the text inside its box, or the name Windows gives the control at its
  centre, matches the description. An action on an unconfirmed one asks you first, and the box on the
  desk shows you where the model looked. Your phone gets the words, and the picture only if you share
  screenshots.
- Text and vision refs take clicks and drags. Before each action the pixels under the ref are compared
  with what they were when it was found; if they changed, the ref is stale and has to be found again.
- A click by `x`/`y` is checked against the text on screen at that point, so `x`/`y` on a painted
  Delete asks you, as a click on a Delete button does.
- After an action on a text ref, a vision ref or a point, the result says how much changed around the
  target (`verify.area`). `expect={"text_appears": "Saved"}` checks with OCR that the words showed up
  where they were not before.

All of this reads only the agent's own scope. Reading text can be switched off in the settings, and
then nothing is read.

## App profiles

The first time an agent works in an app, Overshoulder starts a profile of it, and every session after
that revises it. The next agent starts from what the earlier ones learned instead of from nothing.

- **What it learns from use:** the app's windows, the controls agents acted on (their kind and
  automation id; their names once two sessions have seen them), which methods worked for each kind of
  action there and which kept failing, the shortcut keys used, and where a vision model found a painted
  control, with a small hash of its pixels.
- **What agents add:** `profile(op="add")` keeps a quirk, a shortcut, a how-to (its steps as actions
  and control names) or a note from a guide with its link. `profile(op="learn")` looks the app over
  without acting and adds the controls and shortcut keys an agent would use.
- **How it is used:** `take_scope` returns a short brief of the profile, most used items first, and
  `profile()` returns all of it. Methods that kept failing in this app are tried last. A find by name or
  automation id goes straight to a control the profile knows. A description a vision model answered
  before is checked against the screen and reused without asking the model again: 0.3 s instead of
  38 s with the model loading cold, measured on a painted button with Qwen3-VL 8B.
- **What it never holds:** window titles, typed text, what a field showed, file names, or anything an
  agent saw in only one session. A control whose name is the person's content keeps only its kind.
  Notes that hold an email address, a path or a link are refused.
- **What it never does:** grant anything. A profile only changes the order of methods an action may
  already use, and every hint is checked against the screen. Text in a profile reaches agents marked as
  notes from other agents, never as instructions.

Profiles are `.osp` files, one line per item, in `%LOCALAPPDATA%\Overshoulder\profiles`, with their
last 20 revisions in `history\`. `overshoulder profile list` and `overshoulder profile show <app>`
print them; the settings page lists them and has a switch that turns learning and hints off.

### Sharing a profile

- **Export** from the settings page or with `overshoulder profile export <app> [file]`. The copy leaves
  out what only this PC needs: anything seen in one session, the revision history and the bookkeeping.
  An agent can ask to export with `profile(op="export")`; you approve it at the desk first.
- **Import** is yours to do: upload the file on the settings page, or run
  `overshoulder profile import <file>`. The file is checked first (its format, its size and the hash on
  its last line), and the page shows what it brings, including every note other agents wrote, before
  anything is added. An import adds only the items your profile lacks and marks them as imported; what
  your PC learned stays as it is.
- **Find** profiles other people published on [overshoulder.yepgent.com](https://overshoulder.yepgent.com)
  under Find profiles on the settings page, or with `overshoulder profile search [text]`. Nothing goes
  online until you press Load or run the command. The catalog's signature is checked with the key this
  Overshoulder ships, and apps you already have a profile for come first. Preview downloads one profile,
  checks it against the catalog's hash and its own signature, and opens the import page above;
  `overshoulder profile get <app> [--by <login>]` does the same in the terminal.
- **Publish** from the settings page, or with `overshoulder profile publish <app> --as <github name>`. You
  see every line first; it is the same copy Export saves. Then GitHub's new-file page opens on the
  registry with the file filled in at `apps/<app id>/<github name>.osp`, and GitHub makes the pull
  request, where the registry's checks run. A profile too long for a link opens the page with only the
  file's name, and you paste the text. Overshoulder never signs in to GitHub and doesn't keep your
  user name.
- **The registry** is [drknowhow/overshoulder-profiles](https://github.com/drknowhow/overshoulder-profiles).
  Its checks run the export rules on every pull request; then Yep, the agent that looks after the
  registry, reads the file and merges it as @yepgent. The merge signs the file and the catalog with the
  registry's key and rebuilds the site. Profiles there are public domain (CC0 1.0). The import page of a
  signed file says "Reviewed on the registry, published by @&lt;name&gt; in pull request #&lt;n&gt;";
  any other file says it is not from the registry. `overshoulder profile verify <file>` checks the
  signature in the terminal.
- Agents can't install a profile. When an app has none on this PC, the take_scope brief says the
  registry has one, read from the catalog you loaded last. A weekly check for newer revisions of the
  profiles you installed is on the settings page, off until you turn it on.
- `overshoulder profile associate` makes `.osp` files open the import page when you double-click them
  (for your Windows user only; `--remove` takes it back).

## Safety model

The gateway is the only path to the desktop. Every action goes through the same checks, and every
refusal is logged.

- **One driver at a time.** An agent holds a lease with a time limit, and others queue behind it.
- **Scope.** A window scope is tied to that window and the process instance that owns it. Background
  actions go only to the scope's own windows. When real input is used, the click target is checked
  against the window actually under the pointer, and keystrokes are refused if focus has left the
  scope. Taking a whole screen needs a person's approval.
- **Hard denies, in every scope.** Agents can't act on:
  - the secure desktop (UAC prompts, the lock screen)
  - elevated windows
  - password fields, including a focused one when no target is given
  - Win-key combos, Alt+Tab, Alt+Esc and Ctrl+Esc
  - Overshoulder's own windows

  If one of these checks can't tell, the answer is no.
- **Approvals.** Clicking a control named like *Send*, *Delete*, *Buy*, *Pay* or *Install*, pressing
  Delete or Alt+F4, or pressing Enter in a single-line field all need a one-time Yes from the person
  at the desk. No answer within 60 seconds is No. The same question can also go to your phone; see
  [Approvals on your phone](#approvals-on-your-phone).
- **Narrowing.** A session can do less than its scope allows. `read_only` refuses every action but
  observing, screenshots and recording; `no_typing` refuses typing, `set_value` and hotkeys;
  `deny_processes` (for example `chrome.exe`) takes a program's windows out of the scope, so agents
  can't act on them or see them in `windows()`, `observe` or `wait_for`, and screen screenshots and
  recordings show them as black boxes. An agent can ask for these in `take_scope`. You can set them
  for a client, and its agent can't lift them:

  ```bash
  overshoulder client limit claude-code --no-typing --deny-process chrome.exe
  ```

  A refusal comes back as `narrowed`, with the limit and who set it.
- **The person always wins.** While you use the mouse or keyboard, agents keep to background work,
  and anything that needs your real input waits until you've been idle for a few seconds. Touching
  them during such an action stops it. **Ctrl+Alt+P** revokes the lease and pauses until you run
  `overshoulder resume`.
  The watchdog is a separate process, so agents can't stop it. It outlines the active scope and
  shows the agent's own blue pointer where it last acted; neither appears in recordings.
- **Logs.** Every action goes to a JSONL file in `%LOCALAPPDATA%\Overshoulder\logs`. Typed text is
  recorded only as its length, unless the agent took the scope with `log_text=true` (needed for
  tutorial captions).
- **Recordings and screenshots stay in scope.** For a window scope, both capture that window's own
  pixels, even when something covers it, so your other windows never reach the agent.

## Working beside you

Agents act on their window in the background first: UI Automation, then messages sent to the window
itself. Your cursor doesn't move, and the app you're using keeps the keyboard. Some apps respond only
to a real mouse or keyboard, and `overshoulder settings` sets whether an agent may then use yours:

- **Never.** Those actions are refused with `needs_real_input`, and the agent has to ask you.
- **Only while I'm idle** (the default). An agent may borrow them once you've left them alone for a
  few seconds (5 unless you change it). It brings its window forward, acts, then puts your cursor and
  your window back. A menu or dialog it opened stays in front for its next step, for up to 30 seconds.
  If you move while it works, it stops and leaves things as they are.
- **Full control.** An agent may use them whenever it needs to.

In every mode, touching the mouse or keyboard stops an agent that is using them.

What works in the background depends on the app. Overshoulder tries, in order: a Win32 control's own
message (`BM_CLICK`, `WM_SETTEXT`, `EM_REPLACESEL`), UI Automation, then messages posted to the window
(characters, clicks, drags, and hotkeys with their modifiers set in the app's own key state). The measurements
are in [docs/SPIKE-phase2.md](docs/SPIKE-phase2.md). In
[docs/evidence/phase2-e2e-never](docs/evidence/phase2-e2e-never), a headless Claude agent works in
Notepad, Calculator and Character Map with the input mode set to "never". Sampled every 50 ms for
56 seconds, the person's window stayed in front, the cursor did not move, and no input was injected.
[docs/evidence/phase2-borrow](docs/evidence/phase2-borrow) shows a borrowed click that waited for
the person to stop using the PC, then put their cursor and window back. In
[docs/evidence/phase3-e2e](docs/evidence/phase3-e2e), a headless agent narrows its own session, drags
a box in the background while a program it was denied covers the start point, and records the drag as
a tutorial whose Shotcut project renders to match the video. In
[docs/evidence/phase4-e2e](docs/evidence/phase4-e2e), a headless agent works a window of painted
buttons with text refs and vision refs, and its click on a painted Delete is put to the person. In
[docs/evidence/phase5-e2e](docs/evidence/phase5-e2e), three headless agents do Calculator tasks: the
first with no profile, the second with the profile the first one's session built, and the third in a
clean data directory with that profile exported and imported. On Calculator, the profile did not make
the work smaller (29, 26 and 27 tool calls, no failed tries in any run); the later agents followed its
how-to and said its brief matched what they saw. In [docs/evidence/phase6-e2e](docs/evidence/phase6-e2e),
headless agents build profiles of Calculator, Notepad, Character Map and Paint, @yepgent publishes them
on the registry, and a clean data directory finds the Calculator profile on the site, installs it, and
the next agent's brief carries it.

## Waiting for the person

When an agent needs you to do something (sign in, approve, finish a form), it can watch for the
result instead of asking you to say "done". `overshoulder wait` exits 0 as soon as any condition
holds, and 3 on timeout. Hosts that run background commands, such as Claude Code, pick the work
back up automatically. Agents can also use the `wait_for` tool.

```powershell
overshoulder wait --window-appears "Order confirmed"
overshoulder wait --window-closes "Installing" --file "C:\Users\me\Downloads\report.pdf"
overshoulder wait --json '{"kind": "element_appears", "title_re": "Setup", "name": "Finish"}'
```

Detection only reads window titles, on-screen controls and files. `overshoulder settings` opens a
local page where you switch it on or off, choose what agents may watch, limit it to particular apps,
and set how often it checks and how long it may wait.

## Approvals on your phone

When an agent needs your Yes, Overshoulder can ask your phone at the same time as the box on your
desk. The first answer counts and closes the other prompt. No answer before the time runs out (60
seconds unless you change it) is No. Set it up in `overshoulder settings` under "Approvals on your
phone", then press "Send a test question" to check it.

Your PC only makes outgoing requests; nothing on it listens for your phone. The phone is told what
the agent wants to do, the program's name and when the question expires. The window's title and a
screenshot of the window are sent only if you tick those boxes. The log records which channel
answered, as `approved_by`.

**ntfy.** Install the [ntfy](https://ntfy.sh) app and subscribe to the topic the settings page shows,
on the same server: ntfy.sh, or your own with an access token if it needs one. A question arrives as
a notification titled with the action, such as "Allow click on 'Send'?", with Allow and Deny buttons.
Allow then asks again: a second notification, "Confirm click on 'Send'?", has Cancel where Allow was
and a button that names the control, "Confirm Send", beside it. Only that button approves. In
testing, phone taps sometimes landed on the other button, so one stray tap can refuse an action but
cannot approve one (see [Limits](#limits-phase-6)). A button
posts the answer to a second, secret topic. The answer names that question's one-time id and carries
an HMAC under a key that never leaves your PC, so a forged answer, or a copy of an answer to an
earlier question, is ignored. When a question is answered anywhere or expires, its notification is
deleted. Anyone who knows the topics can read the
questions, so keep them to yourself; "Make new topics" replaces them.

**Webhook.** For your own server or another app. Each question is POSTed to the webhook URL:

```json
{"type": "overshoulder.approval", "id": "<one-time id>", "reason": "click on 'Send'",
 "process": "outlook.exe", "expires_at": 1761240000.0, "text": "<what the phone shows>"}
```

with `title` and `screenshot_png_base64` added if you share them. The request carries
`X-Overshoulder-Signature: sha256=<hex>`, an HMAC-SHA256 of the body under the webhook secret from the
settings page. Answer in the response, or later at the answer URL, which Overshoulder polls every two
seconds with `GET <answer URL>?id=<id>` (signed the same way, over the id):

```json
{"id": "<id>", "answer": "yes", "mac": "<hex HMAC-SHA256 of '<id>.yes' under the webhook secret>"}
```

`answer` is `yes` or `no`. Anything else counts as no answer yet. When the question closes,
Overshoulder POSTs `{"type": "overshoulder.approval.closed", "id": "<id>", "outcome": "answered"}`
(or `"closed"` if it was answered elsewhere or expired), signed the same way.

## Tutorials

![Frames of a Notepad tutorial an agent recorded and annotated](docs/evidence/tutorial-annotate/contact-sheet.png)

`record_start` captures a clean video of the scope. `record_stop` renders it into `tutorial/` inside
the recording's folder (`%LOCALAPPDATA%\Overshoulder\recordings\<stamp>-<name>`):

- `tutorial.mp4`: the agent's pointer gliding to each target, click ripples, numbered highlights and
  a caption strip, with the idle time between steps cut. The camera zooms in on small controls and
  holds still while the next steps stay in view; strokes on a canvas (x/y targets) are drawn without
  it moving. Hotkeys show as keycaps, and typed text appears as a caption while it is typed.
- `tutorial.gif`, 640 px wide, for a README or a chat.
- `step-NN.png` for each step (always the whole frame), `steps.md` and `steps.json`.
- With narration: a voice track in the MP4, plus `narration.wav` and `narration.srt`. Each line starts
  with its step's caption. When a line runs longer than its step, the video holds on the frame just
  before the pointer moves on, until the line ends.
- With `mlt`: `tutorial.mlt`, a Shotcut project of the same edit, with its media in `edit/`. See
  [Finishing in Shotcut](#finishing-in-shotcut).

Everything rendered for people carries a small "Made with Overshoulder" mark at the right end of the
caption strip, where it never covers the app. An annotated screenshot gets a thin strip underneath for
it, and `steps.md` ends with a line linking [overshoulder.yepgent.com](https://overshoulder.yepgent.com).
The Tutorials setting "Show Made with Overshoulder" turns the visible mark off; it is on for a new
install, and turning it off costs nothing. The files say it in their metadata either way: the MP4's
comment tag, a PNG text chunk and a GIF comment. Screenshots an agent takes for itself stay clean.

The original video is never changed. Everything added on top comes from `annotations.json` beside it,
which the agent or you can edit before rendering again:

| Kind | Fields | Does |
|---|---|---|
| `box` | `rect`, `text`?, `color`? | Outlined box, optionally labelled |
| `arrow` | `tail`, `head`, `color`? | Arrow pointing at `head` |
| `text` | `at`, `text`, `size`?, `color`? | Label on a dark pill |
| `badge` | `at`, `text`? | Round badge |
| `spotlight` | `rect` | Dims everything outside `rect` |
| `redact` | `rect`, `style`? | `blur` (default), `pixelate` or `solid`, in every output |
| `step` | `step`, `caption`?, `hidden`?, `narration`?, `audio`? | Edits one step: its caption, whether it shows, what it says |
| `cut` | `t0`, `t1` | Drops that part of the recording and the steps in it |
| `crop` | `rect` | Crops every output |

Coordinates are pixels of the recorded frame, the same pixels `steps.json` uses for each step's
`rect`. A drawn item shows during one step (`step`: that step's id), between `t0` and `t1` seconds,
or throughout. Items from the [example](docs/evidence/tutorial-annotate):

```json
{"id": "a1", "kind": "redact", "rect": [56, 12, 363, 52], "style": "blur", "note": "redacted by ref e72"},
{"id": "a2", "kind": "arrow", "step": 4, "head": [387, 55], "tail": [507, 145]},
{"id": "a4", "kind": "step", "step": 3, "narration": "Press Control S to save the first note."}
```

Agents do the same over MCP. `redact(ref)` while recording hides an element's rectangle in every
frame. `annotate(recording, op)` lists the steps and items, or adds, edits or removes items.
`render(recording, ...)` renders again (`zoom`, `keys`, `gif`, `narrate`, `voice`, `pointer`, `mlt`). An
agent can annotate and render only its own recordings, and these two still work after it releases
the scope. From the command line:

```powershell
overshoulder annotate <dir> list
overshoulder annotate <dir> add arrow --step 4 --tail 507,145 --head 387,55
overshoulder annotate <dir> add step --step 3 --narration "Press Control S to save."
overshoulder render <dir> --narrate --voice Zira     # also: --zoom off, --no-keys, --no-gif, --pointer real, --mlt
overshoulder render <dir> --tts-cmd "piper -m en.onnx -f {out} < {text_file}"
overshoulder voices
```

A screenshot takes a layer too: `overshoulder annotate shot.png add box --rect 10,10,200,80` writes
`shot.annotations.json`, and `overshoulder render shot.png` writes `shot.annotated.png`.

In [docs/evidence/tutorial-annotate](docs/evidence/tutorial-annotate), a headless agent records,
redacts, annotates and narrates a Notepad tutorial on its own, working in the background.

### Finishing in Shotcut

`tutorial.mlt` opens in [Shotcut](https://shotcut.org) as the same edit `tutorial.mp4` shows, in parts
you can change:

- **Recording**: clips of `edit/clean.mp4` with the tutorial's cuts. Each zoom is a Size, Position &
  Rotate filter on its clip, and each narration hold is a still (`edit/hold-NN.png`). `clean.mp4` has
  every recorded frame, so a clip can be lengthened. Redactions are already applied to it, so hiding
  the annotation track shows nothing a redaction covers.
- **Annotations**: `edit/overlay.mov`, everything drawn on top (pointer, highlights, badges, keycaps,
  layer items and the caption strip), transparent elsewhere.
- **Made with Overshoulder**: `edit/mark.png`, the mark on a track of its own.
- **Narration**: `narration.wav`, when the tutorial is narrated.
- A marker at each step, named after its caption.

Paths in the project are relative, so the folder can be moved. Rendered with Shotcut's `melt`, the
project matches `tutorial.mp4` frame for frame (on the Notepad example above, every frame is above
0.99 SSIM and 33 dB PSNR). If you save the project in Shotcut, the next render leaves it alone and
writes `tutorial-2.mlt` with its media in `edit-2/`.

## Limits (phase 6)

- Windows only. Multi-monitor and mixed-DPI setups are untested; development used one 125% monitor.
- Chromium and Electron apps take typing only while they are in front (measured on VS Code; Chrome
  itself is untested), so typing into them needs real input: borrowed in "borrow" mode, refused in
  "never" mode. Clicks and UI Automation work on them in the background.
- UWP and XAML apps such as Calculator ignore posted clicks. UI Automation's Invoke on them brings the
  app forward for a moment, and Overshoulder puts the person's window straight back. Typing into them
  does not. The new Notepad's menus (XAML flyouts) do not open in the background.
- Drags in the background work in apps that read the pointer from each message (Win32, WinForms).
  Explorer, list and tree views start drags from the real mouse, and Tk follows the real cursor while
  a button is held, so dragging in them needs real input: borrowed in "borrow" mode, refused in
  "never" mode. A drop into another window always needs real input. One background drag in the phase 3
  end-to-end run moved nothing while someone was using the mouse at the same time; the cause is not
  known ([run 1](docs/evidence/phase3-e2e#earlier-runs)). That drag reported `verify.changed: false`.
- In four of the six live phone runs, the phone sent a different answer from the one the person meant,
  in both directions. In one of them it approved Delete with Allow and Confirm when the plan was to deny
  it. The cause is not known ([phone check](docs/evidence/phase3-phone)). Both later runs passed: one
  asked its questions 10 s apart, and the other, after the titles began naming the action, 1.5 s apart.
  Use phone approvals for actions you could undo, or answer at the desk.
- For apps that hide their controls from UI Automation, OCR reads the languages installed for your
  Windows user. It was measured on two test windows only (the painted-buttons app at 100% and 125%,
  and Character Map). Refs found this way take only mouse actions; to type, click first.
- The vision model's own confidence is not used: measured with Qwen3-VL 8B it was 0.99 for right and
  wrong boxes alike. A cold local model takes about 30 s to load. The cloud option was tested against a
  stand-in server only, not a real provider.
- Screen-scope recordings use desktop capture, so anything on that screen is recorded, except the
  windows of programs the session is denied.
- A redaction is a fixed rectangle of the recorded frame. If the control it covers scrolls or moves
  inside the window, the redaction stays where the control was.
- Narration uses the voices System.Speech can see (`overshoulder voices` lists them; an English
  Windows 11 has David and Zira). For any other engine, render from the CLI with `--tts-cmd`.
- The Shotcut project's annotation track is one video: an annotation can be hidden or trimmed with
  it, but changing what it draws means editing `annotations.json` and rendering again.
- Profiles are keyed by the app's identity (its package or its program's company and name). Two
  programs that report nothing to tell them apart share one profile. A profile describes the app as it
  looked when it was learned; after an update, items not yet seen on the new version are marked so in
  the brief until a session sees them.
- A registry signature says the registry's checks passed and Yep accepted the file, and who published
  it. It does not say the profile's notes are right: they are what agents saw, and the brief gives them
  to agents as information, never as instructions. A file from anywhere else carries only the hash that
  shows it was not changed after export.

## Development

```powershell
pip install -e ".[dev]"
pytest -q                                   # unit tests, CI runs these
$env:OVERSHOULDER_DESKTOP_TESTS = "1"; pytest -q   # plus live tests that open real apps
```

Design notes: [docs/PLAN.md](docs/PLAN.md), [docs/SPIKE-phase0.md](docs/SPIKE-phase0.md),
[docs/SPIKE-phase2.md](docs/SPIKE-phase2.md).
Evidence from live runs: [docs/evidence](docs/evidence).

## License

MIT. See [LICENSE](LICENSE).
