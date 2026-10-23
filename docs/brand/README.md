# Overshoulder brand

**Line:** Your agent works in its window. Your desk stays yours.

## Mark

Two pointers on an ink tile. The blue one, behind, is the agent's pointer: it is the same blue
(`AGENT_BLUE`) the watchdog draws over the window an agent is working in. The white one, in front, is
yours. The agent works just behind your shoulder and never takes your cursor.

The mark is defined once, as `MARK_SVG` in [`src/overshoulder/brand.py`](../../src/overshoulder/brand.py).
Every file here is rendered from it, and the settings page embeds it directly.

## Colours

| Token | Hex | Used for |
|---|---|---|
| `INK` | `#111827` | Mark tile, caption strip in tutorials, settings text |
| `AGENT_BLUE` | `#2563eb` | Agent pointer, annotation default, settings accent |
| `MARK_BLUE` | `#3b82f6` | The agent pointer in the mark (lighter, to hold up on ink) |
| `SCOPE_GREEN` | `#22c55e` | Outline around the window an agent is driving |
| `PAUSED_AMBER` | `#f5a623` | Outline while the person is using the PC |
| `STOP_RED` | `#ef4444` | Outline after the panic key or an operator pause |
| `PAPER` | `#f8fafc` | Page background |

## Type

The wordmark is set in [Sora](https://fonts.google.com/specimen/Sora) 700. The product's own
surfaces (settings page, captions) use the system font, so nothing fetches a web font at run time.

## Files

| File | What it is |
|---|---|
| `mark.svg` | The mark, 64 x 64 viewBox |
| `icon-16.png` … `icon-256.png` | The mark as PNG at 16, 32, 48, 64 and 256 px |
| `overshoulder.ico` | Windows icon with all five sizes |
| `header-light.png`, `header-dark.png` | README header (mark + wordmark) on a transparent ground, 2x |
| `social-preview.png` | 1280 x 640 link card, with a frame from the agent-made tutorial |

## Rebuilding

```powershell
python docs\brand\build.py
```

Needs Playwright with Chromium (`pip install playwright`, then `playwright install chromium`),
`ffmpeg` on `PATH`, and a network connection for the Sora font. `tests/test_brand.py` fails if
`mark.svg` no longer matches `brand.MARK_SVG`, which is the cue to rebuild.

GitHub has no API for the repository's social preview, and it offers the setting only on public
repositories: a private repository's Settings > General has no Social preview section. When the
repository goes public, upload `social-preview.png` by hand under Settings > General > Social preview.
