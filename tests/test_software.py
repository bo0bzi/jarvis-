from friday import llm, software, tools as T
from friday.tools import ToolContext


def test_install_rejects_bad_ids_and_download_rules(monkeypatch):
    ctx = ToolContext()
    monkeypatch.setattr(software, "_winget", lambda: "winget")
    assert software.install_app(ctx, "cpu-z; del /s C:\\").startswith("Error: 'cpu-z; del")  # no shell tricks
    assert software.install_app(ctx, "").startswith("Error")
    assert software.download_file(ctx, "http://example.com/a.exe").startswith("Error: only https")
    assert software.download_file(ctx, "https://localhost/a.exe").startswith("Error: downloads from this PC")
    assert software.download_file(ctx, "https://192.168.0.5/a.exe").startswith("Error: downloads from this PC")


def test_download_saves_and_reports_real_path(tmp_path, monkeypatch):
    class Resp:
        def __init__(self): self.data = b"hello world"
        def read(self, n=-1):
            d, self.data = self.data, b""
            return d
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(software, "downloads_dir", lambda: tmp_path)
    monkeypatch.setattr(software.urllib.request, "urlopen", lambda req, timeout=0: Resp())
    out = software.download_file(ToolContext(), "https://example.com/tool.exe")
    assert "Downloaded 11 bytes" in out and str(tmp_path / "tool.exe") in out and "NOT run" in out
    assert (tmp_path / "tool.exe").read_bytes() == b"hello world"
    out2 = software.download_file(ToolContext(), "https://example.com/tool.exe")  # never overwrites
    assert (tmp_path / "tool (1).exe").exists() and "tool (1).exe" in out2


def test_claims_need_a_matching_tool():
    # the real failure: 'downloaded' / 'opening' said after only web searches
    assert llm.unbacked_claim("I've downloaded CPU-Z from the official site.", ["web_search"]) == "downloaded"
    assert llm.unbacked_claim("The portable version has been downloaded successfully.", ["web_search"]) == "downloaded"
    assert llm.unbacked_claim("Opening the portable version of CPU-Z now.", []) == "opened"
    assert llm.unbacked_claim("I've downloaded it.", ["download_file"]) == ""
    assert llm.unbacked_claim("CPU-Z installed successfully.", ["install_app"]) == ""
    assert llm.unbacked_claim("It is 6:45 AM.", []) == ""


WINGET_TABLE = """Name                 Id                       Version  Match      Source
------------------------------------------------------------------------
CPUID CPU-Z          CPUID.CPU-Z              2.15     Tag: cpu-z winget
VLC media player     VideoLAN.VLC             3.0.21   Moniker: vlc winget
VLC media player     VideoLAN.VLC.Nightly     4.0      Tag: vlc  winget
"""


class _Proc:
    def __init__(self, out="", rc=0): self.stdout, self.stderr, self.returncode = out, "", rc


def test_winget_lookup_and_best_match(monkeypatch):
    monkeypatch.setattr(software, "_winget", lambda: "winget")
    monkeypatch.setattr(software.subprocess, "run", lambda *a, **k: _Proc(WINGET_TABLE))
    assert ("VLC media player", "VideoLAN.VLC") in software.winget_lookup("vlc")
    assert software.best_match("cpu z") == ("CPUID CPU-Z", "CPUID.CPU-Z")      # 'cpu z' finds 'CPU-Z'
    assert software.best_match("vlc media player") == ("VLC media player", "VideoLAN.VLC")
    assert software.best_match("photoshop") is None                            # not confident: let the AI ask


def test_already_installed_is_not_an_error(monkeypatch):
    monkeypatch.setattr(software, "_winget", lambda: "winget")
    out = "Found an existing package already installed. No available upgrade found."
    monkeypatch.setattr(software.subprocess, "run", lambda *a, **k: _Proc(out, 2316632107))  # the real unsigned code
    r = software.install_app(ToolContext(), "CPUID.CPU-Z")
    assert not r.startswith("Error") and "already installed" in r
    monkeypatch.setattr(software.subprocess, "run", lambda *a, **k: _Proc("Failed", 5))
    assert software.install_app(ToolContext(), "CPUID.CPU-Z").startswith("Error: winget could not install")


def test_plain_install_requests_are_routed_and_others_are_not():
    from friday import quick

    assert quick._install_app_name("download and install vlc media player") == "vlc media player"
    assert quick._install_app_name("can you install cpu-z on this machine?") == "cpu-z"
    assert quick._install_app_name("please install notepad++ for me") == "notepad++"
    assert quick._install_app_name("install the skill for invoices") == ""
    assert quick._install_app_name("install github.com/a/b") == ""
    assert quick._install_app_name("download the report") == ""
    assert quick._install_app_name("what is the weather") == ""


def test_vague_search_borrows_previous_topic():
    from friday import webcontext as W

    q = W._query("lookup how to do that and give me a gameplan", "how can we set you up to receive phonecalls from me through quo?")
    assert "quo" in q.lower() and "phonecalls" in q.lower()
    assert W._query("search for the price of a 2026 Yamaha R7", "hello") == "the price of a 2026 Yamaha R7"


def test_software_tools_registered_and_picked():
    from friday import toolpick

    assert {"winget_search", "install_app", "download_file"} <= set(T.REGISTRY)
    names = toolpick.pick("are you able to download and install cpu-z on this machine?")
    assert {"winget_search", "install_app", "download_file"} <= set(names)
    assert "install_app" not in toolpick.pick("can you install a skill for invoices")
