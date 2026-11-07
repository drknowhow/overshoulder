"""The agent box: a locked-down WSL2 distro where an agent with a shell can reach the desktop only through
the gateway's /mcp endpoint, with a client token of its own.

An agent that runs in the person's Windows account can go around the gateway (README, "What this does not
protect against"). In the box it has no Windows drives, can't start Windows programs, isn't root, can't
reach the Windows clipboard or other distros' services, and its network is limited to DNS, the gateway's
port on the Windows host and the public internet.

`create` builds the box, or repairs one that exists:

1. download the Debian image named in Microsoft's DistributionInfo.json, check its SHA-256, and import it
   as its own distro;
2. as root: write /etc/wsl.conf (no interop, no automount, default user `agent`), the boot and egress
   scripts, install iptables, create `agent` (uid 4242, no sudo, no password) and lock root;
3. add the client `agent-box` (window scopes ask, screen scopes refused); the config guard asks the
   person on screen, as for any new client; its token goes into the distro, readable by `agent` alone;
4. restart the distro and check that the lock-down took effect;
5. allow the gateway's port through the Windows firewall on the WSL adapter, for the gateway's own
   Python only. This is the one step that needs administrator rights; it asks with a UAC prompt.

`remove` undoes each step. Every outside command goes through `run`, so tests can fake wsl.exe and
PowerShell.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

DISTRO = "overshoulder-agent"
CLIENT = "agent-box"
AGENT = "agent"
AGENT_UID = 4242
TOKEN_PATH = "/home/agent/.overshoulder/token"
PORT_PATH = "/etc/overshoulder-port"
INDEX_URL = "https://raw.githubusercontent.com/microsoft/WSL/master/distributions/DistributionInfo.json"
FIREWALL_GROUP = "Overshoulder agent box"
FIREWALL_NAME = "Overshoulder MCP from the agent box"
WSL_ADAPTER = "vEthernet (WSL*"

WSL_CONF = """\
[boot]
systemd=false
command=/usr/local/sbin/agent-boot.sh
[automount]
enabled=false
mountFsTab=false
[interop]
enabled=false
appendWindowsPath=false
[user]
default=agent
"""

AGENT_BOOT = """\
#!/bin/sh
# Run as root each time the distro starts ([boot] command in /etc/wsl.conf).
/usr/local/sbin/agent-egress.sh
# The Windows host's address on WSL's network changes when WSL starts again; agents reach the gateway at
# http://overshoulder-gateway:<port>/mcp, whatever it is.
GW=$(ip route | awk '/default/{print $3; exit}')
sed -i '/ overshoulder-gateway$/d' /etc/hosts
[ -n "$GW" ] && echo "$GW overshoulder-gateway" >> /etc/hosts
# WSLg gives Linux GUI apps a display remoted to the Windows desktop, with the Windows clipboard synced
# both ways. The agent gets neither: the X socket mount is removed, and the WSLg directory (Wayland,
# PulseAudio) is closed to everyone but root.
umount -l /tmp/.X11-unix 2>/dev/null
rmdir /tmp/.X11-unix 2>/dev/null
mkdir -p /tmp/.X11-unix && chown root:root /tmp/.X11-unix && chmod 700 /tmp/.X11-unix
for d in /mnt/wslg /mnt/wslg/runtime-dir; do
  [ -e "$d" ] && chown root:root "$d" 2>/dev/null; [ -e "$d" ] && chmod 700 "$d" 2>/dev/null
done
true
"""

AGENT_EGRESS = """\
#!/bin/sh
# Egress for the agent user (uid 4242) only. Every WSL2 distro shares one network namespace, so these
# rules apply VM-wide but match nothing except this uid. The agent may reach DNS, the Overshoulder
# gateway on the Windows host, and the public internet (its model API); never loopback (other distros'
# services), private ranges (the Windows host's SMB/RPC, the LAN, a tailnet) or link-local.
set -e
U=4242
PORT=$(cat /etc/overshoulder-port 2>/dev/null || echo 47621)
GW=$(ip route | awk '/default/{print $3; exit}')
DNS=$(awk '/^nameserver/{print $2; exit}' /etc/resolv.conf)
for ipt in iptables ip6tables; do
  $ipt -N AGENT_OUT 2>/dev/null || $ipt -F AGENT_OUT
  $ipt -C OUTPUT -m owner --uid-owner $U -j AGENT_OUT 2>/dev/null ||
    $ipt -I OUTPUT 1 -m owner --uid-owner $U -j AGENT_OUT
done
iptables -A AGENT_OUT -d "$DNS" -p udp --dport 53 -j ACCEPT
iptables -A AGENT_OUT -d "$DNS" -p tcp --dport 53 -j ACCEPT
iptables -A AGENT_OUT -d "$GW" -p tcp --dport "$PORT" -j ACCEPT
for net in 127.0.0.0/8 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16 100.64.0.0/10 224.0.0.0/4 0.0.0.0/8; do
  iptables -A AGENT_OUT -d "$net" -j REJECT
done
iptables -A AGENT_OUT -j ACCEPT
for net in ::1/128 fc00::/7 fe80::/10 ff00::/8 ::ffff:0:0/96; do
  ip6tables -A AGENT_OUT -d "$net" -j REJECT
done
ip6tables -A AGENT_OUT -j ACCEPT
"""

# Run as root in the distro after the files are written. Each line can run again on a box that has it.
ROOT_SETUP = f"""\
set -e
chmod 755 /usr/local/sbin/agent-boot.sh /usr/local/sbin/agent-egress.sh
# iptables for the egress filter; git, curl and certificates for the agent's own work (it can't install
# packages itself: it isn't root).
for c in iptables git curl; do command -v $c >/dev/null || NEED=1; done
[ -z "$NEED" ] || (apt-get update -qq &&
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq iptables git curl ca-certificates)
id -u {AGENT} >/dev/null 2>&1 || useradd -m -s /bin/bash -u {AGENT_UID} {AGENT}
[ "$(id -u {AGENT})" = "{AGENT_UID}" ]
for g in sudo adm wheel; do gpasswd -d {AGENT} "$g" >/dev/null 2>&1 || true; done
passwd -l {AGENT} >/dev/null
passwd -l root >/dev/null
"""

# Checked after the restart. Each command prints "ok" only when its part of the lock-down holds. The user
# is who runs it: None is the distro's default user, which must itself be agent.
CHECKS = {
    "agent user": (None, '[ "$(id -u)" = "{uid}" ] && echo ok', "the default user is not agent (uid {uid})"),
    # Windows drives are 9p (or drvfs) mounts. /usr/lib/wsl/drivers is the GPU driver store every distro
    # gets, read-only. An empty /mnt/c left from an earlier set-up is not a mount.
    "no Windows drives": ("agent", "awk '$3==\"9p\"||$3==\"drvfs\"{{print $2}}' /proc/mounts | "
                          "grep -vx /usr/lib/wsl/drivers | grep -q . || echo ok", "a Windows drive is mounted"),
    # WSLInterop in binfmt_misc is shared by every distro in the VM, so this tries to start a Windows
    # program instead: with interop off, WSL's init can't hand it to Windows (UtilAcceptVsock, ~10 s).
    "no Windows programs": ("agent", "[ -z \"$WSL_INTEROP\" ] || exit 0; f=$(mktemp /tmp/probe.XXXXXX.exe); "
                            "printf 'MZ\\220\\000' >\"$f\"; chmod +x \"$f\"; o=$(\"$f\" 2>&1); rm -f \"$f\"; "
                            "case \"$o\" in *UtilAcceptVsock*) echo ok;; esac",
                            "Windows programs can run (interop is on)"),
    "not root": ("agent", "! sudo -n true 2>/dev/null && ! su -c true root </dev/null 2>/dev/null && echo ok",
                 "agent can become root"),
    "egress filter": ("root", "iptables -C OUTPUT -m owner --uid-owner {uid} -j AGENT_OUT 2>/dev/null && "
                      "ip6tables -C OUTPUT -m owner --uid-owner {uid} -j AGENT_OUT 2>/dev/null && echo ok",
                      "the egress filter for uid {uid} is not in place"),
    "no WSLg": ("agent", "[ ! -S /tmp/.X11-unix/X0 ] && ! ls /mnt/wslg/runtime-dir >/dev/null 2>&1 && echo ok",
                "the agent can reach WSLg (the Windows clipboard, Wayland or PulseAudio)"),
    # The rule above being there is not enough: its rules may have been flushed. This has the agent try a
    # private and a loopback address and reads the filter's own counters to see that it refused both.
    "network filter": ("root", "iptables -Z AGENT_OUT && su -s /bin/bash {user} -c 'timeout 3 bash -c "
                       "\"</dev/tcp/10.255.255.1/9\" 2>/dev/null; timeout 3 bash -c \"</dev/tcp/127.0.0.1/9\" "
                       "2>/dev/null'; [ \"$(iptables -L AGENT_OUT -v -x -n | awk '$3==\"REJECT\" && "
                       "($9==\"10.0.0.0/8\" || $9==\"127.0.0.0/8\") && $1>0' | wc -l)\" = 2 ] && "
                       "iptables -S AGENT_OUT | grep -q -- \"--dport $(cat " + PORT_PATH + ") -j ACCEPT\" && echo ok",
                       "the network filter does not refuse the agent's private and loopback connections"),
    "token": ("agent", f"[ -r {TOKEN_PATH} ] && [ \"$(stat -c %a {TOKEN_PATH})\" = 600 ] && echo ok",
              "the client token is missing or readable by others"),
}

Run = Callable[..., subprocess.CompletedProcess]


def _run(args: list[str], input: bytes | None = None, timeout: float = 600) -> subprocess.CompletedProcess:
    env = {**os.environ, "WSL_UTF8": "1"}  # wsl.exe prints UTF-16 otherwise
    return subprocess.run(args, input=input, capture_output=True, timeout=timeout, env=env,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


class BoxError(RuntimeError):
    pass


@dataclass
class Box:
    """What `create` and `remove` do, with the outside world passed in."""
    run: Run = _run
    say: Callable[[str], None] = print
    registry: object = None
    port: int = 47621
    fetch: Callable[[str], bytes] = field(default=lambda url: urllib.request.urlopen(url, timeout=60).read())
    download: Callable[[str, Path], None] | None = None

    def __post_init__(self):
        if self.registry is None:
            from overshoulder.gateway.clients import ClientRegistry
            self.registry = ClientRegistry()

    # -- WSL -------------------------------------------------------------------------------------

    def _wsl(self, *args: str, input: bytes | None = None, check: bool = True) -> str:
        r = self.run(["wsl.exe", *args], input=input)
        out = (r.stdout or b"").decode("utf-8", "replace") if isinstance(r.stdout, bytes) else (r.stdout or "")
        if check and r.returncode != 0:
            err = r.stderr.decode("utf-8", "replace") if isinstance(r.stderr, bytes) else (r.stderr or "")
            raise BoxError(f"wsl {' '.join(args[:4])} failed: {(err or out).strip()[:300]}")
        return out

    def _root(self, script: str, input: bytes | None = None) -> str:
        # --exec: without it wsl.exe hands the command to the distro's login shell first, which expands
        # every $variable in the script before sh sees it.
        return self._wsl("-d", DISTRO, "-u", "root", "--exec", "sh", "-c", script, input=input)

    def _put(self, path: str, content: str | bytes, mode: str, owner: str = "root") -> None:
        data = content.encode("utf-8") if isinstance(content, str) else content
        d = shlex.quote(str(Path(path).parent.as_posix()))
        p = shlex.quote(path)
        self._root(f"mkdir -p {d} && cat > {p}.new && chown {owner}:{owner} {p}.new && chmod {mode} {p}.new "
                   f"&& mv -f {p}.new {p}", input=data)

    def exists(self) -> bool:
        names = self._wsl("--list", "--quiet").replace("\x00", "").split()
        return DISTRO in names

    # -- create ------------------------------------------------------------------------------------

    def image(self) -> tuple[str, str]:
        """The Debian image's URL and SHA-256 from Microsoft's list."""
        index = json.loads(self.fetch(INDEX_URL))
        arch = "Arm64Url" if os.environ.get("PROCESSOR_ARCHITECTURE", "").upper() == "ARM64" else "Amd64Url"
        for d in index.get("ModernDistributions", {}).get("Debian", []):
            ref = d.get(arch) or {}
            if ref.get("Url") and ref.get("Sha256"):
                return ref["Url"], ref["Sha256"].lower().removeprefix("0x")
        raise BoxError("Microsoft's distribution list has no Debian image for this PC")

    def _import(self, home: Path) -> None:
        url, sha = self.image()
        with tempfile.TemporaryDirectory(prefix="overshoulder-box-") as tmp:
            img = Path(tmp) / "debian.wsl"
            self.say(f"Downloading {url}")
            (self.download or _download)(url, img)
            got = _sha256(img)
            if got != sha:
                raise BoxError(f"the Debian image's SHA-256 is {got}, Microsoft's list says {sha}; not imported")
            home.mkdir(parents=True, exist_ok=True)
            self._wsl("--import", DISTRO, str(home), str(img), "--version", "2")

    def _token(self) -> None:
        """Give the box a token that matches its client, adding or rotating the client when needed."""
        c = self.registry.get(CLIENT)
        if c is not None:
            have = self._root(f"cat {TOKEN_PATH} 2>/dev/null || true").strip()
            if have and hashlib.sha256(have.encode()).hexdigest() == c.token_sha256:
                return
        token = self.registry.add(CLIENT, window="approve", screen="deny")
        self._put(TOKEN_PATH, token + "\n", "600", owner=AGENT)
        self._root(f"chown {AGENT}:{AGENT} {shlex.quote(str(Path(TOKEN_PATH).parent.as_posix()))} && "
                   f"chmod 700 {shlex.quote(str(Path(TOKEN_PATH).parent.as_posix()))}")
        self.say(f"Client {CLIENT!r} has a new token. If the gateway is running, answer the box on screen.")

    def check(self) -> list[str]:
        """What doesn't hold in a running box, as the agent sees it (empty when all of it holds)."""
        failed = []
        for name, (user, cmd, why) in CHECKS.items():
            who = ("-u", user) if user else ()
            # On stdin, so Windows' command-line quoting never touches the script.
            script = cmd.format(uid=AGENT_UID, user=AGENT)
            out = self._wsl("-d", DISTRO, *who, "--exec", "sh", "-s", input=script.encode(), check=False)
            if out.strip() != "ok":
                failed.append(f"{name}: {why.format(uid=AGENT_UID)}")
        return failed

    def create(self, home: Path, firewall: bool = True) -> list[str]:
        """Build or repair the box; returns what the final check found wrong (empty when it all holds)."""
        if self.exists():
            self.say(f"The distro {DISTRO!r} exists; checking and repairing it.")
        else:
            self._import(home)
        self._put("/etc/wsl.conf", WSL_CONF, "644")
        self._put("/usr/local/sbin/agent-boot.sh", AGENT_BOOT, "755")
        self._put("/usr/local/sbin/agent-egress.sh", AGENT_EGRESS, "755")
        self._put(PORT_PATH, f"{self.port}\n", "644")
        self._root(ROOT_SETUP)
        self._token()
        self._wsl("--terminate", DISTRO, check=False)
        # Starting it again applies the new wsl.conf. The boot script it starts may still be running, so
        # it is run once more here before the check (it can run any number of times).
        self._root("/usr/local/sbin/agent-boot.sh")
        failed = self.check()
        if firewall:
            self.allow_through_firewall()
        self.serve_box()
        return failed

    def serve_box(self) -> None:
        """Turn on the setting that has the gateway serve the box. It widens what agents may reach, so a
        running gateway asks the person on screen before it takes effect."""
        from overshoulder import settings as st
        s = st.load_or_default()
        if not s.listen_wsl:
            s.listen_wsl = True
            st.save(s)
            self.say("Turned on serving the agent box. If the gateway is running, answer the box on screen.")

    # -- firewall ----------------------------------------------------------------------------------

    def firewall_rules(self) -> int:
        out = self._ps(f"@(Get-NetFirewallRule -Group '{FIREWALL_GROUP}' -ErrorAction SilentlyContinue).Count")
        try:
            return int(out.strip() or 0)
        except ValueError:
            return 0

    def _ps(self, command: str) -> str:
        r = self.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command])
        return r.stdout.decode("utf-8", "replace") if isinstance(r.stdout, bytes) else (r.stdout or "")

    def _elevated(self, command: str) -> None:
        """Run a PowerShell command as administrator; Windows shows its UAC prompt first."""
        import base64
        enc = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
        outer = (f"$p = Start-Process powershell.exe -Verb RunAs -Wait -PassThru -WindowStyle Hidden "
                 f"-ArgumentList '-NoProfile','-NonInteractive','-EncodedCommand','{enc}'; exit $p.ExitCode")
        r = self.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", outer])
        if r.returncode != 0:
            raise BoxError("the firewall change was not made (the UAC prompt was declined, or it failed)")

    def gateway_programs(self) -> list[str]:
        """The Python executables the gateway runs as: a venv's python starts the base interpreter, and
        the scheduled task uses pythonw, so the rule names both of the base install's."""
        base = Path(getattr(sys, "_base_executable", sys.executable)).parent
        return [str(base / exe) for exe in ("python.exe", "pythonw.exe") if (base / exe).exists()]

    def allow_through_firewall(self) -> None:
        if self.firewall_rules() >= len(self.gateway_programs()):
            return
        lines = [f"Get-NetFirewallRule -Group '{FIREWALL_GROUP}' -ErrorAction SilentlyContinue | "
                 "Remove-NetFirewallRule",
                 # the rule an earlier hand-made set-up used, which named python.exe alone
                 "Get-NetFirewallRule -DisplayName 'Overshoulder MCP from WSL' -ErrorAction SilentlyContinue | "
                 "Remove-NetFirewallRule",
                 f"$a = (Get-NetAdapter -IncludeHidden | Where-Object Name -like '{WSL_ADAPTER}').Name"]
        for prog in self.gateway_programs():
            lines.append(f"New-NetFirewallRule -DisplayName '{FIREWALL_NAME}' -Group '{FIREWALL_GROUP}' "
                         f"-Direction Inbound -Action Allow -Protocol TCP -LocalPort {int(self.port)} "
                         f"-InterfaceAlias $a -Program '{prog}' | Out-Null")
        self.say("Allowing the gateway through the Windows firewall from WSL; Windows will ask (UAC).")
        self._elevated("; ".join(lines))

    # -- remove ------------------------------------------------------------------------------------

    def remove(self) -> None:
        if self.exists():
            self._wsl("--unregister", DISTRO)
            self.say(f"Removed the distro {DISTRO!r} and everything in it.")
        if self.registry.remove(CLIENT):
            self.say(f"Removed the client {CLIENT!r}.")
        if self.firewall_rules():
            self._elevated(f"Get-NetFirewallRule -Group '{FIREWALL_GROUP}' | Remove-NetFirewallRule")
            self.say("Removed the firewall rules.")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(url: str, to: Path) -> None:
    with urllib.request.urlopen(url, timeout=120) as r, open(to, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)


def default_home() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "OvershoulderAgentWSL"


DAY_S = 24 * 3600


class Watch:
    """Checks the box when the gateway starts and once a day, and keeps the last result for the person
    (the operator status and the settings page). Root in another distro, or Docker rewriting iptables,
    can undo the network filter while the box runs; this is how the person finds out."""

    def __init__(self, box: Callable[[], Box] | None = None, log: Callable[..., None] = lambda *a, **k: None,
                 every_s: float = DAY_S):
        # check() needs no client registry, so the box gets none
        self._box = box or (lambda: Box(say=lambda s: None, registry=False))
        self._log, self.every_s = log, every_s
        self._result: dict | None = None
        self._lock = threading.Lock()

    def run_once(self) -> dict:
        b = self._box()
        try:
            if not b.exists():
                result = {"exists": False}
            else:
                failed = b.check()
                result = {"exists": True, "ok": not failed, "failed": failed}
        except Exception as e:  # wsl.exe missing or broken: say so, don't stop the gateway
            result = {"exists": None, "ok": False, "failed": [f"the check could not run: {e}"]}
        result["at"] = time.time()
        with self._lock:
            self._result = result
        if result["exists"] is not False:
            self._log("agent_box_check", ok=result["ok"], failed=result["failed"])
        return result

    def start(self, stop: threading.Event) -> threading.Thread:
        def loop():
            self.run_once()
            while not stop.wait(self.every_s):
                self.run_once()
        t = threading.Thread(target=loop, name="overshoulder-agent-box-check", daemon=True)
        t.start()
        return t

    def status(self) -> dict | None:
        """The last result with its age in seconds; None before the first check finished."""
        with self._lock:
            r = dict(self._result) if self._result else None
        if r:
            r["age_s"] = round(time.time() - r.pop("at"))
        return r


def describe(r: dict | None) -> str:
    """One line for a person about the last check."""
    if r is None:
        return "The agent box has not been checked yet."
    if r["exists"] is False:
        return "There is no agent box (overshoulder agent-box create makes one)."
    age = r.get("age_s", 0)
    when = f"{age // 3600} h ago" if age >= 3600 else f"{age // 60} min ago"
    if r["ok"]:
        return f"The agent box held at the last check ({when})."
    return f"THE AGENT BOX IS NOT LOCKED DOWN (checked {when}): " + "; ".join(r["failed"])
