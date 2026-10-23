"""Overshoulder's name, line, colours and mark: the one place they are defined.

The colours are the product's own: the watchdog's pointer and outlines, the tutorial highlights and
caption strip, and the settings page all read them from here. docs/brand/build.py renders every
brand image (icons, README header, social preview) from MARK_SVG; docs/brand/README.md shows them.
"""

from __future__ import annotations

from urllib.parse import quote

NAME = "Overshoulder"
TAGLINE = "Your agent works in its window. Your desk stays yours."

INK = "#111827"  # caption strip, settings text, the mark's tile
AGENT_BLUE = "#2563eb"  # the agent's pointer, step highlights, arrows; never the person's own cursor
MARK_BLUE = "#3b82f6"  # AGENT_BLUE lifted a step so the pointer in the mark holds up on INK
SCOPE_GREEN = "#22c55e"  # watchdog outline while an agent drives
PAUSED_AMBER = "#f5a623"  # watchdog outline while agents wait for the person to stop using the PC
STOP_RED = "#ef4444"  # watchdog outline after a panic or an operator pause
PAPER = "#f8fafc"  # settings page background

# The mark: the agent's blue pointer looking over the shoulder of the person's white one.
POINTER_PATH = "M0 0 L0 22 L5.2 16.9 L8.6 24.6 L12.2 23 L8.9 15.4 L16 15.4 Z"
MARK_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" role="img" aria-label="Overshoulder">'
    f'<rect width="64" height="64" rx="15" fill="{INK}"/>'
    f'<path d="{POINTER_PATH}" transform="translate(17 10) scale(1.3)" fill="{MARK_BLUE}"/>'
    f'<path d="{POINTER_PATH}" transform="translate(28 21) scale(1.3)" fill="#ffffff" stroke="{INK}" '
    'stroke-width="1.4" stroke-linejoin="round"/>'
    "</svg>"
)


def mark_data_uri() -> str:
    return "data:image/svg+xml," + quote(MARK_SVG)
