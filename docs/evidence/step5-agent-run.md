# Step 5 evidence: a headless Claude agent drives three apps over MCP (2026-10-20)

Daemon at a1bb044. Agent: `claude -p --model sonnet` with only the overshoulder MCP tools. Prompt: `prompt.md` below.

## Prompt

You are testing Overshoulder, a desktop-control MCP server, as an ordinary agent. Use ONLY the overshoulder tools. Three windows are open: Calculator (currently focused), a Notepad editing "e2e_notes.txt", and VS Code editing "e2e.py". Do these in order and keep a short log of every tool result that matters:

1. Call windows() and pick the Notepad window whose title contains e2e_notes. take_scope("window:<its hwnd>").
2. WITHOUT clicking anything first (Calculator still has keyboard focus), call act(action="type", text="this must be refused"). It should be refused as out_of_scope. Record the exact error code.
3. Now find the Notepad text area (control_type "Document"), act(action="type", ref=<its ref>, text="Overshoulder was here (1 + 2) {ok}", expect={"text_contains": "Overshoulder was here"}), then act(action="hotkey", combo="ctrl+s"). release_scope().
4. Take the Calculator window as scope. Compute 12 * 12 using find(auto_id=...) and act(action="invoke") on: clearButton, num1Button, num2Button, multiplyButton, num1Button, num2Button, equalButton. Read CalculatorResults and record what it shows. release_scope().
5. Take the VS Code window (title contains e2e.py) as scope. observe() to get its bounds, click the middle of the editor area with act(action="click", x=..., y=...), then act(action="type", text="print('typed by an agent through Overshoulder')") and act(action="hotkey", combo="ctrl+s"). release_scope().
6. Try act(action="hotkey", combo="win+r") while holding any scope and record the refusal code. Then release.

Finish with a compact report: for each step, ok or the error code, and the Calculator result. Do not try to work around refusals.

## Agent report (verbatim)

**CAVEAT** All steps ended as specified, but step 5 needed two actions beyond the script because VS Code was not showing `e2e.py`.

- **1:** ok. Notepad hwnd 296816228, scope taken.
- **2:** refused with `out_of_scope` ("foreground window 0x3470cc0 is not in the window scope").
- **3:** ok. The type readback was `Overshoulder was here (1 + 2) {ok}` and `expect` was met. `ctrl+s` returned ok, but I couldn't independently confirm the save: the Notepad title didn't change and the verify said `changed: false`.
- **4:** ok. Calculator showed 144, read from `CalculatorResults` as "Display is 144".
- **5:** ok, after two extra actions:
  - The only VS Code window was titled "Release Notes: 1.142.0", with `e2e.py` as a background tab. Notepad and Calculator were also drawn over it in the scope screenshot.
  - I ran `act(action="activate")` and clicked the `e2e.py` tab (`e188`) before the editor click at (997, 595).
  - Result: `e2e.py` line 1 reads `print('typed by an agent through Overshoulder')`, and the tab shows no unsaved marker after `ctrl+s`.
- **6:** refused with `denied` ("hotkey 'win+r' uses the Windows key, which acts outside any scope"). I took a fresh VS Code scope for this since step 5 had released the first one, then released again.

Without the extra actions, the editor click would likely have landed on the Notepad window drawn over VS Code. Coordinate clicks inside a scope's bounds aren't checked against what is on top at that point.

## Files on disk afterwards

- e2e_notes.txt: `Overshoulder was here (1 + 2) {ok}`
- e2e.py: `print('typed by an agent through Overshoulder')`
