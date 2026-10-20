"""Clean recordings of a scope: video without overlays, plus the tracks the renderer needs.

Window scopes are captured with PrintWindow(PW_RENDERFULLCONTENT): only the window's own pixels,
even when something covers it, so nothing outside the scope can leak into a recording. Screen scopes
use ffmpeg gdigrab on the monitor rect. The cursor is not captured; its position is sampled every
frame into frames.jsonl and drawn later by the renderer, along with every other annotation.

Output directory:
  video.mp4      clean capture, constant size
  frames.jsonl   {"i", "t", "bounds": [l, t, r, b], "cursor": [x, y]} per frame (screen pixels)
  meta.json      scope, fps, size, start/end epoch time, lease id, client
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

from overshoulder.driver import windows as w
from overshoulder.driver.windows import Rect

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

PW_RENDERFULLCONTENT = 0x2
DIB_RGB_COLORS = 0
SRCCOPY = 0x00CC0020

user32.GetWindowDC.restype = wt.HDC
user32.GetWindowDC.argtypes = [wt.HWND]
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
user32.PrintWindow.argtypes = [wt.HWND, wt.HDC, wt.UINT]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateCompatibleBitmap.restype = wt.HBITMAP
gdi32.CreateCompatibleBitmap.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wt.HDC]
gdi32.GetDIBits.argtypes = [wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT, ctypes.c_void_p, ctypes.c_void_p, wt.UINT]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


def print_window_bgra(hwnd: int) -> tuple[bytes, Rect] | None:
    """The whole window (GetWindowRect area) as top-down BGRA bytes, or None if it cannot be drawn."""
    outer = w.window_rect(hwnd)
    width, height = outer.width, outer.height
    if width <= 0 or height <= 0:
        return None
    wdc = user32.GetWindowDC(hwnd)
    mdc = gdi32.CreateCompatibleDC(wdc)
    bmp = gdi32.CreateCompatibleBitmap(wdc, width, height)
    old = gdi32.SelectObject(mdc, bmp)
    try:
        if not user32.PrintWindow(hwnd, mdc, PW_RENDERFULLCONTENT):
            return None
        bih = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(width * height * 4)
        if not gdi32.GetDIBits(mdc, bmp, 0, height, buf, ctypes.byref(bih), DIB_RGB_COLORS):
            return None
        return buf.raw, outer
    finally:
        gdi32.SelectObject(mdc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(hwnd, wdc)


def crop_bgra(raw: bytes, outer: Rect, inner: Rect) -> tuple[bytes, int, int]:
    """Crop a BGRA buffer of the outer rect down to the inner rect (both in screen pixels)."""
    from PIL import Image
    img = Image.frombuffer("RGBA", (outer.width, outer.height), raw, "raw", "BGRA", 0, 1)
    box = (inner.left - outer.left, inner.top - outer.top, inner.right - outer.left, inner.bottom - outer.top)
    img = img.crop(box)
    return img.tobytes("raw", "BGRA"), img.width, img.height


def cursor_pos() -> tuple[int, int]:
    p = wt.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("ffmpeg not found on PATH; it is needed for recording")
    return exe


class Recording:
    def __init__(self, scope, out_dir: Path, fps: int = 10, lease_id: str = "", client: str = "",
                 alive=None):
        """alive: callable returning False once the lease ends (release, expiry, panic); recording stops."""
        self.scope = scope
        self.alive = alive or (lambda: True)
        self.dir = out_dir
        self.fps = fps
        self.meta = {"scope": scope.describe(), "fps": fps, "lease": lease_id, "client": client}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self.frames = 0
        self.error: str | None = None

    def start(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        b = self.scope.bounds()
        self.size = (b.width // 2 * 2, b.height // 2 * 2)
        self.meta.update({"size": list(self.size), "start": time.time()})
        if self.scope.kind == "window":
            self._proc = subprocess.Popen(
                [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgra",
                 "-s", f"{self.size[0]}x{self.size[1]}", "-r", str(self.fps), "-i", "-", "-c:v", "libx264",
                 "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", str(self.dir / "video.mp4")],
                stdin=subprocess.PIPE)
        else:
            self._proc = subprocess.Popen(
                [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-f", "gdigrab", "-framerate", str(self.fps),
                 "-draw_mouse", "0", "-offset_x", str(b.left), "-offset_y", str(b.top),
                 "-video_size", f"{self.size[0]}x{self.size[1]}", "-i", "desktop", "-c:v", "libx264",
                 "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", str(self.dir / "video.mp4")],
                stdin=subprocess.PIPE)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        period = 1.0 / self.fps
        t0 = time.time()
        with open(self.dir / "frames.jsonl", "w", encoding="utf-8") as track:
            i = 0
            while not self._stop.is_set():
                if not self.alive():
                    self.error = "lease ended"
                    break
                due = t0 + i * period
                now = time.time()
                if now < due:
                    time.sleep(due - now)
                try:
                    b = self.scope.bounds()
                except Exception as e:  # scope went stale: stop recording, keep what we have
                    self.error = f"scope ended: {e}"
                    break
                if self.scope.kind == "window" and not self._write_window_frame(b):
                    i += 1
                    continue
                track.write(json.dumps({"i": i, "t": round(time.time() - t0, 3),
                                        "bounds": [b.left, b.top, b.right, b.bottom],
                                        "cursor": list(cursor_pos())}) + "\n")
                self.frames += 1
                i += 1

    def _write_window_frame(self, inner: Rect) -> bool:
        from PIL import Image
        got = print_window_bgra(self.scope.binding.hwnd)
        if got is None:
            return False
        raw, outer = got
        data, width, height = crop_bgra(raw, outer, inner.intersect(outer))
        if (width, height) != self.size:
            img = Image.frombuffer("RGBA", (width, height), data, "raw", "BGRA", 0, 1)
            canvas = Image.new("RGBA", self.size, (0, 0, 0, 255))
            canvas.paste(img.crop((0, 0, min(width, self.size[0]), min(height, self.size[1]))), (0, 0))
            data = canvas.tobytes("raw", "BGRA")
        try:
            self._proc.stdin.write(data)
        except (BrokenPipeError, OSError) as e:
            self.error = f"encoder stopped: {e}"
            self._stop.set()
            return False
        return True

    def stop(self) -> dict:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        if self._proc:
            try:
                if self.scope.kind == "window":
                    self._proc.stdin.close()
                else:
                    self._proc.communicate(b"q", timeout=20)
                self._proc.wait(timeout=60)
            except (subprocess.TimeoutExpired, OSError):
                self._proc.kill()
        self.meta.update({"end": time.time(), "frames": self.frames, "error": self.error})
        (self.dir / "meta.json").write_text(json.dumps(self.meta, indent=2), encoding="utf-8")
        return {"dir": str(self.dir), **self.meta}


def collect_actions(log_path: Path, lease_id: str, start: float, end: float) -> list[dict]:
    """The gateway log rows for one lease inside a time window, for the renderer."""
    rows = []
    if not log_path.exists():
        return rows
    for line in log_path.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("event") == "action" and r.get("lease") == lease_id and start - 1 <= r.get("ts", 0) <= end + 1:
            rows.append(r)
    return rows
