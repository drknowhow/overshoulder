import os

import pytest


@pytest.fixture(autouse=True)
def _own_data_folder(tmp_path_factory, monkeypatch):
    """Every test gets its own Overshoulder data folder, so the person's settings (the mark switch, input
    mode, profiles) never change what a test sees. A test that sets OVERSHOULDER_HOME itself still wins."""
    monkeypatch.setenv("OVERSHOULDER_HOME", str(tmp_path_factory.mktemp("home")))


def pytest_collection_modifyitems(config, items):
    if os.environ.get("OVERSHOULDER_DESKTOP_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="desktop test; set OVERSHOULDER_DESKTOP_TESTS=1 on an interactive session")
    for item in items:
        if "desktop" in item.keywords:
            item.add_marker(skip)
