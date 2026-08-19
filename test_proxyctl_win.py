"""Windows backend (proxyctl_win) assert-based unit tests.

Runs on Linux: winreg is faked via sys.modules, InternetSetOptionW patched,
netsh subprocess mocked. Never touches a real registry.
Çalıştır: python test_proxyctl_win.py
"""
import os, sys, tempfile
import proxyctl_win
from collector import Proxy

PASS = 0

def check(name, cond):
    global PASS
    assert cond, f"FAIL: {name}"
    PASS += 1
    print(f"  ok: {name}")


# ---------- fake winreg ----------
class FakeKey:
    def __init__(self, store):
        self.store = store
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def QueryValueEx(self, name):
        if name not in self.store:
            raise FileNotFoundError(name)
        return self.store[name]
    def SetValueEx(self, name, res, typ, val):
        self.store[name] = (val, typ)
    def DeleteValue(self, name):
        if name not in self.store:
            raise FileNotFoundError(name)
        del self.store[name]

class FakeWinreg:
    """Dict-backed winreg substitute: vals = {name: (value, type)}."""
    def __init__(self, vals, fail_open=False):
        self.vals = vals
        self.fail_open = fail_open
    HKEY_CURRENT_USER = "HKCU"
    KEY_READ = 1
    KEY_SET_VALUE = 2
    REG_DWORD = 4
    REG_SZ = 1
    def OpenKey(self, root, subkey, res, access):
        if self.fail_open:
            raise OSError("key unreachable")
        return FakeKey(self.vals)

WINREG_MODULES = {}  # name -> module replacements

def run():
    global PASS
    orig_winreg = sys.modules.get("winreg")
    orig_sys_platform = getattr(sys, "platform")
    orig_backup_file = proxyctl_win.BACKUP_FILE
    store = {"ProxyEnable": (1, 4), "ProxyServer": ("127.0.0.1:8080", 1),
             "ProxyOverride": ("<local>;192.168.*", 1)}
    sys.modules["winreg"] = FakeWinreg(store)
    proxyctl_win.BACKUP_FILE = os.path.join(tempfile.mkdtemp(), "backup.json")

    refresh_calls = []
    proxyctl_win._win_refresh = lambda: refresh_calls.append(1)
    winhttp_set_calls = []
    winhttp_reset_calls = []
    real_set = proxyctl_win._winhttp_set
    real_reset = proxyctl_win._winhttp_reset
    proxyctl_win._winhttp_set = lambda s: winhttp_set_calls.append(s) or True
    proxyctl_win._winhttp_reset = lambda: winhttp_reset_calls.append(1)

    try:
        print("== available() ==")
        check("registry erişilebilir", proxyctl_win.available() is True)
        sys.modules["winreg"] = FakeWinreg(store, fail_open=True)
        check("erişilemezse False", proxyctl_win.available() is False)
        sys.modules["winreg"] = FakeWinreg(store)

        print("== backup() ==")
        saved = proxyctl_win.backup()
        check("ProxyEnable yedeklendi", saved[("ProxyEnable", "")] == "1")
        check("ProxyServer yedeklendi", saved[("ProxyServer", "")] == "127.0.0.1:8080")
        check("eksik değer None", saved[("AutoConfigURL", "")] is None)
        check("WinHTTP marker başta 0", saved[("__winhttp", "")] == "0")

        print("== disk yedekleme (crash/kill kurtarma) ==")
        check("backup diske yazıldı", os.path.exists(proxyctl_win.BACKUP_FILE))
        disk = proxyctl_win.load_backup()
        check("load_backup geri okur", disk is not None and disk[("ProxyServer", "")] == "127.0.0.1:8080")
        check("None değeri korunur", disk[("AutoConfigURL", "")] is None)

        print("== connect() ==")
        p = Proxy(host="95.211.174.135", port=3128, protocol="http")
        refresh_calls.clear()
        proxyctl_win.connect(p, saved)
        check("ProxyEnable=1", store["ProxyEnable"] == (1, 4))
        check("ProxyServer ayarlandı", store["ProxyServer"][0] == "95.211.174.135:3128")
        check("ProxyOverride değişmedi", store["ProxyOverride"] == ("<local>;192.168.*", 1))
        check("WinINET refresh çağrıldı", len(refresh_calls) == 1)
        check("WinHTTP set edildi (host:port)", winhttp_set_calls == ["95.211.174.135:3128"])
        check("marker 1 yazıldı", saved[("__winhttp", "")] == "1")
        check("marker diske işlendi", proxyctl_win.load_backup()[("__winhttp", "")] == "1")

        print("== restore() ==")
        store["ProxyEnable"] = (1, 4)
        store["ProxyServer"] = ("95.211.174.135:3128", 1)
        # AutoConfigURL başlangıçta yoktu -> restore (None) onu silmeli
        store["AutoConfigURL"] = ("http://pac.example/proxy.pac", 1)
        refresh_calls.clear()
        winhttp_reset_calls.clear()
        proxyctl_win.restore(saved)
        check("ProxyEnable geri", store["ProxyEnable"] == (1, 4))
        check("ProxyServer geri", store["ProxyServer"] == ("127.0.0.1:8080", 1))
        check("ProxyOverride geri", store["ProxyOverride"] == ("<local>;192.168.*", 1))
        check("önceden yoksa silinir", "AutoConfigURL" not in store)
        check("refresh çağrıldı", len(refresh_calls) == 1)
        check("WinHTTP reset edildi (biz kurmuştuk)", len(winhttp_reset_calls) == 1)

        print("== restore: kullanıcının kendi WinHTTP'u korunur ==")
        saved2 = proxyctl_win.backup()  # marker "0"
        proxyctl_win.restore(saved2)
        check("marker 0 ise reset YOK", len(winhttp_reset_calls) == 1)

        print("== force_off() (last-resort) ==")
        store["ProxyEnable"] = (1, 4)
        # mevcut disk yedeğindeki marker "0" (saved2) -> winhttp reset yok
        refresh_calls.clear()
        winhttp_reset_calls.clear()
        proxyctl_win.force_off()
        check("ProxyEnable=0", store["ProxyEnable"] == (0, 4))
        check("refresh çağrıldı", len(refresh_calls) == 1)
        check("marker 0 ise winhttp reset YOK", len(winhttp_reset_calls) == 0)
        # marker "1" ile crash sonrası kurtarma
        proxyctl_win.save_backup({("ProxyServer", ""): "x", ("__winhttp", ""): "1"})
        proxyctl_win.force_off()
        check("marker 1 ise winhttp reset var", len(winhttp_reset_calls) == 1)

        print("== netsh gerçek subprocess yolu (arg listesi) ==")
        import subprocess
        proxyctl_win._winhttp_set = real_set
        proxyctl_win._winhttp_reset = real_reset
        real_run = subprocess.run
        calls = []
        def fake_run(*args, **kw):
            calls.append(args[0])
            class R:
                returncode = 0
            return R()
        proxyctl_win.subprocess.run = fake_run
        ok = proxyctl_win._winhttp_set("1.2.3.4:8080")
        check("set proxy arg listesi", calls[-1] == ["netsh", "winhttp", "set", "proxy", "1.2.3.4:8080"])
        check("başarılı -> True", ok is True)
        proxyctl_win._winhttp_reset()
        check("reset proxy arg listesi", calls[-1] == ["netsh", "winhttp", "reset", "proxy"])
        proxyctl_win.subprocess.run = real_run

        print("== clear_backup ==")
        proxyctl_win.clear_backup()
        check("dosya silindi", not os.path.exists(proxyctl_win.BACKUP_FILE))
        check("load_backup None", proxyctl_win.load_backup() is None)

        print("== check_alive() ==")
        from unittest.mock import MagicMock, patch
        with patch("proxyctl_win.socket.create_connection") as m:
            m.return_value = MagicMock()
            check("canlı proxy doğrulanır", proxyctl_win.check_alive(p) is True)
        with patch("proxyctl_win.socket.create_connection", side_effect=OSError("refused")):
            check("ölü proxy elenir", proxyctl_win.check_alive(p) is False)

        print("== protocol kısıtı ==")
        socks = Proxy(host="1.2.3.4", port=1080, protocol="socks5")
        try:
            proxyctl_win.connect(socks, saved)
            check("socks5 bağlanmayı reddeder", False)
        except AssertionError:
            check("socks5 bağlanmayı reddeder", True)

        print("== UI backend seçici (headless, Textual DOM'suz) ==")
        from ui import ProxyApp
        counter = {"collected": 0, "tested": 0, "alive": 0, "dead": 0, "transparent": 0}
        proxyctl_win.clear_backup()  # stale backup kalmasın -> _recover no-op
        app = ProxyApp([], counter, "")
        sys.platform = "win32"
        app._init_proxyctl()
        check("win32 -> proxyctl_win seçildi", app.proxyctl is proxyctl_win)
        check("win32 -> connection_error boş", app.connection_error is None)
        # win32 ama registry erişilemez -> disable + mesaj
        sys.modules["winreg"] = FakeWinreg(store, fail_open=True)
        app2 = ProxyApp([], counter, "")
        app2._init_proxyctl()
        check("win32 erişilemezse disable", app2.connection_error is not None)
        sys.modules["winreg"] = FakeWinreg(store)

        print(f"\n{PASS} test geçti ✓")
    finally:
        if orig_winreg is None:
            sys.modules.pop("winreg", None)
        else:
            sys.modules["winreg"] = orig_winreg
        sys.platform = orig_sys_platform
        proxyctl_win.BACKUP_FILE = orig_backup_file

if __name__ == "__main__":
    run()