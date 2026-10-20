You are making a short screen tutorial with Overshoulder, using ONLY the overshoulder tools. Windows Calculator is open.

Tutorial topic: "How to work out a 15% tip on a $48.50 bill in Windows Calculator."

1. windows(title_re="^Calculator$"), then take_scope("window:<hwnd>", log_text=true).
2. act(action="activate") so Calculator is in front, then record_start(fps=10, name="calculator-tip").
3. Do the calculation slowly and clearly, one button per act call, using find(auto_id=...) then act(action="invoke", ref=...):
   clearButton, num4Button, num8Button, decimalSeparatorButton, num5Button, num0Button, multiplyButton,
   num1Button, num5Button, percentButton, equalButton.
   (In Windows Calculator, "48.50 x 15 %" turns 15 into 15% of 48.50, and = shows the tip.)
4. read the element with auto_id CalculatorResults and check the tip is 7.275.
5. record_stop(render=true). Report the tutorial paths it returns (video, markdown, number of steps and images).
6. release_scope().

If anything is refused, report the error code instead of working around it. Finish with a compact report.
