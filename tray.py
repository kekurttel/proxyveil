"""System tray (pystray) for ProxyVeil.

Runs its own daemon thread. UI state updates arrive via
asyncio.run_coroutine_threadsafe(app_loop). Never touches Textual widgets
from the tray thread.

Import-error resistant: if pystray/Pillow is missing, start() returns False
and the app continues headless.
"""
import asyncio
import threading


class TrayManager:
    """System tray (pystray). Runs its own daemon thread."""

    def __init__(self, app):
        self.app = app          # ProxyApp instance (for UI callbacks)
        self._icon = None       # pystray.Icon
        self._thread = None
        self._loop = None       # app's asyncio loop (set in start())

    # ---------- lifecycle ----------
    def start(self, loop) -> bool:
        """Start tray in a daemon thread. False if pystray/Pillow missing."""
        if self._thread and self._thread.is_alive():
            return True
        try:
            import pystray
            from PIL import Image, ImageDraw
        except ImportError:
            return False
        self._loop = loop
        try:
            icon = pystray.Icon(
                "ProxyVeil", self._make_image(Image, ImageDraw),
                "ProxyVeil", self._build_menu())
        except Exception:
            return False
        self._icon = icon
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def _run(self):
        # pystray.Icon.run() blocks until stop()
        try:
            self._icon.run()
        except Exception:
            pass

    def stop(self):
        """Stop the icon safely (None-guarded)."""
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
            self._icon = None

    # ---------- drawing ----------
    @staticmethod
    def _make_image(Image, ImageDraw):
        """Simple green circle + arrow icon (PIL drawing, no heavy graphics)."""
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((8, 8, 56, 56), fill=(46, 204, 113, 255))       # green circle
        d.polygon([(32, 18), (46, 32), (38, 32), (38, 46), (26, 46),
                   (26, 32), (18, 32)], fill=(255, 255, 255, 255))  # arrow
        return img

    # ---------- menu ----------
    def _build_menu(self):
        import pystray
        status = self._status_text()
        return pystray.Menu(
            pystray.MenuItem(status, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Next Server →", self._cb_next),
            pystray.MenuItem("Disconnect", self._cb_disconnect),
            pystray.MenuItem("Quit Tray", self._cb_quit),
        )

    def _status_text(self) -> str:
        c = self.app.connected
        if c is None:
            return "Not connected"
        lat = f"{c.latency_ms:.0f} ms" if c.latency_ms else "-"
        return f"Server: {c.addr()} | Latency: {lat}"

    def update(self, status_text: str = None):
        """Update tooltip + menu (thread-safe: pystray manages its own event loop)."""
        if self._icon is None:
            return  # tray not running — no-op
        try:
            self._icon.title = status_text or self._status_text()
            self._icon.menu = self._build_menu()
            self._icon.update_menu()
        except Exception:
            pass

    # ---------- callbacks (tray thread!) ----------
    def _cb_next(self, icon=None, item=None):
        if self._loop and self.app is not None:
            asyncio.run_coroutine_threadsafe(self.app._tray_next(), self._loop)

    def _cb_disconnect(self, icon=None, item=None):
        if self._loop and self.app is not None:
            asyncio.run_coroutine_threadsafe(self.app._tray_disconnect(), self._loop)

    def _cb_quit(self, icon=None, item=None):
        if self._loop and self.app is not None:
            asyncio.run_coroutine_threadsafe(self.app._tray_quit(), self._loop)