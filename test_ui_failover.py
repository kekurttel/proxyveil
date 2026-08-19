"""Failover regression: rotation must NOT overwrite the original backup.

Old bug: _verify_and_connect re-ran backup() on every rotation, capturing the
proxy's own manual-mode settings as "previous". Original settings were lost;
disconnect/exit restored a dead proxy instead (Q -> manual/dead, or none).

New rule: backup() runs ONCE (first connect). Rotations reuse the original.
Run: python test_ui_failover.py
"""
import asyncio, sys, time, os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from collector import Proxy
import validator

PASS = 0

def check(name, cond):
    global PASS
    assert cond, f"FAIL: {name}"
    PASS += 1
    print(f"  ok: {name}")

ORIGINAL = {("", "mode"): "'auto'", ("http", "host"): "'localhost'",
            ("http", "port"): "0", ("https", "host"): "'localhost'",
            ("https", "port"): "0", ("", "ignore-hosts"): "['localhost']"}

class FakeCtl:
    """proxyctl double: counts backup() calls, records connect/restore args."""
    backup_calls = 0
    connects = []
    restores = []

    @classmethod
    def available(cls):
        return True

    @classmethod
    def backup(cls):
        cls.backup_calls += 1
        return dict(ORIGINAL)

    @classmethod
    def connect(cls, p, saved):
        cls.connects.append((p.addr(), dict(saved)))

    @classmethod
    def restore(cls, saved):
        cls.restores.append(dict(saved))

    @classmethod
    def load_backup(cls):
        return None

    @classmethod
    def clear_backup(cls):
        pass

    @classmethod
    def check_alive(cls, p, timeout=2.0):
        return True

async def _run():
    from ui import ProxyApp

    # HTTPS-capable live proxies for rotation
    p1 = Proxy(host="1.1.1.1", port=3128, protocol="http", status="alive", https=True, latency_ms=50)
    p2 = Proxy(host="2.2.2.2", port=8080, protocol="http", status="alive", https=True, latency_ms=20)
    counter = {"collected": 2, "tested": 2, "alive": 2, "dead": 0, "transparent": 0}

    validator.verify_https = None  # assert offline: would fail hard if network used
    async def _fake_verify(client, p, timeout=3.0):
        return True
    validator.verify_https = _fake_verify

    app = ProxyApp([p1, p2], counter, "")
    app.proxyctl = FakeCtl
    app.skip_confirm = True
    run_task = asyncio.create_task(app.run_async(headless=True, size=(110, 35)))
    try:
        await asyncio.sleep(1.0)

        print("== tek bağlantı: backup 1 kez ==")
        app.selected_proxy = p1
        app.action_connect()
        for _ in range(40):
            if app.connected:
                break
            await asyncio.sleep(0.05)
        check("bağlandı", app.connected is p1)
        check("ilk connect backup aldı", FakeCtl.backup_calls == 1)

        print("== failover: orijinal backup korunur ==")
        await app._verify_and_connect(p2)          # monitor failover path (N/ölüm)
        check("failover backup YENİDEN ALMADI", FakeCtl.backup_calls == 1)
        check("failover yeni proxy'ye bağlandı", app.connected is p2)
        saved_used = FakeCtl.connects[-1][1]
        check("failover orijinali kullandı", saved_used[("", "mode")] == "'auto'")
        check("restore henüz yok", FakeCtl.restores == [])

        print("== disconnect: orijinal geri döner ==")
        await app._do_disconnect_quiet()
        check("bağlantı koptu", app.connected is None)
        check("orijinal restore edildi", FakeCtl.restores[-1][("", "mode")] == "'auto'")

        print("== crash kurtarma: stale backup geri yüklenir ==")
        class RecoveryCtl(FakeCtl):
            @classmethod
            def load_backup(cls):
                return dict(ORIGINAL)          # önceki oturum öldü, dosya kaldı
            @classmethod
            def clear_backup(cls):
                cls.cleared = True
        app.proxyctl = RecoveryCtl
        app._recover_stale_backup()
        check("stale backup restore edildi", RecoveryCtl.restores[-1][("", "mode")] == "'auto'")
        check("clear_backup çağrıldı", getattr(RecoveryCtl, "cleared", False) is True)
        class NoBackupCtl(FakeCtl):
            @classmethod
            def load_backup(cls):
                return None
        app.proxyctl = NoBackupCtl
        before = len(FakeCtl.restores)
        app._recover_stale_backup()
        check("dosya yoksa dokunulmaz", len(FakeCtl.restores) == before)
    finally:
        app.exit()
        await run_task

def main():
    asyncio.run(_run())
    print(f"\n{PASS} test geçti ✓")

if __name__ == "__main__":
    main()