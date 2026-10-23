"""Narration: one spoken line per tutorial step, timed so the words and the video agree.

A step's line is its caption read aloud, the `narration` text a layer gives it, or the `audio` file a
layer gives it (any TTS, any format ffmpeg reads). Lines are spoken with the Windows voice (SAPI:
built in, offline) unless the caller passes another engine. The renderer starts each line as its
step appears and holds the video on the step until the line has finished, so a line never runs into
the next step; narration.wav, narration.srt and the AAC track in tutorial.mp4 all use those times.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import wave
from collections.abc import Callable
from pathlib import Path

RATE = 22050  # mono 16-bit throughout
GAP_S = 0.25  # silence after a line before the video moves on
LONG_TEXT = 40  # typed text longer than this is not read out character by character
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0  # no console over the person's apps
SPOKEN_KEYS = {"Ctrl": "Control", "Del": "Delete", "Esc": "Escape", "Win": "Windows", "PgUp": "Page Up",
               "PgDn": "Page Down", "Ins": "Insert"}

Engine = Callable[[list[str], Path], list[Path]]  # lines, work dir -> one WAV per line

SAPI_SCRIPT = r"""
param([string]$job)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$spec = Get-Content -Raw -Encoding UTF8 $job | ConvertFrom-Json
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
if ($spec.voice) {
  $v = $s.GetInstalledVoices() | Where-Object { $_.Enabled -and $_.VoiceInfo.Name -like ('*' + $spec.voice + '*') } |
       Select-Object -First 1
  if (-not $v) { throw ('no installed voice matches ' + $spec.voice) }
  $s.SelectVoice($v.VoiceInfo.Name)
}
$s.Rate = [int]$spec.rate
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(22050,
  [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
foreach ($it in $spec.lines) { $s.SetOutputToWaveFile($it.path, $fmt); $s.Speak($it.text) }
$s.SetOutputToNull()
$s.Dispose()
"""


class NarrationError(RuntimeError):
    pass


def spoken(caption: str) -> str:
    """A caption as a sentence to read aloud: 'Press Ctrl+S' -> 'Press Control S.'"""
    text = caption
    m = re.fullmatch(r'Type "(.*)"', text, flags=re.S)
    if m:
        text = "Type the text shown." if len(m.group(1)) > LONG_TEXT else f"Type {m.group(1)}"
    if text.startswith("Press "):
        text = "Press " + " ".join(SPOKEN_KEYS.get(k, k) for k in text[6:].split("+"))
    text = text.replace('"', "").strip()
    return text if text.endswith((".", "!", "?")) else text + "."


def sapi(voice: str | None = None, rate: int = 0) -> Engine:
    """The Windows voice. voice: part of an installed voice's name ("Zira"); rate: -10 to 10."""
    def speak(lines: list[str], work: Path) -> list[Path]:
        paths = [work / f"line-{i:03d}.wav" for i in range(len(lines))]
        (work / "sapi.ps1").write_text(SAPI_SCRIPT, encoding="utf-8")
        (work / "job.json").write_text(json.dumps({
            "voice": voice or "", "rate": max(-10, min(10, int(rate))),
            "lines": [{"text": t, "path": str(p)} for t, p in zip(lines, paths, strict=True)]}), encoding="utf-8")
        run = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                              str(work / "sapi.ps1"), "-job", str(work / "job.json")],
                             capture_output=True, text=True, creationflags=NO_WINDOW, timeout=300)
        if run.returncode != 0:
            raise NarrationError(f"Windows speech failed: {(run.stderr or run.stdout).strip()[-400:]}")
        return paths
    return speak


def command(template: str) -> Engine:
    """Any TTS program: template is a command line with {text_file} (UTF-8 text of one line) and {out}
    (the audio file to write, any format ffmpeg reads). Run through the shell, so only for commands a
    person typed, never one an agent sent."""
    def speak(lines: list[str], work: Path) -> list[Path]:
        paths = []
        for i, line in enumerate(lines):
            text_file, out = work / f"line-{i:03d}.txt", work / f"line-{i:03d}.out.wav"
            text_file.write_text(line, encoding="utf-8")
            run = subprocess.run(template.format(text_file=f'"{text_file}"', out=f'"{out}"'), shell=True,
                                 capture_output=True, text=True, creationflags=NO_WINDOW, timeout=300)
            if run.returncode != 0 or not out.exists():
                raise NarrationError(f"TTS command failed for line {i + 1}: {(run.stderr or run.stdout)[-400:]}")
            paths.append(out)
        return paths
    return speak


def normalise(src: Path, dst: Path) -> Path:
    """Convert any audio file to mono 16-bit RATE Hz WAV."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise NarrationError("ffmpeg not found on PATH")
    run = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-ac", "1", "-ar",
                          str(RATE), "-sample_fmt", "s16", str(dst)], capture_output=True, text=True,
                         creationflags=NO_WINDOW)
    if run.returncode != 0:
        raise NarrationError(f"cannot read audio {src}: {run.stderr.strip()[-300:]}")
    return dst


def pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2 or w.getframerate() != RATE:
            raise NarrationError(f"{path.name} is not mono 16-bit {RATE} Hz")
        return w.readframes(w.getnframes())


def seconds(samples: bytes) -> float:
    return len(samples) / 2 / RATE


def voices() -> list[str]:
    """The enabled Windows voices, by name; empty when there are none or no Windows speech."""
    ps = ("Add-Type -AssemblyName System.Speech; (New-Object System.Speech.Synthesis.SpeechSynthesizer)"
          ".GetInstalledVoices() | Where-Object Enabled | ForEach-Object { $_.VoiceInfo.Name }")
    try:
        run = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True,
                             text=True, creationflags=NO_WINDOW, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [x.strip() for x in run.stdout.splitlines() if x.strip()] if run.returncode == 0 else []


def audio_path(value: str, base: Path, outside: bool = False) -> Path:
    """A layer's audio file: relative paths are inside the recording directory, and nothing outside it
    is read unless `outside` (a person rendering from the CLI) allows it."""
    p = Path(value)
    p = (p if p.is_absolute() else base / p).resolve()
    if not outside and not p.is_relative_to(Path(base).resolve()):
        raise NarrationError(f"audio {value} is outside the recording directory; copy it in, or render from the CLI")
    if not p.is_file():
        raise NarrationError(f"audio file {value} does not exist")
    return p


def prepare(steps: list, edits: dict[int, dict], work: Path, engine: Engine, base: Path,
            outside_audio: bool = False) -> dict[int, tuple[str, bytes]]:
    """(text, speech) for every step that speaks, keyed by step id: the layer's audio file when it has
    one, else the engine speaking the layer's narration text or the caption. A step whose narration is
    "" is silent. The text is what narration.srt shows."""
    work.mkdir(parents=True, exist_ok=True)
    out: dict[int, tuple[str, bytes]] = {}
    to_speak: list[tuple[int, str]] = []
    for s in steps:
        e = edits.get(s.id, {})
        text = e.get("narration")
        if e.get("audio"):
            src = audio_path(e["audio"], base, outside_audio)
            out[s.id] = (text or s.caption, pcm(normalise(src, work / f"step-{s.id}.wav")))
        elif text != "":
            to_speak.append((s.id, text or spoken(s.caption)))
    if to_speak:
        made = engine([t for _, t in to_speak], work)
        for (sid, text), path in zip(to_speak, made, strict=True):
            out[sid] = (text, pcm(normalise(path, work / f"step-{sid}.wav")))
    return out


def write_track(path: Path, placed: list[tuple[float, bytes]], total_s: float) -> None:
    """A WAV of total_s seconds with each clip starting at its time."""
    buf = bytearray(int(total_s * RATE) * 2)
    for start, clip in placed:
        at = int(start * RATE) * 2
        end = min(len(buf), at + len(clip))
        buf[at:end] = clip[:end - at]
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(bytes(buf))


def write_srt(path: Path, cues: list[tuple[float, float, str]]) -> None:
    def ts(t: float) -> str:
        ms = int(round(t * 1000))
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
    blocks = [f"{i}\n{ts(a)} --> {ts(b)}\n{text}\n" for i, (a, b, text) in enumerate(cues, 1)]
    path.write_text("\n".join(blocks), encoding="utf-8")


def mux(video: Path, audio: Path, out: Path) -> None:
    """Copy the video stream and add the audio as AAC."""
    ffmpeg = shutil.which("ffmpeg")
    run = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video), "-i", str(audio),
                          "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
                          "-movflags", "+faststart", str(out)], capture_output=True, text=True,
                         creationflags=NO_WINDOW)
    if run.returncode != 0:
        raise NarrationError(f"could not add the narration track: {run.stderr.strip()[-300:]}")
