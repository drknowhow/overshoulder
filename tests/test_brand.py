from pathlib import Path
from urllib.parse import unquote

from overshoulder import brand
from overshoulder import settings as st
from overshoulder.settings_page import render_page

BRAND_DIR = Path(__file__).resolve().parents[1] / "docs" / "brand"


def test_committed_mark_matches_the_code():
    committed = (BRAND_DIR / "mark.svg").read_text(encoding="utf-8").strip()
    assert committed == brand.MARK_SVG, (
        "docs/brand/mark.svg is out of date with overshoulder.brand.MARK_SVG; "
        "run python docs/brand/build.py and commit what it writes")


def test_mark_data_uri_round_trips():
    uri = brand.mark_data_uri()
    assert uri.startswith("data:image/svg+xml,")
    assert unquote(uri.removeprefix("data:image/svg+xml,")) == brand.MARK_SVG


def test_settings_page_carries_the_mark_and_brand_colours():
    page = render_page(st.Settings(), "tok", [], "C:/logs", saved=False)
    assert '<link rel="icon" href="data:image/svg+xml,' in page
    assert brand.MARK_SVG in page
    assert f"--accent:{brand.AGENT_BLUE}" in page
    assert "{" not in page.split("<style>")[0]  # every format field was filled
