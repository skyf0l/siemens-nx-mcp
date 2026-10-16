r"""Start graphical NX detached, optionally with a user directory for startup apps.

Development helper for fully automatic live testing: NX loads applications
from <UGII_USER_DIR>/startup at launch, so no journal has to be played by hand.

    python scripts/dev_start_nx.py --user-dir C:\path\to\nx_user_dir
    python scripts/dev_start_nx.py --wait 120   # wait until NX shows its main window
    python scripts/dev_start_nx.py --nx-root "C:\Program Files\Siemens\NX2406"

Which NX: --nx-root, else UGII_BASE_DIR, else the installations Windows knows
(HKLM\SOFTWARE\Siemens\NX*, Siemens NX uninstall entries, <Program Files>\Siemens\NX*
folders), each a folder with NXBIN\ugraf.exe. With several, the one matching
UGII_VERSION (e.g. v2406) wins, else the newest; the script prints which one it
starts and NX gets UGII_BASE_DIR set to it.

NX inherits this environment: NX_MCP_WORKSPACE if set (else the hosts use
<home>/NX_MCP_WORKSPACE, like the sidecar); --env NX_MCP_WORKSPACE=... overrides it.
Without --user-dir no startup app loads; play nx_app/journals/start.py instead.
NX runs in <workspace>/.nx-mcp/nx-cwd: it writes scratch files to its working directory
(udf_database.txt and dbc_udf_ascii.def when the hole and thread library loads), which
then stay out of the checkout.
"""

import argparse
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

SIEMENS_KEY = r"SOFTWARE\Siemens"
UNINSTALL_KEYS = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
)
ROOT_VALUES = ("UGII_BASE_DIR", "INSTALLDIR", "")  # "" is the key's default value.


@dataclass(frozen=True)
class Install:
    root: Path
    release: str  # "2406", "12.0"; "" when no name says it
    source: str

    @property
    def order(self) -> tuple[int, ...]:
        return tuple(int(part) for part in re.findall(r"\d+", self.release))

    def label(self) -> str:
        return f"NX {self.release}" if self.release else "NX (release unknown)"


def release_of(text: str) -> str:
    """``2406`` for ``NX 2406``, ``Siemens NX 2406`` or ``NX2406``; ``""`` otherwise."""
    match = re.search(r"NX\s*(\d+(?:\.\d+)?)", text or "", re.IGNORECASE)
    return match.group(1) if match else ""


def has_ugraf(root: Path) -> bool:
    return (root / "NXBIN" / "ugraf.exe").is_file()


def _subkeys(winreg, key) -> list[str]:
    names, index = [], 0
    while True:
        try:
            names.append(winreg.EnumKey(key, index))
        except OSError:
            return names
        index += 1


def _value(winreg, key, name: str):
    try:
        return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def registry_installs(winreg=None) -> list[Install]:
    r"""NX installations the Windows registry names (HKLM\SOFTWARE\Siemens\NX*, then the
    Siemens NX uninstall entries); [] off Windows."""
    if winreg is None:
        try:
            import winreg
        except ImportError:
            return []
    hive, found = winreg.HKEY_LOCAL_MACHINE, []
    try:
        with winreg.OpenKey(hive, SIEMENS_KEY) as siemens:
            for name in _subkeys(winreg, siemens):
                if not name.upper().startswith("NX"):
                    continue
                with winreg.OpenKey(siemens, name) as key:
                    values = (_value(winreg, key, value) for value in ROOT_VALUES)
                    root = next((value for value in values if value), None)
                if root:
                    source = f"HKLM\\{SIEMENS_KEY}\\{name}"
                    found.append(Install(Path(root), release_of(name), source))
    except OSError:
        pass
    for uninstall in UNINSTALL_KEYS:
        try:
            with winreg.OpenKey(hive, uninstall) as entries:
                for name in _subkeys(winreg, entries):
                    with winreg.OpenKey(entries, name) as key:
                        display = str(_value(winreg, key, "DisplayName") or "")
                        publisher = str(_value(winreg, key, "Publisher") or "")
                        location = _value(winreg, key, "InstallLocation")
                    if (
                        location
                        and "siemens" in publisher.lower()
                        and re.search(r"\bNX\b", display)
                    ):
                        found.append(Install(Path(location), release_of(display), display))
        except OSError:
            continue
    return found


def folder_installs(env: dict[str, str]) -> list[Install]:
    r"""<Program Files>\Siemens\NX* folders."""
    bases: list[str] = []
    for base in (env.get("ProgramW6432"), env.get("ProgramFiles")):
        if base and os.path.normcase(base) not in [os.path.normcase(b) for b in bases]:
            bases.append(base)
    bases = bases or [r"C:\Program Files"]
    found = []
    for base in bases:
        siemens = Path(base) / "Siemens"
        if siemens.is_dir():
            for child in sorted(siemens.iterdir()):
                if child.is_dir() and child.name.upper().startswith("NX"):
                    found.append(Install(child, release_of(child.name), f"folder {siemens}"))
    return found


def find_installs(env: dict[str, str], winreg=None) -> list[Install]:
    r"""Every NX installation with NXBIN\ugraf.exe, once each (the first source kept)."""
    unique: dict[str, Install] = {}
    for install in registry_installs(winreg) + folder_installs(env):
        key = os.path.normcase(os.path.normpath(str(install.root)))
        if key in unique or not has_ugraf(install.root):
            continue
        release = install.release or release_of(install.root.name)
        unique[key] = Install(install.root, release, install.source)
    return list(unique.values())


def newest(installs: list[Install]) -> Install:
    return max(installs, key=lambda i: (i.order, str(i.root).lower()))


def choose(installs: list[Install], ugii_version: str | None) -> tuple[Install, str]:
    """The installation to start and why: the one UGII_VERSION names, else the newest."""
    wanted = release_of("NX" + re.sub(r"^[vV]", "", ugii_version or ""))
    if wanted:
        major = wanted.split(".")[0]
        matching = [i for i in installs if i.release.split(".")[0] == major]
        if matching:
            return newest(matching), f"UGII_VERSION={ugii_version}"
        return newest(installs), f"the newest; UGII_VERSION={ugii_version} matches none"
    return newest(installs), "the newest" if len(installs) > 1 else "the only one found"


def resolve_nx_root(nx_root: Path | None, env: dict[str, str], winreg=None) -> tuple[Path, str]:
    """(NX root, the line to print); raises LookupError with the reason when none fits."""
    for given, origin in ((nx_root, "--nx-root"), (env.get("UGII_BASE_DIR"), "UGII_BASE_DIR")):
        if given:
            root = Path(given)
            if not has_ugraf(root):
                raise LookupError(f"{origin} {root} has no NXBIN\\ugraf.exe")
            return root, f"NX root {root} ({origin})"
    installs = find_installs(env, winreg)
    if not installs:
        raise LookupError(
            r"No NX installation found (HKLM\SOFTWARE\Siemens\NX*, Siemens NX uninstall "
            r"entries, <Program Files>\Siemens\NX*); pass --nx-root or set UGII_BASE_DIR"
        )
    chosen, why = choose(installs, env.get("UGII_VERSION"))
    line = f"NX root {chosen.root} ({chosen.label()}, {why}; from {chosen.source})"
    others = [i for i in installs if i is not chosen]
    if others:
        listed = ", ".join(f"{i.label()} at {i.root}" for i in others)
        line += f"; also installed: {listed} (choose with --nx-root)"
    return chosen.root, line


def nx_pids() -> list[int]:
    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq ugraf.exe", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return [int(line.split(",")[1].strip('"')) for line in out.splitlines() if "ugraf.exe" in line]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--user-dir", type=Path, help="UGII_USER_DIR for this NX session")
    parser.add_argument("--env", action="append", default=[], help="extra NAME=VALUE")
    parser.add_argument("--wait", type=float, default=90, help="seconds to wait for the NX process")
    parser.add_argument(
        "--nx-root", type=Path, help="NX installation folder (default: found automatically)"
    )
    args = parser.parse_args()
    if nx_pids():
        print(f"NX is already running: {nx_pids()}")
        return 1
    try:
        base, line = resolve_nx_root(args.nx_root, dict(os.environ))
    except LookupError as exc:
        print(exc)
        return 2
    print(line)
    env = dict(os.environ)
    env["UGII_BASE_DIR"] = str(base)  # NX's own files come from the same installation.
    if args.user_dir:
        env["UGII_USER_DIR"] = str(args.user_dir.resolve())
    for item in args.env:
        name, _, value = item.partition("=")
        env[name] = value
    workspace = Path(env.get("NX_MCP_WORKSPACE") or Path.home() / "NX_MCP_WORKSPACE")
    cwd = workspace / ".nx-mcp" / "nx-cwd"
    cwd.mkdir(parents=True, exist_ok=True)
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        [str(base / "NXBIN" / "ugraf.exe")],
        env=env,
        cwd=cwd,
        creationflags=flags,
        close_fds=True,
    )
    deadline = time.monotonic() + args.wait
    while time.monotonic() < deadline:
        if nx_pids():
            print(
                f"NX started: pid {nx_pids()[0]}"
                + (f", UGII_USER_DIR={env['UGII_USER_DIR']}" if args.user_dir else "")
            )
            return 0
        time.sleep(1)
    print("NX did not start in time")
    return 2


if __name__ == "__main__":
    sys.exit(main())
