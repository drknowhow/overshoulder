# Overshoulder: plan v3

Status: written 2026-10-20 before any code; phases 0 to 4 were built by 2026-10-23. The README
describes what exists today. This file keeps the reasoning behind it.

## Goal

A standalone product, in its own repo with its own name, brand and style:
a desktop hand for AI agents. Any agent (Claude Code, Codex, Gemini, local
models, scripts) can see, operate, record and annotate whatever is on screen,
in any app, and turn that into tutorial screenshots and videos. Nothing in it
is written for a particular application or a particular agent. Browsers work
too, though browser-native tools stay the better path for web pages.

## Product shape

- **Interfaces:** an MCP server (the main door for agents), a CLI, and a
  Python API, all going through the same gateway.
- **Agent-neutral:** no code, memory or config from any one agent. Clients
  authenticate with their own tokens; approvals go to a pluggable notifier
  (desktop toast by default; phone or webhook adapters optional).
- **Brand and docs:** its own name, logo, colour scheme and site; README
  with install, quick start, the safety model, and a demo tutorial video
  made by the tool itself.
- **Packaging:** `pip`/`uv` install, a Windows installer for the watchdog
  and tray, signed releases. Windows first; the gateway API is written so
  macOS/Linux drivers can be added later.
- **Licence:** MIT.

## Scope: the one unit of permission

Every session declares a **scope** when it takes the driver lease:

- `window` - one top-level window, picked by handle, title pattern or process
  name, then bound to PID + create time + window handle. Covers that window's
  own dialogs and popups (same owner chain). If the window closes or a
  different process takes its handle, the scope ends.
- `screen` - one monitor (`screen:1`, `screen:2`) or all monitors
  (`screen:all`).

The scope decides three things at once:
1. **Where actions may land.** Every click, drag and keystroke target must be
   inside the scope (control bounds or coordinates). Focus is checked before
   each keystroke; if focus left the scope, the action is refused.
2. **What gets recorded.** Window scope records that window's region (and
   follows it if it moves); screen scope records the monitor(s).
3. **What observe returns.** UIA tree, screenshots and OCR are limited to the
   scope, so an agent working in one window never reads the rest of the desktop.

`screen` scope is broader authority than `window`, so it requires an explicit
grant per session (approved by the person at the PC, or pre-authorized for a
named client). `window` scope can be self-granted by an authorized client,
subject to the universal rules below.

## Universal safety rules (independent of app and scope)

| Rule | Why |
|---|---|
| Exclusive driver lease (one owner, TTL, heartbeat, queue, cancel) | One shared mouse, keyboard and focus. Two agents would type into each other's windows. |
| Auto-pause when a human touches mouse or keyboard | Same race, with the person as the other driver. |
| Every action is `observe -> bind -> act once -> verify` | Bind to the target (UIA role/name/AutomationId/bounds, or a coordinate inside scope) and a state hash; refuse if the binding went stale. |
| Coordinate actions need a fresh screenshot and are scope-clamped | Coordinates break after DPI, layout or window moves. |
| Hard denies in every scope: secure desktop (UAC), elevated windows, password fields, credential dialogs, clipboard/secret capture | No grant unlocks these. |
| Consequential actions need one-shot visual approval (screenshot + target + action + expiry) | Detected by generic signals, not app knowledge: button/menu text (send, delete, buy, pay, install, format, submit), file-system dialogs, closing unsaved work. |
| Panic hotkey + "agent driving" banner in a separate watchdog the MCP cannot stop | A hung server must not be able to disable its own kill switch. |
| Per-client auth + Windows-user ACL, not just loopback | Any local process can reach a loopback port. |
| Every action appended to a JSONL log with scope, target and result | Audit, and the source for tutorial rendering. |

Optional, per session: a caller may narrow further (deny-list of process
names, read-only mode, no-typing mode). The tool ships with no built-in app
list.

## Architecture

1. **Gateway (MCP server, ours).** The safety boundary: lease, scope, client
   auth, rules above, approvals, rate limits, log. Every agent talks to this
   and nothing else.
2. **Driver (reused library).** `pywinauto` (BSD-3) over UI Automation for
   find + act, plus Win32 `SendInput` for raw mouse/keys inside scope.
   Evaluate `CursorTouch/Windows-MCP` (MIT, ~9.7k stars) and
   `sbroenne/mcp-windows` (MIT) in phase 0 for code and ideas, always behind
   the gateway, never exposed directly.
3. **Locator chain (generic, no app code).** UIA tree first; then OCR text
   match; then vision-model bounding box with a confidence threshold. Each
   result is a target the gateway can bind and verify.
4. **Watchdog (separate process).** Panic hotkey, banner drawn around the
   active scope (window outline or screen border), human-input detection.
5. **Recorder.** ffmpeg `gdigrab` (window region or monitor) for clean
   capture, plus the JSONL timeline.
6. **Renderer (offline).** Video + timeline -> tutorial: cursor halo, click
   ripples, keystroke captions, zoom-to-target, arrows, boxes, numbered
   callouts, blur for redacted regions; exports MP4, GIF and per-step PNGs.
   Exports a project file other editors can open (Shotcut `.mlt` first),
   and accepts a narration audio track from any TTS.
7. **Annotate API.** Agents (or the person) can also annotate any screenshot
   or recording after the fact: add/edit/remove arrows, boxes, text, step
   numbers, blur, crop and trim, by step id or timestamp. Edits are stored as
   a layer over the clean capture, so they can be changed and re-rendered.

## Vision fallback (no UIA tree: canvas apps, games, some Electron)

- Locate by OCR, then vision model bounding box + confidence; act only above a
  threshold, otherwise ask.
- Verify by before/after screenshot diff in the target region, plus a model
  check of the expected change.

As built in phase 4: Windows OCR (three passes, one with long borders painted
out, because the engine skips labels inside a row of bordered boxes) and a
vision model that is off by default. The model's confidence turned out to be
the same for right and wrong boxes, so there is no threshold: a vision box is
confirmed by OCR text inside it or a UI Automation name at its centre, and
otherwise the person is asked with a picture of the box. Verification compares
pixels around the target, and `text_appears` checks the expected change with
OCR instead of a model.

## Tutorial output

- One action per step, with a pause before and after each click.
- Zoom to the target region, then back out to show context.
- Short numbered captions that match a written step list.
- Narration generated from the timeline, so words and video agree.
- Every tutorial also exports the step list with screenshots as a document.

## Phases

0. **Spike (1 PR).** pywinauto + SendInput against several unrelated windows
   (a Win32 app, a UWP app, an Electron app) to prove nothing is app-specific.
   Read Windows-MCP and mcp-windows; decide reuse vs. write. Measure DPI and
   coordinate behaviour on both monitors.
1. **Smallest useful slice.** Gateway with lease, `window` and `screen` scope,
   scope clamping, hard denies; UIA observe + screenshots; click and sanitized
   typing; post-action verify; watchdog (panic key, scope outline); ffmpeg
   recording of the scope + JSONL timeline; offline render of halos and
   numbered callouts.
2. **Tutorial + annotate.** Zoom, keystroke captions, redaction, annotate API
   with editable layers, GIF/PNG export, narration, step-list document.
   Built 2026-10-23 (README § Tutorials). The Shotcut `.mlt` export followed in phase 3.
3. **Breadth.** Drag, hotkeys, approvals on a phone, per-agent client
   credentials, optional per-session narrowing.
   Built 2026-10-23: drag, approvals on a phone (ntfy and a signed webhook),
   per-session narrowing and the Shotcut `.mlt` export. Hotkeys and per-agent
   client credentials came with the phase 1 gateway.
4. **Locator fallback.** OCR and vision locate + verify.
   Built 2026-10-23: text refs from Windows OCR, vision refs from a local or
   cloud model, the text check under x/y clicks, approval with a picture for
   unconfirmed vision refs, the area check and `text_appears`. Capture of
   windows that are not DPI aware was fixed on the way.
5. **Knowledge.** App profiles built from use and from guides, revised every
   session, packaged and shared; a "Made with Overshoulder" mark on rendered
   output. Built 2026-10-24: the `.osp` format, learning at the end of each
   lease, the brief and method order, anchors by automation id, reuse of
   confirmed vision boxes, agent notes, export and import with the settings
   page and the `.osp` file association, and the mark with its metadata.
6. **Site.** A place to publish and find shared profiles, and how Overshoulder
   is paid for. Built 2026-10-24: signed `.osp` files, the registry at
   drknowhow/overshoulder-profiles (checks on every pull request, merged by
   @yepgent, signed on merge, CC0), the site at overshoulder.yepgent.com, Find
   profiles and Publish on the settings page, and four seed profiles.
   Overshoulder stays free with no paid tier; the mark is on for a new install
   and has a free switch to turn it off, and its metadata stays either way.

Deferred until asked: live overlays, OBS scenes, remote (phone) driving.

## Decided

- Name: Overshoulder. Private repo first, public once it worked.
- Own repo, own brand and style; a tool for any agent.

## Open questions

- `screen` scope: approve per session, or allow standing grants per client?
