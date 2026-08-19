"""Headless smoke test: kaynak çek -> TUI kur -> test (sınırlı) -> bağlan simülasyonu
-> export -> SVG. Hızlı: ~2-3 dk. gsettings gerçekten kurulmaz (FakeCtl ile mock).

Çalıştır: source .venv/bin/activate && python smoke_test.py
"""
import asyncio, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import aiohttp

from collector import Proxy, collect
from main import get_real_ip

OUT = "smoke_out"
os.makedirs(OUT, exist_ok=True)

MAX_PROXIES = 300      # hız: tüm 10k'yı test etme
ALIVE_TARGET = 20
MAX_WAIT = 120         # saniye


class FakeCtl:
    """proxyctl mock: gerçek gsettings'e dokunmaz, çağrıları kaydeder."""
    SCHEMA = "org.gnome.system.proxy"
    saved = None

    @classmethod
    def available(cls):
        return True

    @classmethod
    def backup(cls):
        cls.saved = {("", "mode"): "'auto'", ("http", "host"): "'localhost'",
                     ("http", "port"): "0", ("https", "host"): "'localhost'",
                     ("https", "port"): "0", ("", "ignore-hosts"): "'['localhost', '127.0.0.1']'"}
        return dict(cls.saved)

    @classmethod
    def connect(cls, p, saved):
        assert p.protocol == "http", "sadece http"
        cls.saved[("", "mode")] = "'manual'"
        cls.saved[("http", "host")] = f"'{p.host}'"
        cls.saved[("http", "port")] = str(p.port)
        cls.saved[("https", "host")] = f"'{p.host}'"
        cls.saved[("https", "port")] = str(p.port)

    @classmethod
    def restore(cls, saved):
        cls.saved = dict(saved)

    @classmethod
    def check_alive(cls, p, timeout=2.0):
        return True  # testte her canlıya izin ver


async def main():
    print("[1/5] gerçek IP tespiti...")
    async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"}) as s:
        real_ip = await get_real_ip(s)
    print(f"      gerçek IP: {real_ip}")

    print("[2/5] kaynak çek...")
    proxies = await collect()
    proxies = proxies[:MAX_PROXIES]
    print(f"      {len(proxies)} proxy test edilecek (ilk {MAX_PROXIES})")
    if not proxies:
        print("HATA: proxy toplanamadı")
        return 1

    print("[3/5] TUI kurulumu (headless, FakeCtl)...")
    from ui import ProxyApp
    counter = {"collected": len(proxies), "tested": 0, "alive": 0, "dead": 0, "transparent": 0}
    app = ProxyApp(proxies, counter, real_ip)
    app.concurrency = 200
    app.proxyctl = FakeCtl            # gerçek gsettings'e asla dokunma
    app.skip_confirm = True           # onay modal'ını atla
    run_task = asyncio.create_task(app.run_async(headless=True, size=(110, 35)))
    try:
        await asyncio.sleep(1.5)
        app.action_toggle_test()
        deadline = time.monotonic() + MAX_WAIT
        while time.monotonic() < deadline and counter["alive"] < ALIVE_TARGET:
            await asyncio.sleep(2)
        # test bitmesini bekle
        while app.running and time.monotonic() < deadline:
            await asyncio.sleep(2)
        await app._enrich_countries()

        tested = sum(1 for p in proxies if p.status != "pending")
        alive = sum(1 for p in proxies if p.status == "alive")
        dead = sum(1 for p in proxies if p.status == "dead")
        transp = sum(1 for p in proxies if p.anon == "transparent")
        print(f"[4/5] SONUÇ: test edildi={tested} canlı={alive} ölü={dead} transparent={transp}")
        if alive < ALIVE_TARGET:
            print(f"UYARI: {alive} canlı (hedef {ALIVE_TARGET})")

        # --- bağlanma simülasyonu (FakeCtl) ---
        print("      connect simulation (FakeCtl — gsettings UNCHANGED)...")
        lives = [p for p in proxies if p.status == "alive" and p.anon != "transparent"]
        http_lives = [p for p in lives if p.protocol == "http"]
        if http_lives:
            app.selected_proxy = http_lives[0]
            app.action_connect()                     # C
            for _ in range(20):                      # async verify beklenir (max ~10s)
                if app.connected:
                    break
                await asyncio.sleep(0.5)
            assert app.connected is http_lives[0], "C: not connected"
            print(f"      C connected: {app.connected.addr()}  mode="
                  f"{FakeCtl.saved[('', 'mode')]}")
            app.action_next_proxy()                  # N
            await asyncio.sleep(2)
            if app.connected:
                print(f"      N switched: {app.connected.addr()}")
            app.action_toggle_alive()                # L
            print(f"      L filter: alive_only={app.alive_only}")
            app.action_toggle_alive()
            app.action_disconnect()                  # X
            await asyncio.sleep(1)
            assert app.connected is None, "X: not disconnected"
            print(f"      X disconnected, restore mode={FakeCtl.saved[('', 'mode')]}")

        # export (canlı + non-transparent)
        rows = [p for p in proxies if p.status == "alive" and p.anon != "transparent"]
        exp = os.path.join(OUT, "proxies_export.txt")
        with open(exp, "w") as f:
            for p in rows[:50]:
                f.write(f"{p.addr()}\n")
        print(f"      export: {exp} ({len(rows)} satır)")
        for p in rows[:6]:
            print(f"        {p.addr():<24} {p.protocol:<6} {p.anon:<11} "
                  f"https={'Y' if p.https else 'N'} {p.latency_ms:.0f}ms country={p.country or '?'}")

        print("[5/5] SVG ekran görüntüsü...")
        svg = os.path.join(OUT, "tui.svg")
        with open(svg, "w") as f:
            f.write(app.export_screenshot())
        print(f"      {svg} yazıldı ({os.path.getsize(svg)} bayt)")
        with open(svg) as f:
            content = f.read()
        print(f"      svg içerik kontrolü: "
              f"{'OK' if ('Proxy' in content or 'IP' in content) else 'UYARI'}")
    finally:
        app.exit()
        await run_task
    return 0 if alive >= ALIVE_TARGET else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))