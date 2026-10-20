"""Overshoulder: a desktop hand for AI agents."""

import sys

__version__ = "0.1.0.dev0"

if sys.platform == "win32":
    # pywinauto sets per-monitor v1 awareness when imported; claim v2 first so UIA rects, capture and
    # SendInput all agree in physical pixels on mixed-DPI setups.
    from overshoulder.driver.display import enable_dpi_awareness

    enable_dpi_awareness()
