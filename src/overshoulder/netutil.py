"""HTTP for requests that carry a credential (the vision API key, the ntfy token, the webhook secret).

urllib follows a redirect with the same headers, Authorization included, to whatever host the redirect
names. These requests go only to the address in the person's settings: a redirect is refused.
"""

from __future__ import annotations

import urllib.error
import urllib.request


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, f"redirect to {newurl} refused: Overshoulder sends "
                                     "this request only to the address in its settings", headers, fp)


_OPENER = urllib.request.build_opener(_RefuseRedirects)


def urlopen(req: urllib.request.Request, timeout: float):
    """urllib.request.urlopen, except that a redirect is an HTTPError instead of being followed."""
    return _OPENER.open(req, timeout=timeout)
