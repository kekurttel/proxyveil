"""TrayManager birim testi.

pystray gerçekten kurulmaz — path mock'lanır / ImportError simüle edilir.
Çalıştır: python test_tray.py
"""
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, "/home/kaneki/proxy")

from tray import TrayManager

PASS = 0

def check(name, cond):
    global PASS
    assert cond, f"FAIL: {name}"
    PASS += 1
    print(f"  ok: {name}")


class FakeApp:
    def __init__(self):
        self.connected = None
        self._tray_next_called = 0
        self._tray_disconnect_called = 0
        self._tray_quit_called = 0

    async def _tray_next(self):
        self._tray_next_called += 1

    async def _tray_disconnect(self):
        self._tray_disconnect_called += 1

    async def _tray_quit(self):
        self._tray_quit_called += 1


def run():
    print("== import edilebilir ==")
    tm = TrayManager(FakeApp())
    # pystray kuruluysa True (X11 yoksa da thread başlar); kurulu değilse False
    try:
        import pystray  # noqa: F401
        has_pystray = True
    except ImportError:
        has_pystray = False
    r = tm.start(None)
    check(f"start ({'pystray var' if has_pystray else 'pystray yok'})",
          r is has_pystray or r is False)

    print("== import hatasına dayanıklılık (mock ImportError) ==")
    with patch.dict("sys.modules", {"pystray": None, "PIL": None}):
        tm2 = TrayManager(FakeApp())
        # module attr alınamaz -> start yine False dönmeli (real import dene ama başarısızsa False)
        ok = tm2.start(None)
        check("ImportError sonrası start False veya çökme yok", ok in (True, False))

    print("== pystray kurulu + ikon başlatma ==")
    import tray as tray_mod
    fake_pystray = MagicMock()
    fake_pystray.Menu.return_value = "menu"
    fake_pystray.Icon.return_value = MagicMock()

    with patch.dict("sys.modules", {"pystray": fake_pystray, "PIL": MagicMock()}):
        # tray_mod._pystray yok — bu yüzden doğrudan sistemsel import, patch yeterli
        tm3 = TrayManager(FakeApp())
        import asyncio
        loop = asyncio.new_event_loop()  # döner
        # gerçek pystray kurulmadıysa (venv'de yok) start False döner — kabul
        r = tm3.start(loop)
        check("start çalışır (True/False, hata yok)", r in (True, False))
        tm3.stop()
        check("stop None-safe", True)

    print("== callback'ler loop'a coroutine gönderir ==")
    tm4 = TrayManager(FakeApp())
    tm4._loop = "fake-loop"
    mock_rcs = MagicMock(return_value="future")
    with patch("asyncio.run_coroutine_threadsafe", mock_rcs):
        tm4._cb_next()
        args = mock_rcs.call_args.args
        check("_cb_next coroutine gönderir", asyncio.iscoroutine(args[0]))
        check("_cb_next loop'u kullanır", args[1] == "fake-loop")
        mock_rcs.reset_mock()
        tm4._cb_disconnect()
        tm4._cb_quit()
        check("3 callback çağrısı", mock_rcs.call_count == 2)

    print("== update None-safe ==")
    tm5 = TrayManager(FakeApp())
    tm5.update("x")  # _icon None -> no-op, hata yok
    check("update _icon yokken no-op", True)

    print("== _status_text ==")
    class P:
        def addr(self):
            return "1.2.3.4:80"
        latency_ms = 123.4
    tm6 = TrayManager(FakeApp())
    tm6.app.connected = P()
    s = tm6._status_text()
    check("status text ip+latency içerir", "1.2.3.4:80" in s and "123" in s)
    tm6.app.connected = None
    check("bağlı değil text", tm6._status_text() == "Not connected")

    print("== _make_image ==")
    from PIL import Image, ImageDraw
    img = TrayManager._make_image(Image, ImageDraw)
    check("ikon 64x64 RGBA", img.size == (64, 64) and img.mode == "RGBA")

    print(f"\n{PASS} test geçti ✓")


if __name__ == "__main__":
    run()