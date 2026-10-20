import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")

from overshoulder.driver import display  # noqa: E402


def test_enable_dpi_awareness_is_idempotent():
    assert display.enable_dpi_awareness()
    assert display.enable_dpi_awareness()


def test_monitors_reports_one_primary_with_positive_size():
    mons = display.monitors()
    assert mons, "no monitors enumerated"
    assert sum(m.primary for m in mons) == 1
    assert all(m.width > 0 and m.height > 0 and m.dpi >= 96 for m in mons)
    assert [m.index for m in mons] == list(range(1, len(mons) + 1))
