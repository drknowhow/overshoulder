import os

import pytest


def pytest_collection_modifyitems(config, items):
    if os.environ.get("OVERSHOULDER_DESKTOP_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="desktop test; set OVERSHOULDER_DESKTOP_TESTS=1 on an interactive session")
    for item in items:
        if "desktop" in item.keywords:
            item.add_marker(skip)
