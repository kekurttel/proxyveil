"""System-wide proxy control (gsettings/GNOME).

UI-independent and testable: all gsettings calls go through `_run_gsettings`;
tests mock it.

Flow:
  backup()            -> read and store current settings
  connect(p)          -> mode=manual, http/https host/port, keep ignore-hosts
  restore(saved)      -> restore backed-up values (disconnect)
  check_alive(p, 2s)  -> TCP connect: is the proxy still up?

Note: SOCKS proxies don't fully work with gsettings system proxy —
only HTTP/HTTPS can connect system-wide. Others stay in "project/export" mode.
"""
import asyncio, json, os, socket, subprocess

SCHEMA = "org.gnome.system.proxy"

def _cache_dir():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "proxyveil")

# Persisted pre-connect backup — survives crash/kill so restore works "at every means".
BACKUP_FILE = os.path.join(_cache_dir(), "backup.json")
# (schema-path or schema, key) pairs — backup/restore order
KEYS = [
    ("", "mode"),
    ("http", "host"), ("http", "port"), ("http", "enabled"),
    ("https", "host"), ("https", "port"),
    ("", "ignore-hosts"),
]


def _run_gsettings(*args):
    """Run gsettings. Error -> GSettingsError."""
    out = subprocess.run(["gsettings", *args], capture_output=True, text=True, timeout=5)
    if out.returncode != 0:
        raise GSettingsError(f"gsettings {' '.join(args)} -> {out.stderr.strip()}")
    return out.stdout.strip()


def available() -> bool:
    """Is gsettings installed and the GNOME proxy schema reachable?"""
    try:
        subprocess.run(["gsettings", "--version"], capture_output=True, timeout=5)
        # read schema — InvalidSchema?
        _run_gsettings("get", SCHEMA, "mode")
        return True
    except FileNotFoundError:
        return False
    except GSettingsError:
        return False


def backup() -> dict:
    """Read all current values (as strings), return {key: value}.
    Persisted to disk — a crash/kill mid-connect is recoverable next start."""
    out = {}
    for path, key in KEYS:
        full = f"{SCHEMA}.{path}" if path else SCHEMA
        out[(path, key)] = _run_gsettings("get", full, key)
    save_backup(out)
    return out

def save_backup(saved: dict) -> None:
    """Write backup to disk (atomic). Best-effort: never breaks connect."""
    try:
        os.makedirs(os.path.dirname(BACKUP_FILE), exist_ok=True)
        data = [{"path": p, "key": k, "value": v} for (p, k), v in saved.items()]
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
        return {(d["path"], d["key"]): d["value"] for d in data}
    except (OSError, ValueError, KeyError, TypeError):
        return None

def clear_backup() -> None:
    """Remove persisted backup (after a successful restore)."""
    try:
        os.remove(BACKUP_FILE)
    except OSError:
        pass


def restore(saved: dict):
    """Restore backed-up values (disconnect)."""
    for (path, key), val in saved.items():
        full = f"{SCHEMA}.{path}" if path else SCHEMA
        _run_gsettings("set", full, key, val)


def connect(p, saved: dict) -> None:
    """Connect to the selected proxy. saved: backup() result (for disconnect).
    Note: http.enabled only set to true (kept true if already)."""
    from collector import Proxy
    assert isinstance(p, Proxy) and p.protocol in ("http", "https")
    _run_gsettings("set", SCHEMA, "mode", "'manual'")
    _run_gsettings("set", f"{SCHEMA}.http", "enabled", "true")
    _run_gsettings("set", f"{SCHEMA}.http", "host", f"'{p.host}'")
    _run_gsettings("set", f"{SCHEMA}.http", "port", str(p.port))
    _run_gsettings("set", f"{SCHEMA}.https", "host", f"'{p.host}'")
    _run_gsettings("set", f"{SCHEMA}.https", "port", str(p.port))
    # ignore-hosts untouched (already backed up)


def check_alive(p, timeout: float = 2.0) -> bool:
    """Fast TCP connect: is the proxy still listening?"""
    try:
        with socket.create_connection((p.host, p.port), timeout=timeout):
            return True
    except OSError:
        return False


class GSettingsError(Exception):
    pass