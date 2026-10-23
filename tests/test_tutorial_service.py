"""Agents annotate and re-render their own recordings through the service, and nobody else's."""

import json
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")

from overshoulder.gateway.errors import GatewayError  # noqa: E402
from tests.rechelpers import HAVE_FFMPEG, make_recording  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg on PATH")


def own(rec, client):
    meta = json.loads((rec / "meta.json").read_text(encoding="utf-8"))
    (rec / "meta.json").write_text(json.dumps({**meta, "client": client}), encoding="utf-8")
    return rec


@pytest.fixture
def svc(tmp_path):
    from overshoulder.gateway.core import gateway_for_tests
    from overshoulder.service import Service

    s = Service(gateway_for_tests(tmp_path), shots_dir=tmp_path / "shots")
    return s, s.gw.registry.add("alice"), s.gw.registry.add("bob")


@needs_ffmpeg
def test_a_recording_is_reached_by_its_name_and_only_by_its_owner(svc):
    s, alice, bob = svc
    rec = own(make_recording(s.recordings_dir), "alice")
    assert s.own_recording(alice, "rec") == rec
    assert s.own_recording(alice, str(rec)) == rec  # the path record_stop returned works too
    with pytest.raises(GatewayError, match="another client"):
        s.own_recording(bob, "rec")
    with pytest.raises(ValueError, match="no recording"):
        s.own_recording(alice, "..\\..\\shots")  # only the name counts: nothing outside the directory
    with pytest.raises(ValueError, match="no recording"):
        s.own_recording(alice, "")


@needs_ffmpeg
def test_annotate_edits_the_layer_and_lists_the_rendered_steps(svc):
    s, alice, _ = svc
    own(make_recording(s.recordings_dir), "alice")
    first = s.annotate(alice, "rec")
    assert first["items"] == [] and first["steps"] is None and first["size"] == [320, 240]

    box = s.annotate(alice, "rec", "add", item={"kind": "box", "rect": [190, 140, 270, 190], "step": 1,
                                                "text": "Go"})["item"]
    s.annotate(alice, "rec", "add", item={"kind": "step", "step": 2, "caption": "Save the file"})
    s.annotate(alice, "rec", "edit", item_id=box["id"], changes={"text": None, "color": "#dc2626"})
    out = s.render_recording(alice, "rec", gif=False)
    assert out["ok"] and out["steps"] == 2 and out["annotations"] == 2

    listed = s.annotate(alice, "rec")
    assert [x["caption"] for x in listed["steps"]] == ['Click "Go"', "Save the file"]
    assert listed["items"][0] == {"id": box["id"], "kind": "box", "rect": [190, 140, 270, 190], "step": 1,
                                  "color": "#dc2626"}
    assert s.annotate(alice, "rec", "remove", item_id=box["id"])["removed"] is True
    with pytest.raises(ValueError, match="needs an id"):
        s.annotate(alice, "rec", "edit", changes={"text": "x"})
    with pytest.raises(ValueError, match="op must be"):
        s.annotate(alice, "rec", "delete_everything")


@needs_ffmpeg
def test_agents_cannot_pick_another_tts_program_or_audio_from_anywhere(svc):
    s, alice, _ = svc
    own(make_recording(s.recordings_dir), "alice")
    for opt in ({"engine": "calc.exe"}, {"outside_audio": True}, {"out_dir": "C:/"}):
        with pytest.raises(ValueError, match="unknown render options"):
            s.render_recording(alice, "rec", **opt)


@needs_ffmpeg
def test_one_recording_renders_once_at_a_time(svc):
    s, alice, _ = svc
    own(make_recording(s.recordings_dir), "alice")
    s._rendering.add("rec")
    with pytest.raises(ValueError, match="already being rendered"):
        s.render_recording(alice, "rec")
    s._rendering.clear()
    assert s.render_recording(alice, "rec", gif=False)["ok"]
    assert json.loads((s.gw.log.path).read_text(encoding="utf-8").splitlines()[-1])["event"] == "render"
