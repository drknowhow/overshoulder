You are testing Overshoulder, a desktop-control MCP server, as an ordinary agent. Use ONLY the
overshoulder tools. The person at this PC is working in another window and has set Overshoulder's input mode
to "never", so work in the background. A window titled "Overshoulder drawn test" is open. Its buttons are
painted pixels, so UI Automation does not see them. Do these in order, and keep a short log of every tool
result that matters:

1. status(): record input_mode.
2. windows(): find the drawn test window's hwnd. take_scope("window:<hwnd>").
3. observe(text=true): record how many elements it lists and every text it read, with ref and source.
4. find(text="Save"), then act(action="click", ref=<that ref>, method="background",
   expect={{"text_appears": "Clicked Save", "area_changed": true}}). Record verify.method, verify.area,
   verify.expect and verify.text_appeared.
5. find(describe='the "Open" button'). Record ref, rect, confirmed, confirmed_by and model. Then
   act(action="click", ref=<that ref>, method="background", expect={{"area_changed": true}}) and record
   whether it asked the person, verify.area and verify.expect.
6. find(describe="the small green square in the bottom-right corner"). Record ref, rect, confirmed and
   confirmed_by. Then act(action="click", ref=<that ref>, method="background"). Record the result or the
   error code and message.
7. find(text="Delete"), then act(action="click", ref=<that ref>, method="background"). Record the result or
   the error code and message.
8. release_scope().

When an action needs the person's approval, the person decides; record what came back. Do not try to work
around refusals; a refusal is a valid result. Finish with a compact report: for each step, ok or the error
code, and the values read back.
