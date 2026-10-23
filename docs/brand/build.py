"""Render Overshoulder's brand images from overshoulder.brand into docs/brand.

Writes mark.svg, icon-<n>.png, overshoulder.ico, header-light.png and header-dark.png (the README
header, on a transparent ground) and social-preview.png (1280 x 640, for link cards). The social
preview shows a frame of the agent-made tutorial in docs/evidence/tutorial-annotate.

Needs Playwright with Chromium (pip install playwright; playwright install chromium), ffmpeg, and a
network connection for the Sora web font. Run from anywhere: python docs/brand/build.py
"""

from __future__ import annotations

import base64
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PIL import Image  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from overshoulder import brand  # noqa: E402

ICON_SIZES = (16, 32, 48, 64, 256)
VIDEO = ROOT / "docs" / "evidence" / "tutorial-annotate" / "tutorial.mp4"
FRAME_AT_S = 12.8  # zoomed in on step 4: the new-tab button, the arrow and its label
FONT = ('<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Sora:wght@600;700&display=swap">'
        "<style>body{margin:0;background:transparent}</style>")


def data_uri(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode()


def frame_png() -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "frame.png"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(FRAME_AT_S), "-i", str(VIDEO), "-frames:v", "1",
                        str(out)], check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return out.read_bytes()


def header_html(ink: str) -> str:
    return (FONT + '<div id="shot" style="display:inline-flex;align-items:center;gap:22px;padding:4px 8px 4px 4px">'
            f'<img src="{brand.mark_data_uri()}" width="96" height="96" alt="">'
            f'<span style="font:700 66px/1 Sora,sans-serif;letter-spacing:-0.01em;color:{ink}">{brand.NAME}</span>'
            "</div>")


def social_html(frame: bytes) -> str:
    return FONT + f"""
<div id="shot" style="width:1280px;height:640px;background:{brand.INK};color:#fff;display:grid;
     grid-template-columns:640px 640px;overflow:hidden;font-family:Sora,sans-serif">
  <div style="padding:0 0 0 84px;display:flex;flex-direction:column;justify-content:center;gap:30px">
    <div style="display:flex;align-items:center;gap:24px">
      <img src="{brand.mark_data_uri()}" width="84" height="84" alt="">
      <span style="font-weight:700;font-size:58px;letter-spacing:-0.01em;line-height:1">{brand.NAME}</span>
    </div>
    <div style="font-weight:600;font-size:31px;line-height:1.35;color:#c7cfdd;max-width:470px">{brand.TAGLINE}</div>
    <div style="font:600 18px 'Segoe UI',sans-serif;letter-spacing:.06em;color:#8b95a7">
      MCP SERVER FOR WINDOWS<br>CLAUDE CODE · CODEX · GEMINI CLI</div>
  </div>
  <div style="align-self:center;margin-right:-80px">
    <img src="{data_uri(frame)}" alt="" style="display:block;width:720px;border-radius:12px;
         border:6px solid {brand.SCOPE_GREEN};box-shadow:0 24px 60px rgba(0,0,0,.45)">
  </div>
</div>"""


def main() -> int:
    (HERE / "mark.svg").write_text(brand.MARK_SVG + "\n", encoding="utf-8")
    with sync_playwright() as p:
        browser = p.chromium.launch()

        def shoot(html: str, out: Path, scale: int = 1) -> None:
            page = browser.new_page(device_scale_factor=scale, viewport={"width": 1400, "height": 800})
            page.set_content(html, wait_until="networkidle")
            page.evaluate("document.fonts.ready")
            page.locator("#shot").screenshot(path=str(out), omit_background=True)
            page.close()

        for n in ICON_SIZES:
            shoot(f'<img id="shot" src="{brand.mark_data_uri()}" width="{n}" height="{n}" style="display:block">',
                  HERE / f"icon-{n}.png")
        shoot(header_html(brand.INK), HERE / "header-light.png", scale=2)
        shoot(header_html("#ffffff"), HERE / "header-dark.png", scale=2)
        shoot(social_html(frame_png()), HERE / "social-preview.png")
        browser.close()

    icons = {n: Image.open(HERE / f"icon-{n}.png").convert("RGBA") for n in ICON_SIZES}
    icons[256].save(HERE / "overshoulder.ico", sizes=[(n, n) for n in ICON_SIZES],
                    append_images=[icons[n] for n in ICON_SIZES if n != 256])
    for f in sorted(HERE.glob("*.*")):
        if f.suffix in (".png", ".ico", ".svg"):
            print(f"{f.name:22} {f.stat().st_size:>8,} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
