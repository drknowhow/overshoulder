import math
import shutil
import subprocess
import sys
import wave
from array import array

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")

from overshoulder import narrate as nr  # noqa: E402
from overshoulder import render as R  # noqa: E402
from overshoulder.layer import Layer  # noqa: E402
from tests.rechelpers import HAVE_FFMPEG, make_recording  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG or not shutil.which("ffprobe"), reason="needs ffmpeg and ffprobe")


def tone(path, secs, rate=16000):
    """A 440 Hz tone, at another rate than RATE so every clip goes through normalise."""
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(array("h", (int(8000 * math.sin(2 * math.pi * 440 * i / rate))
                                  for i in range(int(secs * rate)))).tobytes())
    return path


class FakeVoice:
    """Speaks every line as `secs` of tone and remembers what it was asked to say."""

    def __init__(self, secs=3.0):
        self.secs, self.heard = secs, []

    def __call__(self, lines, work):
        self.heard += lines
        return [tone(work / f"fake-{i}.wav", self.secs) for i in range(len(lines))]


def cues(path):
    out = []
    for block in path.read_text(encoding="utf-8").strip().split("\n\n"):
        _, times, text = block.split("\n", 2)
        a, b = (sum(float(x) * m for x, m in zip(ts.replace(",", ".").split(":"), (3600, 60, 1), strict=True))
                for ts in times.split(" --> "))
        out.append((a, b, text))
    return out


def audio_codecs(video):
    return subprocess.run([shutil.which("ffprobe"), "-v", "error", "-select_streams", "a", "-show_entries",
                           "stream=codec_name", "-of", "csv=p=0", str(video)],
                          capture_output=True, text=True, check=True).stdout.split()


def loud(samples, a, b):
    """Mean absolute level of the narration track between a and b seconds."""
    seg = array("h", samples[int(a * nr.RATE) * 2:int(b * nr.RATE) * 2])
    return sum(abs(x) for x in seg) / max(1, len(seg))


def test_captions_become_sentences_to_read_aloud():
    assert nr.spoken('Click "Clear"') == "Click Clear."
    assert nr.spoken("Press Ctrl+Shift+S") == "Press Control Shift S."
    assert nr.spoken('Type "hi there"') == "Type hi there."
    assert nr.spoken('Type "' + "x" * 60 + '"') == "Type the text shown."
    assert nr.spoken("Bring the window to the front") == "Bring the window to the front."
    assert nr.spoken("Done!") == "Done!"


@needs_ffmpeg
def test_the_video_holds_each_step_until_its_line_has_ended(tmp_path):
    rec = make_recording(tmp_path)
    plain = R.render(rec, gif=False)
    voice = FakeVoice(3.0)
    out = R.render(rec, narrate=True, engine=voice, gif=False)

    assert voice.heard == ["Click Go.", "Press Control S."]
    (a1, b1, t1), (a2, b2, t2) = cues(rec / "tutorial" / "narration.srt")
    assert (t1, t2) == ("Click Go.", "Press Control S.")
    assert b1 - a1 == pytest.approx(3.0, abs=0.01) and b2 - a2 == pytest.approx(3.0, abs=0.01)
    assert a2 >= b1 + nr.GAP_S - 0.1  # the second line waits for the first, so they never overlap
    assert out["seconds"] >= b2 and out["seconds"] > plain["seconds"] + 2 and out["held_s"] > 2
    assert audio_codecs(out["video"]) == ["aac"]

    track = nr.pcm(rec / "tutorial" / "narration.wav")
    assert nr.seconds(track) == pytest.approx(out["seconds"], abs=0.05)
    assert loud(track, a1 + 0.2, b1 - 0.2) > 1000 and loud(track, a2 + 0.2, b2 - 0.2) > 1000
    assert loud(track, b1 + 0.02, a2 - 0.02) == 0


@needs_ffmpeg
def test_the_layer_rewrites_silences_or_supplies_a_line(tmp_path):
    rec = make_recording(tmp_path)
    layer = Layer.for_recording(rec)
    layer.add({"kind": "step", "step": 1, "narration": "Press the blue Go button."})
    second = layer.add({"kind": "step", "step": 2, "narration": ""})
    voice = FakeVoice(1.0)
    R.render(rec, narrate=True, engine=voice, gif=False)
    assert voice.heard == ["Press the blue Go button."]
    assert [c[2] for c in cues(rec / "tutorial" / "narration.srt")] == ["Press the blue Go button."]

    tone(rec / "clip.wav", 1.5, rate=44100)
    layer.edit(second["id"], {"narration": None, "audio": "clip.wav"})
    voice = FakeVoice(1.0)
    R.render(rec, narrate=True, engine=voice, gif=False)
    assert voice.heard == ["Press the blue Go button."]  # step 2 used its own audio, not the voice
    (_, _, _), (a2, b2, t2) = cues(rec / "tutorial" / "narration.srt")
    assert t2 == "Press Ctrl+S" and b2 - a2 == pytest.approx(1.5, abs=0.01)


@needs_ffmpeg
def test_audio_outside_the_recording_is_read_only_when_a_person_allows_it(tmp_path):
    rec = make_recording(tmp_path)
    outside = tone(tmp_path / "outside.wav", 1.0)
    Layer.for_recording(rec).add({"kind": "step", "step": 1, "audio": str(outside)})
    with pytest.raises(nr.NarrationError, match="outside the recording"):
        R.render(rec, narrate=True, engine=FakeVoice(), gif=False)
    Layer.for_recording(rec).edit("a1", {"audio": "../outside.wav"})
    with pytest.raises(nr.NarrationError, match="outside the recording"):
        R.render(rec, narrate=True, engine=FakeVoice(), gif=False)
    out = R.render(rec, narrate=True, engine=FakeVoice(), gif=False, outside_audio=True)
    assert out["lines"] == 2


@needs_ffmpeg
def test_a_render_without_narration_has_no_voice_and_no_old_narration_files(tmp_path):
    rec = make_recording(tmp_path)
    R.render(rec, narrate=True, engine=FakeVoice(1.0), gif=False)
    assert (rec / "tutorial" / "narration.srt").exists()
    out = R.render(rec, gif=False)
    assert "narration" not in out and audio_codecs(out["video"]) == []
    assert not (rec / "tutorial" / "narration.srt").exists() and not (rec / "tutorial" / "narration.wav").exists()


@needs_ffmpeg
def test_any_tts_command_can_speak_the_lines(tmp_path):
    script = tmp_path / "fake_tts.py"
    script.write_text(
        "import sys, wave\n"
        "text = open(sys.argv[1], encoding='utf-8').read()\n"
        "w = wave.open(sys.argv[2], 'wb'); w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)\n"
        "w.writeframes(b'\\x10\\x10' * 800 * len(text.split())); w.close()\n", encoding="utf-8")
    engine = nr.command(f'"{sys.executable}" "{script}" {{text_file}} {{out}}')
    work = tmp_path / "work"
    work.mkdir()
    made = engine(["one two three", "four"], work)
    lengths = [nr.seconds(nr.pcm(nr.normalise(p, p.with_suffix(".n.wav")))) for p in made]
    assert lengths == [pytest.approx(0.3, abs=0.01), pytest.approx(0.1, abs=0.01)]


@pytest.mark.skipif(not nr.voices(), reason="no Windows voices installed")
def test_the_windows_voice_speaks(tmp_path):
    voice = nr.voices()[0].split()[1]  # "Microsoft Zira Desktop" -> "Zira"
    made = nr.sapi(voice)(["Click Clear.", "Press Control S."], tmp_path)
    secs = [nr.seconds(nr.pcm(p)) for p in made]
    assert all(0.4 < s < 5 for s in secs)
    with pytest.raises(nr.NarrationError, match="no installed voice"):
        nr.sapi("NoSuchVoiceAnywhere")(["Hi."], tmp_path)
