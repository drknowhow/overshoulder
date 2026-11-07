"""Clean recordings of a scope: video without overlays, plus the tracks the renderer needs.

Window scopes are captured with PrintWindow(PW_RENDERFULLCONTENT): only the window's own pixels,
even when something covers it, so nothing outside the scope can leak into a recording. A window that is
not per-monitor DPI aware is captured at the size it draws at and stretched to its size on screen, as
DWM shows it. Screen scopes
grab the monitor rect, with the windows of any program the session may not see blacked out before the
frame reaches the encoder (gateway.limits). The cursor is not captured; its position is sampled every
frame into frames.jsonl. The renderer draws every annotation later, and by default it draws the
agent's own pointer from the action timeline, since in background mode the real cursor is the
person's.

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

from overshoulder.driver import display
from overshoulder.driver import windows as w
from overshoulder.driver.windows import Rect
from overshoulder.gateway.limits import black_out, capture_rects

# How often a recording looks again for password fields and denied windows to black out. A look takes too
# long to run for every frame, so each frame is held until a look that began after it was grabbed has
# finished, and is blacked out with what that look and the one before the frame found (held_rects).
HIDE_REFRESH_S = 0.3
# A frame held longer than this, because a look hangs on a frozen app, stops the recording.
HOLD_MAX_S = 3.0


def held_rects(before: list[Rect], after: list[Rect], area: Rect) -> list[Rect]:
    """What to black out in a frame grabbed after look `before` finished and before look `after` began.

    The same rects in both: nothing moved. Otherwise a field moved, came or went between the looks, and it
    may have been anywhere in the frame meanwhile (a field that turns back is past both places it was
    seen), so the whole frame is blacked out.
    """
    if set(before) == set(after):
        return list(after)
    return [area]


user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

PW_RENDERFULLCONTENT = 0x2
DIB_RGB_COLORS = 0
SRCCOPY = 0x00CC0020
# The daemon runs under pythonw, so a console program it starts gets a console window of its own,
# which would sit on top of the app being recorded.
NO_WINDOW = subprocess.CREATE_NO_WINDOW

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
    """The whole window (GetWindowRect area) as top-down BGRA bytes, or None if it cannot be drawn.

    PrintWindow gets a window's drawing before DWM stretches it, so a DPI-unaware window on a 125%
    monitor would fill only the top-left 80% of a bitmap of its screen size. It is drawn at its own size
    and scaled to the screen size instead, so every caller gets the window as it looks on screen.
    """
    outer = w.window_rect(hwnd)
    if outer.width <= 0 or outer.height <= 0:
        return None
    width, height = drawn_size(outer.width, outer.height, display.window_scale(hwnd))
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
        return stretch_bgra(buf.raw, (width, height), (outer.width, outer.height)), outer
    finally:
        gdi32.SelectObject(mdc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(hwnd, wdc)


def drawn_size(width: int, height: int, scale: float) -> tuple[int, int]:
    """The size a window draws itself at, from its size on screen and display.window_scale."""
    if abs(scale - 1) < 0.01:
        return width, height
    return max(1, round(width / scale)), max(1, round(height / scale))


def stretch_bgra(raw: bytes, size: tuple[int, int], to: tuple[int, int]) -> bytes:
    """A BGRA buffer resized from `size` to `to`, the way DWM stretches a window that is not DPI aware."""
    if size == to:
        return raw
    from PIL import Image
    img = Image.frombuffer("RGBA", size, raw, "raw", "BGRA", 0, 1).resize(to, Image.Resampling.BILINEAR)
    return img.tobytes("raw", "BGRA")


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
        # Looks for what to black out, (began, ended, rects) oldest first; and frames grabbed but not yet
        # blacked out and sent to the encoder, oldest first. Times are perf_counter: on Windows monotonic()
        # moves in 15.6 ms steps, so a look and a frame would often share a time.
        self._looks: list[tuple[float, float, list[Rect]]] = []
        self._held: list[dict] = []
        self._lock = threading.Lock()
        self._track = None
        self._hide_error: str | None = None

    def _look(self) -> None:
        began = time.perf_counter()
        rects = capture_rects(self.scope)
        with self._lock:
            self._looks.append((began, time.perf_counter(), rects))
            del self._looks[:-64]

    def _refresh_hidden(self) -> None:
        while not self._stop.wait(HIDE_REFRESH_S):
            try:
                self._look()
            except Exception as e:  # can't tell where password fields are: stop rather than show them
                self._hide_error = f"stopped: {e}"
                return

    def start(self) -> None:
        self._look()  # refuses the recording if password fields can't be checked
        self.dir.mkdir(parents=True, exist_ok=True)
        b = self.scope.bounds()
        self.size = (b.width // 2 * 2, b.height // 2 * 2)
        self.meta.update({"size": list(self.size), "start": time.time()})
        # Frames are piped in, window or screen: the recorder sees every frame before the encoder does.
        self._origin = Rect(b.left, b.top, b.left + self.size[0], b.top + self.size[1])
        pix = "bgra" if self.scope.kind == "window" else "rgb24"
        self._proc = subprocess.Popen(
            [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", pix,
             "-s", f"{self.size[0]}x{self.size[1]}", "-r", str(self.fps), "-i", "-", "-c:v", "libx264",
             "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", str(self.dir / "video.mp4")],
            stdin=subprocess.PIPE, creationflags=NO_WINDOW)
        self._track = open(self.dir / "frames.jsonl", "w", encoding="utf-8")
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        threading.Thread(target=self._refresh_hidden, daemon=True, name="overshoulder-record-hide").start()

    def _loop(self) -> None:
        import mss
        period = 1.0 / self.fps
        t0 = time.time()
        grabber = None if self.scope.kind == "window" else (mss.MSS() if hasattr(mss, "MSS") else mss.mss())
        try:
            self._frames(period, t0, grabber)
        finally:
            if grabber is not None:
                grabber.close()

    def _frames(self, period: float, t0: float, grabber) -> None:
        i = 0
        while not self._stop.is_set():
            if not self.alive():
                self.error = "lease ended"
                break
            if self._hide_error:
                self.error = self._hide_error
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
            row = {"i": i, "t": round(time.time() - t0, 3), "bounds": [b.left, b.top, b.right, b.bottom],
                   "cursor": list(cursor_pos())}
            if grabber is None:
                self._write_window_frame(b, row)
            else:
                self._write_screen_frame(grabber, row)
            i += 1
            if not self._flush():
                break

    def _hold(self, began: float, kind: str, data: bytes, size: tuple[int, int], area: Rect,
              row: dict | None) -> bool:
        with self._lock:
            # The last look that had finished before the grab began.
            before = next((r for _, ended, r in reversed(self._looks) if ended < began), self._looks[0][2])
        self._held.append({"ended": time.perf_counter(), "before": before, "kind": kind, "data": data,
                           "size": size, "area": area, "row": row})
        return True

    def _write_window_frame(self, inner: Rect, row: dict | None = None) -> bool:
        """Grab the window and hold the frame until it can be blacked out (_flush)."""
        began = time.perf_counter()
        got = print_window_bgra(self.scope.binding.hwnd)
        if got is None:
            return False
        raw, outer = got
        shown = inner.intersect(outer)
        data, width, height = crop_bgra(raw, outer, shown)
        return self._hold(began, "window", data, (width, height), shown, row)

    def _write_screen_frame(self, grabber, row: dict | None = None) -> bool:
        """Grab the monitor rect and hold the frame until it can be blacked out (_flush)."""
        began = time.perf_counter()
        o = self._origin
        shot = grabber.grab({"left": o.left, "top": o.top, "width": o.width, "height": o.height})
        return self._hold(began, "screen", shot.rgb, tuple(shot.size), o, row)

    def _flush(self) -> bool:
        """Black out and send every held frame that a look began after; False once the recording must stop."""
        while self._held:
            f = self._held[0]
            with self._lock:
                after = next((r for began, _, r in self._looks if began > f["ended"]), None)
            if after is None:
                if time.perf_counter() - f["ended"] > HOLD_MAX_S:
                    self.error = "stopped: password fields were not checked in time"
                    self._held.clear()
                    return False
                return True
            self._held.pop(0)
            if not self._send(f, held_rects(f["before"], after, f["area"])):
                return False
        return True

    def _send(self, f: dict, rects: list[Rect]) -> bool:
        from PIL import Image
        width, height = f["size"]
        if f["kind"] == "screen":
            data = f["data"]
            if rects:
                data = black_out(Image.frombytes("RGB", f["size"], data), f["area"], rects).tobytes()
        else:
            img = Image.frombuffer("RGBA", (width, height), f["data"], "raw", "BGRA", 0, 1).copy()
            black_out(img, f["area"], rects)
            if (width, height) != self.size:
                canvas = Image.new("RGBA", self.size, (0, 0, 0, 255))
                canvas.paste(img.crop((0, 0, min(width, self.size[0]), min(height, self.size[1]))), (0, 0))
                img = canvas
            data = img.tobytes("raw", "BGRA")
        if not self._feed(data):
            return False
        if self._track is not None and f["row"] is not None:
            self._track.write(json.dumps(f["row"]) + "\n")
        self.frames += 1
        return True

    def _feed(self, data: bytes) -> bool:
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
        if self._held:  # the last frames need one more look before they can go out
            try:
                self._look()
                self._flush()
            except Exception as e:
                self.error = self.error or f"stopped: {e}"
            self._held.clear()
        if self._track is not None:
            self._track.close()
        if self._proc:
            try:
                self._proc.stdin.close()
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
