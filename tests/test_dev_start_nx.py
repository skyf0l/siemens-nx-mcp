"""scripts/dev_start_nx.py finds the NX installation to start (fake registry and folders)."""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "dev_start_nx.py"
spec = importlib.util.spec_from_file_location("dev_start_nx_under_test", SCRIPT)
assert spec is not None and spec.loader is not None
dev_start_nx = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dev_start_nx)

SIEMENS = "HKLM\\SOFTWARE\\Siemens"
UNINSTALL = "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall"


class FakeKey:
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeWinreg:
    """winreg over {key path: {value name: data}}; parent keys exist implicitly."""

    def __init__(self, keys):
        self.keys = keys
        self.HKEY_LOCAL_MACHINE = FakeKey("HKLM")

    def _exists(self, path):
        return any(k == path or k.startswith(path + "\\") for k in self.keys)

    def OpenKey(self, parent, name):
        path = f"{parent.path}\\{name}"
        if not self._exists(path):
            raise FileNotFoundError(path)
        return FakeKey(path)

    def EnumKey(self, key, index):
        children = sorted(
            {
                k[len(key.path) + 1 :].split("\\")[0]
                for k in self.keys
                if k.startswith(key.path + "\\")
            }
        )
        if index >= len(children):
            raise OSError("no more keys")
        return children[index]

    def QueryValueEx(self, key, name):
        values = self.keys.get(key.path, {})
        if name not in values:
            raise FileNotFoundError(name)
        return values[name], 1


def nx(tmp_path, folder, ugraf=True):
    root = tmp_path / "Program Files" / "Siemens" / folder
    (root / "NXBIN").mkdir(parents=True)
    if ugraf:
        (root / "NXBIN" / "ugraf.exe").write_bytes(b"")
    return root


@pytest.fixture
def env(tmp_path):
    return {"ProgramFiles": str(tmp_path / "Program Files")}


def test_release_names():
    assert dev_start_nx.release_of("NX 2406") == "2406"
    assert dev_start_nx.release_of("Siemens NX 2512") == "2512"
    assert dev_start_nx.release_of("NX2306") == "2306"
    assert dev_start_nx.release_of("NX 12.0") == "12.0"
    assert dev_start_nx.release_of("Solid Edge 2024") == ""


def test_registry_key_and_uninstall_entry_give_one_install(tmp_path, env):
    root = nx(tmp_path, "NX2406")
    registry = FakeWinreg(
        {
            f"{SIEMENS}\\NX 2406": {"UGII_BASE_DIR": str(root), "": str(root) + "\\"},
            f"{SIEMENS}\\Installed Products": {},
            f"{UNINSTALL}\\{{A3B2}}": {
                "DisplayName": "Siemens NX 2406",
                "Publisher": "Siemens",
                "InstallLocation": str(root) + "\\",
            },
            f"{UNINSTALL}\\{{OTHER}}": {"DisplayName": "Other", "InstallLocation": "C:\\x"},
        }
    )
    installs = dev_start_nx.find_installs(env, registry)
    assert [(i.root, i.release) for i in installs] == [(root, "2406")]
    assert installs[0].source == "HKLM\\SOFTWARE\\Siemens\\NX 2406"
    path, line = dev_start_nx.resolve_nx_root(None, env, registry)
    assert path == root and "NX 2406, the only one found" in line


def test_uninstall_entries_and_folders_are_found_without_siemens_keys(tmp_path, env):
    root_2412 = nx(tmp_path / "other", "NX2412")
    nx(tmp_path, "NX2306")
    nx(tmp_path, "NX2512", ugraf=False)  # A leftover folder: no ugraf.exe, never chosen.
    registry = FakeWinreg(
        {
            f"{UNINSTALL}\\{{B}}": {
                "DisplayName": "Siemens NX 2412",
                "Publisher": "Siemens Digital Industries Software",
                "InstallLocation": str(root_2412),
            }
        }
    )
    installs = dev_start_nx.find_installs(env, registry)
    assert sorted(i.release for i in installs) == ["2306", "2412"]
    path, line = dev_start_nx.resolve_nx_root(None, env, registry)
    assert path == root_2412 and "NX 2412, the newest" in line
    assert "also installed: NX 2306 at" in line and "--nx-root" in line


def test_ugii_version_picks_its_release_else_the_newest(tmp_path, env):
    root_2406, root_2506 = nx(tmp_path, "NX2406"), nx(tmp_path, "NX2506")
    registry = FakeWinreg({})
    path, line = dev_start_nx.resolve_nx_root(None, {**env, "UGII_VERSION": "v2406"}, registry)
    assert path == root_2406 and "UGII_VERSION=v2406" in line
    path, line = dev_start_nx.resolve_nx_root(None, {**env, "UGII_VERSION": "v2312"}, registry)
    assert path == root_2506 and "matches none" in line
    for _ in range(3):  # Deterministic.
        assert dev_start_nx.resolve_nx_root(None, env, registry)[0] == root_2506


def test_explicit_root_and_ugii_base_dir_win_and_are_checked(tmp_path, env):
    root_2406, root_2506 = nx(tmp_path, "NX2406"), nx(tmp_path, "NX2506")
    registry = FakeWinreg({})
    with_base = {**env, "UGII_BASE_DIR": str(root_2406)}
    assert dev_start_nx.resolve_nx_root(None, with_base, registry) == (
        root_2406,
        f"NX root {root_2406} (UGII_BASE_DIR)",
    )
    assert dev_start_nx.resolve_nx_root(root_2506, with_base, registry)[0] == root_2506
    with pytest.raises(LookupError, match="--nx-root .* has no NXBIN"):
        dev_start_nx.resolve_nx_root(tmp_path / "nowhere", env, registry)


def test_nothing_installed_says_how_to_choose(tmp_path, env):
    with pytest.raises(LookupError, match="No NX installation found.*--nx-root"):
        dev_start_nx.resolve_nx_root(None, env, FakeWinreg({}))


def test_registry_errors_and_missing_winreg_are_not_fatal(tmp_path, env, monkeypatch):
    class Broken(FakeWinreg):
        def OpenKey(self, parent, name):
            raise PermissionError(name)

    root = nx(tmp_path, "NX2406")
    assert [i.root for i in dev_start_nx.find_installs(env, Broken({}))] == [root]
    import builtins

    real_import = builtins.__import__

    def no_winreg(name, *args, **kwargs):
        if name == "winreg":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_winreg)
    assert dev_start_nx.registry_installs() == []
