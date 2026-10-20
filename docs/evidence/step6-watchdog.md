# Step 6 evidence: the watchdog (2026-10-20)

Daemon (`overshoulder serve`) and watchdog (`overshoulder watchdog`) running as separate processes;
agent calls made over MCP with the `claude-headless` client token. Scripts: `spike/panic_e2e.py`,
`spike/overlay_check.py`.

## Panic hotkey stops an action in progress

An agent typed 540 characters into Notepad; Ctrl+Alt+P was injected 2.5 s in.

```
type returned after 2.4s: {"ok": false, "error": {"code": "keys_held", "message": "alt+ctrl is held down; wait until it is released"}}
status after: {"paused": "panic hotkey ctrl+alt+p; an operator must resume", "active": null, "queue": []}
act after panic: {"ok": false, "error": {"code": "lease_revoked", "message": "lease revoked: panic hotkey ctrl+alt+p"}}
```

Gateway log:

```
1792505313.044 action   type  ok=False keys_held
1792505313.060 lease_revoked        panic hotkey ctrl+alt+p
1792505330.933 resumed              (operator: overshoulder resume)
```

125 of the 540 characters reached Notepad before typing stopped. Typing stopped on the held-modifier
guard (the injected Ctrl+Alt were down at that instant); the lease was revoked 16 ms later, and every
later action was refused with `lease_revoked` until an operator ran `overshoulder resume`. A panic
cannot be undone by the watchdog's automatic resume (unit-tested: `test_never_resumes_a_panic`,
`test_automatic_resume_cannot_undo_a_panic`).

## Scope outline

While a window scope was held, the watchdog showed one window:

```
{"transparent": true, "topmost": true, "affinity": "0x11", "rect": [2, 35, 1918, 878]}   scope bounds [7, 40, 1913, 873]
```

- 5 px outside the scope on every side, click-through (`WS_EX_TRANSPARENT`), topmost.
- Display affinity `WDA_EXCLUDEFROMCAPTURE` (0x11): it does not appear in screenshots or recordings,
  so it was checked by properties rather than by eye.
- Hidden again after `release_scope`.
- A click by element ref and typing through the daemon succeeded with the outline up, so the
  outline does not intercept the gateway's `WindowFromPoint` hit-test.

## Not verified live

Auto-pause on *physical* mouse/keyboard input needs a person at the desk: injected input is ignored
by design, so it cannot be scripted. The decision logic is unit-tested (`tests/test_watchdog.py`).
