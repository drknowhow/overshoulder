**DONE** All four steps ran. Only the coordinate click was refused (`needs_real_input`), and I didn't work around it.

- **Step 1:** `status()` returned ok, `input_mode` = `"never"`.
- **Step 2 (Notepad, hwnd 249041108):** ok.
  - `type` into the Document `e6` used method `replace_selection`, `background: true`. The `text_contains` expectation was met.
  - `hotkey ctrl+s` used `posted_keys`, `background: true`. Its own verify showed no visible change.
  - `read` of the Document: `Typed in the background by an agent (6 * 7 = 42)`.
  - After release, the window title was still `e2e_never.txt - Notepad` with no unsaved marker. That suggests the save worked, but I didn't check the file on disk.
- **Step 3 (Calculator, hwnd 246878222):** ok, except the click.
  - `type 12*12=` with no ref used `posted_chars`, `background: true`. It couldn't read the text back.
  - `CalculatorResults` (`e54`) reads `Display is 144`.
  - `num7Button` (`e87`) has rect `[425,527,521,590]`, so the centre is (473, 558).
  - `click` at (473, 558) failed with `needs_real_input`: "this needs the person's mouse or keyboard, and their input mode is 'never'...".
- **Step 4 (Character Map, hwnd 109973990):** ok.
  - `set_value` on the Edit box `e94` ("Characters to copy :") used `set_text_message`, `background: true`. The readback was `Overshoulder\r`.
  - `click` on `Select` (`e95`) used `button_message`, `background: true`.
  - Final `read` of the Edit box: `!Overshoulder\r`. The extra `!` is the character selected in the grid, which Select inserted at the caret.

Every `act` that succeeded ran in the background. I released all three scopes.
