"""Window-switching combos with extra modifiers are refused, and requests that carry a credential
don't follow redirects."""

import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")

from overshoulder import phone, vision  # noqa: E402
from overshoulder.gateway import errors as E  # noqa: E402
from overshoulder.gateway.policy import require_hotkey_allowed  # noqa: E402


@pytest.mark.parametrize("combo", ["alt+tab", "alt+shift+tab", "ctrl+alt+tab", "ctrl+shift+alt+tab",
                                   "alt+esc", "alt+shift+esc", "ctrl+esc", "ctrl+shift+esc", "ctrl+alt+del"])
def test_window_switching_combos_are_refused_with_extra_modifiers(combo):
    with pytest.raises(E.Denied):
        require_hotkey_allowed(combo)


@pytest.mark.parametrize("combo", ["ctrl+tab", "ctrl+shift+tab", "shift+tab", "esc", "alt+f"])
def test_ordinary_combos_still_pass(combo):
    require_hotkey_allowed(combo)


@pytest.fixture
def servers():
    """A server that redirects every request to a second one, which records what reached it."""
    seen = []

    class Sink(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(dict(self.headers))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        do_POST = do_GET

        def log_message(self, *a):
            pass

    sink = ThreadingHTTPServer(("127.0.0.1", 0), Sink)

    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{sink.server_port}/stolen")
            self.end_headers()

        do_POST = do_GET

        def log_message(self, *a):
            pass

    first = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    for s in (sink, first):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{first.server_port}", seen
    for s in (sink, first):
        s.shutdown()


def test_the_vision_key_is_not_sent_on_a_redirect(servers):
    url, seen = servers
    with pytest.raises(vision.VisionError):
        vision._post_json(url, {"x": 1}, headers={"Authorization": "Bearer secret-key"}, timeout=5)
    assert seen == []


def test_the_ntfy_token_is_not_sent_on_a_redirect(servers):
    url, seen = servers
    status, _ = phone._http("POST", url, b"hi", {"Authorization": "Bearer secret-token"}, timeout=5)
    assert status == 302 and seen == []
