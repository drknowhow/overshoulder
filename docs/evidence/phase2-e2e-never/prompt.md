You are testing Overshoulder, a desktop-control MCP server, as an ordinary agent. Use ONLY the
overshoulder tools. The person at this PC is working in another window and has set Overshoulder's input mode
to "never": agents must work in the background and never use their mouse or keyboard. Three windows are open
behind theirs: a Notepad editing "e2e_never.txt", Calculator, and Character Map. Do these in order, and keep a
short log of every tool result that matters:

1. status(): record input_mode.
2. windows(), then take_scope("window:<hwnd>") on the Notepad whose title contains e2e_never.
   find(control_type="Document"), then act(action="type", ref=<its ref>, text="Typed in the background by an agent (6 * 7 = 42)",
   expect={"text_contains": "Typed in the background"}), then act(action="hotkey", combo="ctrl+s").
   read the Document and record what it shows. release_scope().
3. take_scope on Calculator. act(action="type", text="12*12=") with no ref (the keys go to Calculator itself).
   read the element with auto_id CalculatorResults and record what it shows. Then find(auto_id="num7Button")
   and try act(action="click", x=<the centre x of its rect>, y=<the centre y of its rect>). Record the result
   or the error code exactly. release_scope().
4. take_scope on Character Map. find(control_type="Edit") (the "Characters to copy" box) and
   act(action="set_value", ref=<its ref>, value="Overshoulder"). Then find(name="Select", control_type="Button")
   and act(action="click", ref=<its ref>). read the Edit box and record its value. release_scope().

Every act result has result.method and result.background; record them. Do not try to work around refusals; a
refusal is a valid result. Finish with a compact report: for each step, ok or the error code, the methods
used, and the values read back.
