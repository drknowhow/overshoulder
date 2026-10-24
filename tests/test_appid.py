from overshoulder.appid import ID_RE, display_name, identify, make_id, script_of, slug


def test_slug():
    assert slug("Microsoft Corporation") == "microsoft-corporation"
    assert slug("  Google LLC.  ") == "google-llc"
    assert slug("Ünïcode Ltd") == "n-code-ltd"
    assert slug("x" * 60) == "x" * 40
    assert slug("---") == ""


def test_script_of_names_the_script_a_host_runs():
    assert script_of("powershell.exe", ["powershell", "-NoProfile", "-File", r"tests\drawnapp.ps1"]) == "drawnapp"
    assert script_of("pythonw.exe", ["pythonw", r"C:\x\overshoulder_launch.pyw"]) == "overshoulder-launch"
    assert script_of("python.exe", ["python", "-m", "http.server"]) == "http-server"
    assert script_of("javaw.exe", ["javaw", "-jar", "Tool.jar"]) == "tool"
    assert script_of("python.exe", ["python"]) == ""
    assert script_of("notepad.exe", ["notepad", "a.py"]) == ""


def test_make_id():
    assert make_id("Notepad.exe", "Microsoft", "Microsoft.WindowsNotepad_8wekyb3d8bbwe") \
        == "Microsoft.WindowsNotepad_8wekyb3d8bbwe"
    assert make_id("charmap.exe", "Microsoft Corporation") == "microsoft-corporation.charmap"
    assert make_id("tool.exe") == "unknown.tool"
    assert make_id("pythonw.exe", "Python Software Foundation", script="overshoulder-launch") \
        == "python-software-foundation.pythonw+overshoulder-launch"
    assert make_id("…") == "unknown.app"
    for i in ("microsoft-corporation.charmap", "unknown.app", "a.b+c"):
        assert ID_RE.match(i)


def test_display_name():
    assert display_name("charmap.exe", "Character Map", "Microsoft Windows") == "Character Map"
    assert display_name("x.exe", "", "Product") == "Product"
    assert display_name("x.exe", "d" * 61, "") == "x"
    assert display_name("python.exe", "Python", script="drawnapp") == "drawnapp"


def test_identify_keeps_the_version_out_of_the_id():
    a = identify("Code.exe", publisher="Microsoft Corporation", description="Visual Studio Code", version="1.99.0.0")
    b = identify("Code.exe", publisher="Microsoft Corporation", description="Visual Studio Code", version="1.100.2.0")
    assert a.id == b.id == "microsoft-corporation.code"
    assert (a.version, b.version) == ("1.99.0.0", "1.100.2.0")
    pkg = identify("CalculatorApp.exe", publisher="Microsoft", version="1.0.0.0",
                   package_family="Microsoft.WindowsCalculator_8wekyb3d8bbwe", package_version="11.2508.4.0")
    assert pkg.packaged and pkg.version == "11.2508.4.0"
    assert pkg.describe() == {"id": "Microsoft.WindowsCalculator_8wekyb3d8bbwe", "name": "CalculatorApp",
                              "version": "11.2508.4.0"}
