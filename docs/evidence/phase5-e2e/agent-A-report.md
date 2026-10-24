**DONE** Scientific mode gave 12 × 34 + 5 = 413 and √144 = 12, both read from the "Display is N" text. Calculator is back in Standard mode (the header read "Standard Calculator mode") and the scope is released.

- The profile was empty at the start, so nothing from earlier sessions applied.
- Every action ran in the background with `invoke` or `select`, and none needed the person's real input.
- Switching to Scientific took two steps: invoke "Open Navigation", then select "Scientific Calculator". The pane closed on its own after each mode change.
- The only thing that slowed me down was that button refs are renumbered per mode (Square root was `e36` in Standard and `e95` in Scientific). I had to observe again after switching before pressing anything.
- I left two notes in the profile: a "how" for switching to Scientific mode, and a quirk covering the self-closing pane and the per-mode refs.
