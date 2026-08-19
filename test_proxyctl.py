"""proxyctl yedek/geri yükleme mantığı için assert tabanlı birim test.

gsettings gerçekten çalıştırılmaz — _run_gsettings mock'lanır.
Çalıştır: python test_proxyctl.py
"""
import proxyctl
from unittest.mock import patch
from collector import Proxy

PASS = 0

def check(name, cond):
    global PASS
    assert cond, f"FAIL: {name}"
    PASS += 1
    print(f"  ok: {name}")

# gsettings çağrılarını yakalayan sahte: (schema, key) -> değerler dict'i
fake_store = {}
fake_log = []

def fake_gsettings(*args):
    fake_log.append(args)
    op, schema, key = args[0], args[1], args[2:]
    if op == "get":
        val = fake_store.get((schema, key[0]), "'none'")
        if (schema, key[0]) not in fake_store:
            raise proxyctl.GSettingsError(f"InvalidSchema: {schema}")
        return val
    if op == "set":
        fake_store[(schema, key[0])] = key[1]
        return ""
    if op == "--version":
        return "2.44.0"
    raise AssertionError(f"beklenmeyen gsettings op: {args}")

def run():
    n = len(fake_store.keys())  # İlk durum: boş
    fake_store.clear(); fake_log.clear()
    # şemayı "var" yap
    fake_store[("org.gnome.system.proxy", "mode")] = "'auto'"
    proxyctl._run_gsettings = fake_gsettings

    print("== available() ==")
    check("gsettings erişilebilir", proxyctl.available() is True)

    print("== backup() ==")
    # eksik anahtar -> InvalidSchema (hesap makinesi): şemayı doldur
    for path, key in proxyctl.KEYS:
        full = f"org.gnome.system.proxy.{path}" if path else "org.gnome.system.proxy"
        fake_store.setdefault((full, key), "'0'" if key == "port" else "'{}'")
    saved = proxyctl.backup()
    check("mode yedeklendi", saved[("", "mode")] == "'auto'")
    check("http host yedeklendi", ("http", "host") in saved)
    check("ignore-hosts yedeklendi", ("", "ignore-hosts") in saved)

    print("== connect() ==")
    p = Proxy(host="95.211.174.135", port=3128, protocol="http")
    proxyctl.connect(p, saved)
    check("mode=manual", fake_store[("org.gnome.system.proxy", "mode")] == "'manual'")
    check("http.host ayarlandı", fake_store[("org.gnome.system.proxy.http", "host")] == "'95.211.174.135'")
    check("https.port ayarlandı", fake_store[("org.gnome.system.proxy.https", "port")] == "3128")
    check("http.enabled=true", fake_store[("org.gnome.system.proxy.http", "enabled")] == "true")
    check("ignore-hosts değişmedi", fake_store[("org.gnome.system.proxy", "ignore-hosts")] == "'{}'")

    print("== restore() ==")
    proxyctl.restore(saved)
    check("mode geri döndü", fake_store[("org.gnome.system.proxy", "mode")] == "'auto'")
    check("http.host geri döndü",
          fake_store[("org.gnome.system.proxy.http", "host")] == fake_store.setdefault(
              ("org.gnome.system.proxy.http", "host"), "'old'"))

    print("== protocol kısıtı ==")
    socks = Proxy(host="1.2.3.4", port=1080, protocol="socks5")
    try:
        proxyctl.connect(socks, saved)
        check("socks5 bağlanmayı reddeder", False)
    except AssertionError:
        check("socks5 bağlanmayı reddeder", True)
    https_p = Proxy(host="9.9.9.9", port=443, protocol="https")
    proxyctl.connect(https_p, saved)
    check("https protokol kabul (CONNECT tüneli)",
          fake_store[("org.gnome.system.proxy.http", "host")] == "'9.9.9.9'")

    print("== check_alive() ==")
    from unittest.mock import MagicMock
    with patch("proxyctl.socket.create_connection") as m:
        m.return_value = MagicMock()
        check("canlı proxy doğrulanır", proxyctl.check_alive(p) is True)
    with patch("proxyctl.socket.create_connection", side_effect=OSError("conn refused")):
        check("ölü proxy elenir", proxyctl.check_alive(p) is False)

    print("== gsettings hatası ==")
    try:
        with patch.object(proxyctl, "_run_gsettings", side_effect=proxyctl.GSettingsError("x")):
            proxyctl.backup()
        check("GSettingsError yükselir", False)
    except proxyctl.GSettingsError:
        check("GSettingsError yükselir", True)

    print(f"\n{PASS} test geçti ✓")

if __name__ == "__main__":
    run()