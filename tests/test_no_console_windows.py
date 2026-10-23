"""The daemon runs under pythonw, where every console program it starts (ffmpeg, PowerShell) gets a
console window of its own unless asked not to. One once sat on top of the window an agent was
recording, in the way of its real-input clicks. So every subprocess call in the package must pass
creationflags; this scans the source rather than a list of known calls, so a new call is covered."""

import ast
from pathlib import Path

SRC = Path(__file__).parents[1] / "src" / "overshoulder"
CALLS = {"Popen", "run", "call", "check_call", "check_output"}


def subprocess_calls(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in CALLS:
            if isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess":
                yield node


def test_every_subprocess_call_hides_its_console_window():
    missing, seen = [], 0
    for path in sorted(SRC.rglob("*.py")):
        for call in subprocess_calls(ast.parse(path.read_text(encoding="utf-8"))):
            seen += 1
            if not any(k.arg == "creationflags" for k in call.keywords):
                missing.append(f"{path.relative_to(SRC)}:{call.lineno}")
    assert seen >= 10  # the scan found the calls at all
    assert not missing, f"subprocess calls without creationflags (add CREATE_NO_WINDOW): {missing}"
