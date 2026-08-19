"""System-wide proxy control for Windows (WinINET registry + WinHTTP).

Mirrors proxyctl.py's API surface so ui.py needs no backend logic:
  available() / backup() / save_backup() / load_backup() / clear_backup()
  restore(saved) / connect(p, saved) / check_alive(p) / force_off()

Mechanism: the Settings > Network & Proxy toggle is the WinINET registry key
  HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings
  (ProxyEnable DWORD, ProxyServer REG_SZ host:port, ProxyOverride, AutoConfigURL).
No admin needed (HKCU). After every change InternetSetOptionW makes running
apps pick it up instantly. WinHTTP (used by Windows Update/services) is set
best-effort via `netsh winhttp` — needs admin; on failure it is silently
skipped and only WinINET coverage applies.

winreg/ctypes are imported lazily (Windows-only modules) so this file imports
and can be unit-tested on Linux with fakes injected into sys.modules.
"""
import asyncio, json, os, socket, subprocess

INTERNET_SETTINGS_KEY = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
# (value-name, "") pairs — same tuple shape as proxyctl's (path, key) dict keys
WIN_KEYS = ["ProxyEnable", "ProxyServer", "ProxyOverride", "AutoConfigURL"]
# marker persisted in the backup: "1" only when WE set WinHTTP (so restore/reset
# never destroys a WinHTTP proxy the user configured before ProxyVeil)
WINHTTP_MARKER = ("__winhttp", "")
REG_TYPES = {"ProxyEnable": "dword"}  # everything else REG_SZ


def _cache_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "proxyveil")

BACKUP_FILE = os.path.join(_cache_dir(), "backup.json")


def _winreg():
    import winreg  # Windows-only — lazy so Linux tests can fake sys.modules
    return winreg


def _open(access):
    wr = _winreg()
    return wr.OpenKey(wr.HKEY_CURRENT_USER, INTERNET_SETTINGS_KEY, 0, access)


def _read(name):
    """Value as string; None if the value does not exist."""
    wr = _winreg()
    try:
        with _open(wr.KEY_READ) as k:
            val, _ = k.QueryValueEx(name)
        return str(val)
    except FileNotFoundError:
        return None


def _write(name, value) -> None:
    wr = _winreg()
    typ = wr.REG_DWORD if REG_TYPES.get(name) == "dword" else wr.REG_SZ
    val = int(value) if typ == wr.REG_DWORD else value
    with _open(wr.KEY_SET_VALUE) as k:
        k.SetValueEx(name, 0, typ, val)


def _delete(name) -> None:
    wr = _winreg()
    try:
        with _open(wr.KEY_SET_VALUE) as k:
            k.DeleteValue(name)
    except FileNotFoundError:
        pass


def available() -> bool:
    """Is the WinINET proxy registry key reachable?"""
    try:
        with _open(_winreg().KEY_READ):
            pass
        return True
    except Exception:
        return False


def backup() -> dict:
    """Read all current values (as strings; None = absent). Persisted to disk."""
    out = {(name, ""): _read(name) for name in WIN_KEYS}
    out[WINHTTP_MARKER] = "0"
    save_backup(out)
    return out


def save_backup(saved: dict) -> None:
    """Write backup to disk (atomic). Best-effort: never breaks connect."""
    try:
        os.makedirs(os.path.dirname(BACKUP_FILE), exist_ok=True)
        data = [{"name": n, "value": v} for (n, _), v in saved.items()]
        tmp = BACKUP_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, BACKUP_FILE)
    except OSError:
        pass


def load_backup():
    """Read persisted backup; None if absent/corrupt."""
    try:
        with open(BACKUP_FILE) as f:
            data = json.load(f)
        return {(d["name"], ""): d["value"] for d in data}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def clear_backup() -> None:
    """Remove persisted backup (after a successful restore)."""
    try:
        os.remove(BACKUP_FILE)
    except OSError:
        pass


def connect(p, saved: dict) -> None:
    """Connect to the selected proxy. saved: backup() result (for disconnect)."""
    from collector import Proxy
    assert isinstance(p, Proxy) and p.protocol in ("http", "https")
    _write("ProxyEnable", "1")
    _write("ProxyServer", f"{p.host}:{p.port}")
    # ProxyOverride untouched (already backed up)
    _win_refresh()
    if _winhttp_set(f"{p.host}:{p.port}"):
        saved[WINHTTP_MARKER] = "1"
        save_backup(saved)


def restore(saved: dict):
    """Restore backed-up values (disconnect). Skips internal markers."""
    for (name, _), val in saved.items():
        if name.startswith("__"):
            continue
        if val is None:
            _delete(name)
        else:
            _write(name, val)
    _win_refresh()
    if saved.get(WINHTTP_MARKER) == "1":
        _winhttp_reset()


def force_off() -> None:
    """Last-resort emergency off: disable the toggle, restore persisted WinHTTP
    only if we set it (marker read from disk — crash path has no in-memory state)."""
    _write("ProxyEnable", "0")
    _win_refresh()
    saved = load_backup()
    if saved and saved.get(WINHTTP_MARKER) == "1":
        _winhttp_reset()


def check_alive(p, timeout: float = 2.0) -> bool:
    """Fast TCP connect: is the proxy still listening?"""
    try:
        with socket.create_connection((p.host, p.port), timeout=timeout):
            return True
    except OSError:
        return False


def _win_refresh() -> None:
    """Notify WinINET that proxy settings changed — browsers pick it up instantly."""
    try:
        import ctypes
        wininet = ctypes.windll.wininet
        wininet.InternetSetOptionW(None, 39, None, 0)  # INTERNET_OPTION_SETTINGS_CHANGED
        wininet.InternetSetOptionW(None, 37, None, 0)  # INTERNET_OPTION_REFRESH
    except Exception:
        pass


def _winhttp_set(server: str) -> bool:
    """Best-effort WinHTTP proxy. True only if it actually applied (admin)."""
    try:
        r = subprocess.run(["netsh", "winhttp", "set", "proxy", server],
                           capture_output=True, timeout=5)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _winhttp_reset() -> None:
    try:
        subprocess.run(["netsh", "winhttp", "reset", "proxy"],
                       capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


class WinProxyError(Exception):
    pass
