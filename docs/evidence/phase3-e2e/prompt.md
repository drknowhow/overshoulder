You are testing Overshoulder, a desktop-control MCP server, as an ordinary agent. Use ONLY the
overshoulder tools. The person at this PC is working in another window and has set Overshoulder's input mode
to "never": agents must work in the background and never use their mouse or keyboard. Two windows are open
behind theirs: "Overshoulder drag test", a window with one blue box you can drag (the box's centre is at
screen pixel (589, 358); the drop point is (829, 498)), and Character Map. Do these in order, and keep a
short log of every tool result that matters:

1. status(): record input_mode.
2. windows(): note both windows' hwnds. take_scope("window:<drag test hwnd>", read_only=true), then try
   act(action="click", x=589, y=358). Record the error code and the limit it names. release_scope().
3. take_scope("window:<Character Map hwnd>", deny_processes=["charmap.exe"]). Record the result or the
   error code. If it gave you a scope, release_scope().
4. take_scope("window:<drag test hwnd>", log_text=true, deny_processes=["charmap.exe"]). Record the limits
   in the result. Call windows() and record whether Character Map is listed.
5. record_start(name="drag-box"). act(action="drag", x=589, y=358, to_x=829, to_y=498,
   method="background"); record result.method, result.background and the verify result. Then
   record_stop(narrate=true, gif=false, mlt=true) and record the tutorial's video, project and frames.
6. release_scope().

Do not try to work around refusals; a refusal is a valid result. Finish with a compact report: for each step,
ok or the error code, the methods used, and the values read back.
