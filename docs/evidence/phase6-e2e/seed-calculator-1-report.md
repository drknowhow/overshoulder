**DONE** Calculator is back in Standard mode with the display at 0, and the scope is released.

- **Read:** In Scientific mode, 12 × 34 + 5 showed "Display is 413" and √144 showed "Display is 12". Both came from the display element, using button `invoke` only.
- **Cleanup:** No dialog is open. I didn't touch memory, history or the clipboard. The navigation pane closed itself after each mode switch.
- **Profile:** `take_scope` returned no profile for Calculator, so I started one. I added a `how` note for switching to Scientific mode: `invoke` "Open Navigation", then `select` "Scientific Calculator". I also added a quirk note on refs, the pane closing and how to read the display.

**Slowed me down:**
- The first `invoke` on "Open Navigation" reported `focus.stolen: true, restored: false, person_active: true`. It ran in the background, but Calculator may have taken focus from the person's window.
- Control refs change between modes. The digit "One" was `e43` in Standard and `e110` in Scientific, so I had to observe again after switching.
