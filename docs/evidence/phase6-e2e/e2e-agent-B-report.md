**DONE** 2^10 = 1,024 and √81 = 9, both read from the display (`Display is 1,024` and `Display is 9`). Calculator is back in Standard mode with `Display is 0`, and the scope is released.

**Profile lines I used:**
- `how h-switch-to-scientific-mode ... invoke Open%20Navigation ; select Scientific%20Calculator`. This gave me the exact two-step path, and it worked as written. I did the same in reverse for Standard.
- `quirk ... Mode switch rebuilds the UI: refs from before (e.g. digits) go stale, so observe again.` I re-observed after each mode switch and had no stale-ref errors.
- `quirk ... Display is read via the name 'Display is N' on CalculatorResults.` I read the display from that element.
- `quirk ... Buttons work with invoke in background.` Every button press was `invoke`.
- `ctl clearbutton ... aid=clearButton`. `find` returned Clear straight from the profile (`"via": "profile"`).

**Steps saved:** a few. I skipped working out the navigation path and the `Open Navigation` and `Clear` lookups. Everything else still needed a fresh `observe`. The profile has no entries for the scientific-only buttons (`powerButton`, `squareRootButton`), so I found those in the observe output.

**Caveats:**
- The final Clear changed nothing, because the mode switch had already reset the display to 0.
- Three `act` results reported `focus.stolen: true`, and two of those had `restored: false` with `person_active: true`. They were the first Open Navigation and the two mode-select calls. All calls were `uia_invoke` or `uia_select` with `background: true`, so I can't say whether your window focus was disturbed.
