"""Textual TUI: live table, filters, detail view, export, retest, system-wide
connect (gsettings/GNOME), fast rotation with failover."""
import asyncio, os, time
from datetime import datetime

from tray import TrayManager

from collector import Proxy, get_country
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Footer, Header, Input, Label, Static

# Country lookup cache (ip-api/ipwho) — module-level, survives repeated enrich
COUNTRY_CACHE = {}

ANON_COLORS = {"elite": "bold green", "anonymous": "yellow", "transparent": "red"}
TRUST_COLORS = {"high": "bold green", "medium": "yellow"}

ANON_FILTERS = ["all", "elite", "anonymous", "transparent"]
PROTO_FILTERS = ["all", "http", "https", "socks4", "socks5"]

COLS = ["IP", "Port", "Proto", "Country", "Anonymity", "HTTPS", "Latency", "Trust", "Source"]


class ConfirmModal(ModalScreen):
    """Confirmation dialog: system proxy settings will change."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    CSS = """
    ConfirmModal > Vertical { align: center middle; }
    #box { width: 62; height: 8; border: thick $primary; background: $surface;
           padding: 1 2; }
    """

    def __init__(self, prompt: str, app_action: str, **kwargs):
        super().__init__(**kwargs)
        self.prompt = prompt
        self.app_action = app_action

    def compose(self) -> ComposeResult:
        with Vertical():
            with Vertical(id="box"):
                yield Static(self.prompt, id="prompt")
                yield Label(" y=confirm  n/esc=cancel      ", id="hint")

    def action_confirm(self) -> None:
        getattr(self.app, f"action_{self.app_action}")()
        self.dismiss()

    def action_cancel(self) -> None:
        self.dismiss()

    def on_key(self, event) -> None:
        if event.key == "y":
            self.action_confirm()
            event.stop()
        elif event.key == "n":
            self.action_cancel()
            event.stop()


class DetailScreen(Screen):
    """Raw test output of the selected proxy."""

    BINDINGS = [
        Binding("escape,q", "app.pop_screen", "Back"),
        Binding("e", "app.export_selected", "Export"),
        Binding("t", "app.retest_selected", "Retest"),
        Binding("c", "app.connect", "Connect"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="detail-body")
        yield Footer()

    def on_mount(self) -> None:
        p = self.app.selected_proxy
        if p is None:
            self.query_one("#detail-body", Static).update("No proxy selected.")
            return
        lines = [
            f"[bold]{p.addr()}[/bold]  proto={p.protocol}  source={p.source or '-'}  "
            f"trust={p.trust}",
            f"status: {p.status}   anonymity: {p.anon or '-'}   https: "
            f"{'yes' if p.https else 'no'}",
            f"latency: {p.latency_ms:.0f} ms   country: {p.country or '-'}",
            f"response IP: {p.ext_ip or '-'}",
            "",
            "[bold]Response headers (anonymity leak check):[/bold]",
        ]
        for k, v in sorted(p.headers.items()):
            lines.append(f"  {k}: {v}")
        self.query_one("#detail-body", Static).update("\n".join(lines))


class ProxyApp(App):
    """Free proxy collector + verifier TUI."""

    TITLE = "ProxyVeil — free proxy collector + verifier + system-wide rotation"
    CSS = """
    #statusbar { height: 1; background: $panel; padding: 0 1; }
    #filters { height: 3; padding: 0 1; }
    Input { width: 14; }
    DataTable { height: 1fr; }
    #help { height: 1; background: $surface; color: $text-muted; padding: 0 1; }
    """
    BINDINGS = [
        Binding("enter", "toggle_test", "Start/Stop Test"),
        Binding("r", "retest_selected", "Retest Selected"),
        Binding("e", "export", "Export Filtered"),
        Binding("d", "show_detail", "Details"),
        Binding("f", "focus_filters", "Filters"),
        # "x" removed — Textual has no Column.visible, column hiding unsupported
        Binding("q", "quit", "Quit"),
        Binding("ctrl+r", "retest_all", "Retest All"),
        Binding("?", "toggle_help", "Help"),
        Binding("c", "connect", "Connect (system)"),
        Binding("n", "next_proxy", "Next Live Proxy"),
        Binding("shift+x", "disconnect", "Disconnect"),
        Binding("l", "toggle_alive", "Live/All"),
        Binding("t", "toggle_trusted", "Trusted Only"),
    ]

    def __init__(self, proxies, counter, real_ip, **kwargs):
        super().__init__(**kwargs)
        self.proxies = proxies            # list[Proxy]
        self.counter = counter            # {"collected": n, "tested": n, "alive": n}
        self.real_ip = real_ip
        self.running = False
        self.test_task = None
        self.selected_proxy = None
        self.anon_filter = "all"
        self.proto_filter = "all"
        self.country_filter = ""
        self.max_latency = 0
        self.trusted_only = False         # T: only high-trust sources
        self.sorted_col = 6               # default: Latency column
        self.sort_asc = True
        self.help_visible = False
        self.concurrency = 50
        self.connected = None          # Proxy | None — active connected proxy
        self.connected_saved = None    # proxyctl.backup() result
        self.connection_error = None
        self.proxyctl = None           # proxyctl module (inject mock for tests)
        self.skip_confirm = False
        self.alive_only = True         # L: live+anonymous only vs all
        self._failover_attempts = 0
        self._monitor_task = None
        self._pending_target = None    # confirm dialog target (avoid re-selection loss)
        self._loop = None              # running asyncio loop (set in on_mount)
        self.tray = TrayManager(self)

    # ---------- compose ----------
    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="statusbar")
        with Horizontal(id="filters"):
            yield Static("[bold]Filter:[/bold]")
            yield Input(placeholder="proto (http/socks4/socks5)", id="f-proto")
            yield Input(placeholder="anon (elite/anonymous)", id="f-anon")
            yield Input(placeholder="country (2 letters)", id="f-country")
            yield Input(placeholder="max latency ms", id="f-latency")
        yield DataTable(id="table", zebra_stripes=True)
        yield Static("", id="help")
        yield Footer()

    async def on_unmount(self) -> None:
        """Restore system proxy settings on exit (esc/ctrl+c/crash)."""
        await self._restore_on_exit()
        self._sync_tray()

    def on_mount(self) -> None:
        self._loop = asyncio.get_running_loop()
        table = self.query_one("#table", DataTable)
        table.cursor_type = "row"
        for name in COLS:
            table.add_column(name, key=name)
        self.refresh_status()
        self.query_one("#f-proto", Input).value = self.proto_filter
        self.query_one("#f-anon", Input).value = self.anon_filter
        self.query_one("#f-country", Input).value = self.country_filter
        self.query_one("#f-latency", Input).value = str(self.max_latency) if self.max_latency else ""
        self.update_help()
        self._init_proxyctl()
        self.refresh_status()
        self._monitor_task = asyncio.create_task(self._monitor_loop())

    def _init_proxyctl(self) -> None:
        """Resolve proxyctl; if unavailable, disable connect + warn."""
        if self.proxyctl is not None:
            return
        try:
            import proxyctl as pc
            if not pc.available():
                self.connection_error = "gsettings missing/non-GNOME — connect disabled, export/project mode"
                return
            self.proxyctl = pc
            self._recover_stale_backup()
        except ImportError:
            self.connection_error = "proxyctl import failed"

    def _recover_stale_backup(self) -> None:
        """A previous session died while connected (crash/kill) — the persisted
        pre-connect backup is still on disk. Restore it now."""
        if self.proxyctl is None:
            return
        saved = self.proxyctl.load_backup()
        if not saved:
            return
        try:
            self.proxyctl.restore(saved)
            self.proxyctl.clear_backup()
            self.notify("Previous session crashed while connected — original proxy "
                        "settings restored", severity="warning")
        except Exception as e:
            self.notify(f"Crash recovery failed: {e}", severity="error")

    def _connected_label(self) -> str:
        if self.connected:
            c = self.connected
            cc = c.country or "?"
            return f"CONNECTED: {c.addr()} ({cc})"
        if self.connection_error:
            return "CONNECT DISABLED"
        return "NOT CONNECTED"

    def _live_proxies(self):
        return [p for p in self.proxies if p.status == "alive" and p.anon != "transparent"]

    # ---------- tray ----------
    def _sync_tray(self) -> None:
        """Single point of tray state: start/stop/update based on self.connected."""
        if self.connected is None:
            self.tray.stop()
            return
        if self._loop is None:
            return
        if not self.tray.start(self._loop):
            self.notify("Tray unavailable (pystray/Pillow missing)", severity="warning")
            return
        self.tray.update()

    # ---------- tray callbacks (run via run_coroutine_threadsafe) ----------
    async def _tray_next(self) -> None:
        """Tray 'Next Server' — like N but no confirm."""
        if self.connected is None:
            self._pending_target = self._next_fastest()
        else:
            self._pending_target = self._next_fastest(exclude=self.connected)
        if self._pending_target is None:
            return
        self.action_do_connect_fastest()

    async def _tray_disconnect(self) -> None:
        self.action_do_disconnect()
        self._sync_tray()

    async def _tray_quit(self) -> None:
        """Tray 'Quit': disconnect + restore, keep TUI open."""
        await self._do_disconnect_quiet()
        self._sync_tray()
        self.tray.stop()

    def _fastest_candidates(self):
        lives = self._live_proxies()
        return [p for p in lives if p.protocol in ("http", "https") and p.https]

    # ---------- events ----------
    def on_input_changed(self, event: Input.Changed) -> None:
        v = event.value.strip().lower()
        mid = event.input.id
        if mid == "f-proto":
            self.proto_filter = v if v in PROTO_FILTERS else ("all" if not v else v)
            if v not in PROTO_FILTERS and v:
                event.input.value = ""  # invalid -> clear
        elif mid == "f-anon":
            self.anon_filter = v if v in ANON_FILTERS else ("all" if not v else v)
            if v not in ANON_FILTERS and v:
                event.input.value = ""
        elif mid == "f-country":
            self.country_filter = v[:2]
        elif mid == "f-latency":
            self.max_latency = int(v) if v.isdigit() else 0
        self.refresh_status()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.input.focus()  # keep focus on filter after enter

    def on_data_table_row_selected(self, event) -> None:
        if event.row_key.value is not None:
            self.selected_proxy = self._proxy_for_row(event.row_key)
            self.push_screen(DetailScreen())

    def on_data_table_header_selected(self, event) -> None:
        try:
            idx = COLS.index(event.column_key.value)
        except ValueError:
            return
        if self.sorted_col == idx:
            self.sort_asc = not self.sort_asc
        else:
            self.sorted_col, self.sort_asc = idx, True
        self.rebuild_table()

    # ---------- helpers ----------
    def _visible(self):
        out = []
        for p in self.proxies:
            if self.trusted_only and p.trust != "high":
                continue
            if self.alive_only and (p.status != "alive" or p.anon == "transparent"):
                continue
            if self.proto_filter != "all" and p.protocol != self.proto_filter:
                continue
            if self.anon_filter != "all" and p.anon != self.anon_filter:
                continue
            if self.country_filter and p.country.lower()[:2] != self.country_filter:
                continue
            if self.max_latency and (not p.latency_ms or p.latency_ms > self.max_latency):
                continue
            if self.alive_only and p.https is False:
                continue  # default live view: hide non-HTTPS
            out.append(p)
        return out

    def _proxy_for_row(self, key):
        for p in self.proxies:
            if id(p) == key.value:
                return p
        return None

    def _on_proxy_done(self, p) -> None:
        """Live counter + table updates per finished proxy."""
        if not self.is_running:
            return  # app closed, DOM gone
        alive = sum(1 for q in self.proxies if q.status == "alive")
        tested = sum(1 for q in self.proxies if q.status != "pending")
        dead = sum(1 for q in self.proxies if q.status == "dead")
        transp = sum(1 for q in self.proxies if q.anon == "transparent")
        self.counter.update({"tested": tested, "alive": alive, "dead": dead,
                             "transparent": transp})
        self.refresh_status()
        self.rebuild_table()

    def refresh_status(self) -> None:
        c = self.counter
        badge = self._connected_label()
        badge = f"[bold green]{badge}[/]" if self.connected else f"[dim]{badge}[/]"
        self.sub_title = self._connected_label()
        self.query_one("#statusbar", Static).update(
            f"{badge}   [bold cyan]collected: {c['collected']}[/]   "
            f"[bold]tested: {c['tested']}[/]   "
            f"[bold green]alive: {c['alive']}[/]   "
            f"[bold yellow]transparent: {c.get('transparent', 0)}[/]   "
            f"[bold red]dead: {c.get('dead', 0)}[/]   "
            f"real IP: {self.real_ip or '-'}   "
            f"filter: proto={self.proto_filter} anon={self.anon_filter} "
            f"country={self.country_filter or '-'} max_ms={self.max_latency or '-'} "
            f"trusted_only={self.trusted_only}   "
            f"{'[bold green]TEST RUNNING[/]' if self.running else '[dim]stopped[/]'}")

    def update_help(self) -> None:
        if self.help_visible:
            txt = ("[bold]Shortcuts:[/] Enter=start/stop test  r=retest selected  "
                   "ctrl+r=retest all  d=details  e=export  f=filters  C=connect  "
                   "N=next fastest  X=disconnect  L=live/all  T=trusted only  "
                   "?=hide help  q=quit")
        else:
            txt = ("Enter=start/stop test  C=connect  N=next fastest  X=disconnect  "
                   "L=live/all  T=trusted only  r=retest  d=details  e=export  "
                   "f=filters  ctrl+r=retest all  ?=help  q=quit")
        self.query_one("#help", Static).update(txt)

    # ---------- table ----------
    def _sort_key(self, p):
        col = self.sorted_col
        if col == 0:
            return (p.host, p.port)
        if col == 1:
            return p.port
        if col == 2:
            return p.protocol
        if col == 3:
            return p.country
        if col == 4:
            return p.anon
        if col == 5:
            return 1 if p.https else 0
        if col == 6:
            return p.latency_ms
        if col == 7:
            return p.trust
        return p.source

    def rebuild_table(self) -> None:
        table = self.query_one("#table", DataTable)
        table.clear()
        rows = sorted(self._visible(), key=self._sort_key, reverse=not self.sort_asc)
        # https first within the sort (default latency asc, https on top)
        if self.sorted_col == 6 and self.sort_asc:
            rows = sorted(rows, key=lambda p: (0 if p.https else 1, p.latency_ms or 99999))
        for p in rows:
            anon = p.anon or ("..." if p.status == "testing" else "-")
            color = ANON_COLORS.get(p.anon, "")
            anon_cell = f"[{color}]{anon}[/]" if color else anon
            https_cell = "yes" if p.https else "no"
            tcolor = TRUST_COLORS.get(p.trust, "")
            trust_cell = f"[{tcolor}]{p.trust}[/]" if tcolor else p.trust
            src = (p.source or "-").split("-")[0]  # shorten: thespeedX-http -> thespeedX
            if p.status == "alive":
                lat = f"{p.latency_ms:.0f} ms" if p.latency_ms else "-"
            elif p.status == "testing":
                lat = "..."
            else:
                lat = "-"
            table.add_row(
                p.host, str(p.port), p.protocol, p.country or "-",
                anon_cell, https_cell, lat, trust_cell, src,
                key=id(p),
            )

    # ---------- connect / rotate ----------
    def _target_from_selected(self):
        p = self.selected_proxy or (self._live_proxies() or [None])[0]
        if p is None:
            self.notify("No live proxy — start the test first (Enter)", severity="warning")
            return None
        if p.protocol not in ("http", "https"):
            self.notify("System proxy only supports HTTP/HTTPS — SOCKS is "
                        "export/project mode only", severity="warning")
            return None
        if p.https is False:
            self.notify("This proxy has no HTTPS CONNECT support — system connect "
                        "would break your internet. Pick one with HTTPS=yes.",
                        severity="warning")
            return None
        if p is not self.selected_proxy:
            self.selected_proxy = p  # keep target after confirm dialog
        return p

    def action_connect(self) -> None:
        if self.proxyctl is None:
            self.notify(self.connection_error or "Connect unavailable", severity="error")
            return
        if self.connected:
            self.notify("Already connected — N to switch, X to disconnect", severity="warning")
            return
        p = self._target_from_selected()
        if p is None:
            return
        if not self.skip_confirm:
            self.push_screen(ConfirmModal(
                f"System proxy settings will change.\nConnect to {p.addr()} ({p.country or '?'})?",
                "do_connect"))
            return
        self.action_do_connect()

    def action_do_connect(self) -> None:
        p = self.selected_proxy
        if p is None or self.proxyctl is None:
            self.notify("No proxy selected to connect", severity="warning")
            return
        self._failover_attempts = 0
        asyncio.create_task(self._verify_and_connect(p))

    async def _verify_and_connect(self, p, attempts_left: int = 3) -> None:
        """Backup -> connect -> verify HTTPS through proxy (3s) -> rollback+next if dead.
        Backup is taken ONCE (first connect) and reused on failover rotation —
        the original pre-connect settings must survive rotation, never be
        overwritten by the proxy's own state."""
        import aiohttp
        from validator import verify_https
        try:
            saved = self.connected_saved if self.connected_saved is not None \
                else self.proxyctl.backup()
            self.proxyctl.connect(p, saved)
            self.connected = p
            self.connected_saved = saved
            self.refresh_status()
            self.notify(f"Connecting to {p.addr()}... verifying", severity="info")
            async with aiohttp.ClientSession(
                    headers={"User-Agent": "ProxyVeil/2.0"}) as s:
                ok = await verify_https(s, p, timeout=3.0)
            if ok:
                self._failover_attempts = 0
                self.refresh_status()
                self.notify(f"Connected: {p.addr()} ({p.country or '?'})")
                self._sync_tray()
                return
            # verify failed -> rollback original, try next fastest
            self.proxyctl.restore(saved)
            self.connected = None
            self.connected_saved = None
            self.proxyctl.clear_backup()
            self.notify(f"{p.addr()} failed HTTPS check — rolling back", severity="error")
            self._sync_tray()
        except Exception as e:
            self.notify(f"Connect failed: {e}", severity="error")
            self._sync_tray()
            return
        if attempts_left > 1 and not self.connected:
            nxt = self._next_fastest(exclude=p)
            if nxt:
                self.notify(f"Trying next fastest: {nxt.addr()}", severity="info")
                await self._verify_and_connect(nxt, attempts_left - 1)

    def _next_fastest(self, exclude=None):
        cands = [p for p in self._fastest_candidates() if p is not exclude]
        if not cands:
            self.notify("No HTTPS-capable live proxy left", severity="error")
            return None
        return min(cands, key=lambda p: (p.latency_ms or 99999))

    def action_next_proxy(self) -> None:
        """N: connect to fastest HTTPS live proxy (auto-connect if not connected)."""
        if self.proxyctl is None:
            self.notify(self.connection_error or "Connect unavailable", severity="error")
            return
        if not self.connected:
            self.action_connect_fastest()
            return
        nxt = self._next_fastest(exclude=self.connected)
        if nxt is None:
            return
        self._pending_target = nxt
        if not self.skip_confirm:
            self.push_screen(ConfirmModal(
                f"Switch to fastest HTTPS proxy?\n{nxt.addr()} ({nxt.country or '?'})",
                "do_connect_fastest"))
            return
        self.action_do_connect_fastest()

    def action_connect_fastest(self) -> None:
        """Connect to the fastest HTTPS-capable live HTTP proxy."""
        if self.proxyctl is None:
            self.notify(self.connection_error or "Connect unavailable", severity="error")
            return
        nxt = self._next_fastest(exclude=self.connected)
        if nxt is None:
            return
        self._pending_target = nxt
        if not self.skip_confirm:
            self.push_screen(ConfirmModal(
                f"Connect to fastest HTTPS proxy?\n{nxt.addr()} ({nxt.country or '?'})",
                "do_connect_fastest"))
            return
        self.action_do_connect_fastest()

    def action_do_connect_fastest(self) -> None:
        nxt = self._pending_target or self._next_fastest(exclude=self.connected)
        if nxt is None:
            return
        self._pending_target = None
        self._failover_attempts = 0
        asyncio.create_task(self._verify_and_connect(nxt))

    async def _monitor_loop(self) -> None:
        """Failover monitor: every 5s verify HTTPS through connected proxy;
        on failure rotate to next fastest (event-driven, not polling loop)."""
        while True:
            await asyncio.sleep(5)
            if not self.is_running:
                break
            if not self.connected or self.proxyctl is None:
                continue
            ok, latency_ms = await self._monitor_check(self.connected)
            if not ok:
                self._failover_attempts += 1
                if self._failover_attempts > 3:
                    self.notify("3 failovers — stopping rotation", severity="error")
                    await self._do_disconnect_quiet()
                    continue
                self.notify(f"Failover: {self.connected.addr()} unreachable — "
                            f"switching to fastest ({self._failover_attempts}/3)",
                            severity="warning")
                nxt = self._next_fastest(exclude=self.connected)
                if nxt:
                    await self._verify_and_connect(nxt)
            else:
                # latency monitor: update tray tooltip with measured latency
                self.tray.update(
                    f"Server: {self.connected.addr()} | Latency: {latency_ms:.0f} ms")

    async def _monitor_check(self, p):
        """HTTPS echo through connected proxy, timed. Returns (ok, latency_ms)."""
        import aiohttp
        from validator import verify_https
        t0 = time.monotonic()
        try:
            async with aiohttp.ClientSession(
                    headers={"User-Agent": "ProxyVeil/2.0"}) as s:
                ok = await verify_https(s, p, timeout=3.0)
        except Exception:
            ok = False
        return ok, (time.monotonic() - t0) * 1000

    async def _do_disconnect_quiet(self) -> None:
        try:
            if self.connected_saved:
                self.proxyctl.restore(self.connected_saved)
                self.proxyctl.clear_backup()
            self.connected = None
            self.connected_saved = None
            self.refresh_status()
            self._sync_tray()
        except Exception as e:
            self.notify(f"Restore failed: {e}", severity="error")

    def action_disconnect(self) -> None:
        if self.proxyctl is None or self.connected is None:
            self.notify("Not connected", severity="warning")
            return
        if not self.skip_confirm:
            self.push_screen(ConfirmModal(
                "System proxy settings will return to previous values.\nDisconnect?",
                "do_disconnect"))
            return
        self.action_do_disconnect()

    def action_do_disconnect(self) -> None:
        if self.connected is None:
            return
        asyncio.create_task(self._do_disconnect_quiet())
        self.notify("Disconnected, previous settings restored")

    def action_toggle_alive(self) -> None:
        self.alive_only = not self.alive_only
        self.rebuild_table()
        self.refresh_status()
        self.notify("Live + anonymous" if self.alive_only else "All (dead/transparent included)")

    def action_toggle_trusted(self) -> None:
        self.trusted_only = not self.trusted_only
        self.rebuild_table()
        self.refresh_status()
        self.notify("Trusted sources only (high)" if self.trusted_only else "All sources")

    async def _restore_on_exit(self) -> None:
        """On exit (esc/ctrl+c/crash) restore previous settings. Never fail silently."""
        if self.connected and self.proxyctl and self.connected_saved:
            try:
                self.proxyctl.restore(self.connected_saved)
                self.proxyctl.clear_backup()
                print(f"[proxyctl] restored on exit: mode -> "
                      f"{self.connected_saved.get(('', 'mode'), '?')}")
            except Exception as e:
                print(f"[proxyctl] EXIT RESTORE FAILED — fix manually: {e}")
                # last resort: subprocess.run with arg list (no shell)
                import subprocess
                subprocess.run(["gsettings", "set", self.proxyctl.SCHEMA, "mode",
                                self.connected_saved.get(("", "mode"), "'none'")],
                               capture_output=True)
        self._sync_tray()

    # ---------- actions ----------
    def action_toggle_test(self) -> None:
        if self.running:
            self.running = False
            if self.test_task:
                self.test_task.cancel()
            self.refresh_status()
        else:
            self.running = True
            self.refresh_status()
            self.test_task = asyncio.create_task(self._run_tests())

    async def _run_tests(self) -> None:
        from validator import run_batch
        pending = [p for p in self.proxies if p.status in ("pending", "dead")]
        try:
            await run_batch(pending, self.real_ip or "", self.concurrency, on_done=self._on_proxy_done)
        except asyncio.CancelledError:
            return
        self.running = False
        self.counter.update({
            "tested": sum(1 for p in self.proxies if p.status != "pending"),
            "alive": sum(1 for p in self.proxies if p.status == "alive"),
            "dead": sum(1 for p in self.proxies if p.status == "dead"),
            "transparent": sum(1 for p in self.proxies if p.anon == "transparent"),
        })
        self.refresh_status()
        self.rebuild_table()
        await self._enrich_countries()

    async def _enrich_countries(self) -> None:
        """Fill countries for live proxies via ip-api/ipwho chain."""
        import aiohttp
        async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as s:
            for p in self.proxies:
                if p.status == "alive" and not p.country:
                    p.country = await get_country(p, s, COUNTRY_CACHE)
        self.rebuild_table()

    def action_retest_selected(self) -> None:
        if self.selected_proxy:
            asyncio.create_task(self._retest([self.selected_proxy]))

    def action_retest_all(self) -> None:
        asyncio.create_task(self._retest(list(self.proxies)))

    async def _retest(self, targets) -> None:
        from validator import run_batch
        for p in targets:
            p.status = "pending"
        self.running = True
        self.refresh_status()
        await run_batch(targets, self.real_ip or "", self.concurrency, on_done=self._on_proxy_done)
        self.running = False
        self.counter["tested"] = sum(1 for p in self.proxies if p.status != "pending")
        self.counter["alive"] = sum(1 for p in self.proxies if p.status == "alive")
        self.counter["dead"] = sum(1 for p in self.proxies if p.status == "dead")
        self.counter["transparent"] = sum(1 for p in self.proxies if p.anon == "transparent")
        self.refresh_status()
        self.rebuild_table()

    def action_export(self) -> None:
        rows = [p for p in self._visible() if p.status == "alive" and p.anon != "transparent"]
        if not rows:
            self.notify("No live proxy to export", severity="warning")
            return
        fname = f"proxies_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        with open(fname, "w") as f:
            for p in rows:
                f.write(f"{p.addr()}\n")
        self.notify(f"{len(rows)} proxies -> {fname}")

    def action_export_selected(self) -> None:
        if self.selected_proxy:
            fname = f"proxies_{self.selected_proxy.addr().replace(':', '_')}.txt"
            with open(fname, "w") as f:
                f.write(self.selected_proxy.addr() + "\n")
            self.notify(f"export -> {fname}")

    def action_show_detail(self) -> None:
        if self.selected_proxy:
            self.push_screen(DetailScreen())

    def action_focus_filters(self) -> None:
        self.query_one("#f-proto", Input).focus()

    def action_toggle_col(self) -> None:
        # Textual has no Column.visible — column hiding unsupported (removed).
        self.notify("Column toggle unsupported in Textual — removed", severity="warning")

    def action_toggle_help(self) -> None:
        self.help_visible = not self.help_visible
        self.update_help()

    def on_data_table_row_highlighted(self, event) -> None:
        if event.row_key.value is not None:
            p = self._proxy_for_row(event.row_key)
            if p:
                self.selected_proxy = p
