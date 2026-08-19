# ProxyVeil

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Main](https://img.shields.io/badge/branch-main-brightgreen)

Free public proxy collector → anonymity/security verifier → Textual TUI with
system-wide connect (GNOME/gsettings) and fast HTTPS-first rotation.

Collects public free proxy lists, classifies each proxy by anonymity level,
verifies HTTPS support and latency, then lets you browse the results in a
live-updating TUI — including one-key system-wide proxy connect for GNOME.

> Terminal-based free-proxy collector, verifier, and system-wide rotator.
> Collect → validate (anonymity, HTTPS CONNECT, latency) → connect system-wide
> → rotate with one key / tray menu. Uses only public free-proxy sources.

## Features

- **Multi-source collection** — high- and medium-trust public lists (monosans,
  proxifly, thespeedX), HTTP/SOCKS4/SOCKS5, deduped by `ip:port`+protocol
- **Anonymity classification** — elite / anonymous / transparent (leakers dropped)
- **HTTPS verification** — CONNECT tunnel test, HTTPS-capable proxies prioritized
- **Latency measurement** — average of 3 repeats, live sorting
- **Textual TUI** — live table, filters, detail view, retest, export
- **System-wide connect** — GNOME/gsettings with backup/restore, failover rotation
- **System tray** — status + latency tooltip, "Next Server" rotation from tray
- **Trust filter** — `--trusted-only` restricts to high-trust sources

## Install

```bash
git clone https://github.com/kekurttel/proxyveil
cd ~/proxyveil
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

### Commands

```bash
python main.py                          # collect; Enter starts the test
python main.py --auto                   # collect + auto-start test
python main.py --trusted-only           # only high-trust sources (monosans/proxifly)
python main.py --concurrency 100        # concurrent tests (default 50)
python main.py --no-countries           # skip country detection
```

### TUI shortcuts

| Key | Action |
|---|---|
| `Enter` | start/stop test |
| `C` | connect to selected HTTP/HTTPS proxy (confirmation required) |
| `N` | switch to next fastest HTTPS-capable live proxy (TCP check; auto-connects if not connected) |
| `X` | disconnect, restore previous settings |
| `L` | live+anonymous / all (dead+transparent included) |
| `T` | trusted-only (high sources) / all sources |
| `r` | retest selected |
| `ctrl+r` | retest all |
| `d` | detail view (raw headers, timings, response IP) |
| `e` | export filtered list (`proxies_YYYYmmdd_HHMMSS.txt`, `ip:port` lines) |
| `f` | focus filters |
| `?` | help |
| `q` | quit |

Filters: protocol, anonymity, country (2 letters), max latency (ms).

## Sources

| Source | Protocols | Trust | Type |
|---|---|---|---|
| monosans/proxy-list | http, socks4, socks5 | high (hourly-verified) | raw `ip:port` |
| monosans/proxy-list proxies.json | http, socks4, socks5 | high | JSON with country |
| proxifly protocol files | http, socks4, socks5 | high | raw `scheme://ip:port` |
| thespeedX/proxy-list | http, socks4, socks5 | medium (unverified) | raw `ip:port` |

Dedup by `ip:port`+protocol; invalid lines silently dropped. `--trusted-only`
filters to high sources only.

## Verification

Per proxy (1 retry, then dead):

1. **Liveness** — echo over proxy (5s connect / 10s request)
2. **Anonymity** — response headers + response IP:
   - `elite`: no real IP leak, no forwarding headers
   - `anonymous`: `X-Forwarded-For` etc present, no real IP
   - `transparent`: real IP leaks → dropped from exports
3. **HTTPS** — CONNECT tunnel to https echo (2 retries for CONNECT flakiness)
4. **Latency** — average of 3 repeats (ms)

HTTPS-capable proxies are tested first (prioritized) and shown on top by default
(live view hides non-HTTPS; `L` shows all).

Echo services (public, return only IP, never credentials/cookies): `api64.ipify.org`,
`wtfismyip.com`, `ipinfo.io`, `ifconfig.me`. Country: `ip-api.com` → `ipwho.is`
chain (proxy IP queried).

## System-wide connect (GNOME/gsettings)

`C` connects to the selected **HTTP/HTTPS** proxy (https host/port set to the same
values). SOCKS proxies don't work system-wide — they stay in export/project mode
(a warning is shown).

- Before connecting, current settings are backed up: `mode`, `http.host/port/enabled`,
  `https.host/port`, `ignore-hosts` (preserved, untouched).
- After connecting, the proxy is **verified** via HTTPS echo (3s); on failure it
  rolls back and tries the next fastest (max 3 attempts).
- `N`: connects to the fastest HTTPS-capable live proxy (TCP check first; skips dead).
- **Failover**: a background monitor checks the connected proxy every 5s via HTTPS
  echo; on failure it auto-rotates to the next fastest (max 3 failovers).
- `X`: restores backed-up values (`mode` returns to previous).
- **On exit (esc/ctrl+c/crash) settings are auto-restored** (`finally`-guaranteed).
- No `gsettings` / non-GNOME: header shows "CONNECT DISABLED", `C/N/X` warn —
  the tool falls back to export/project mode.

**Which apps are affected:** system proxy is honored by GNOME-aware apps —
GNOME Web (Epiphany), Firefox (if set to use system settings), Snap packages,
gio-based apps (glib-networking). Chromium/Chrome use their own proxy settings.
Terminal tools use `http_proxy`/`https_proxy` env vars — unaffected by gsettings.

## System tray

When you connect (`C` or `N`), a tray icon appears (green circle + arrow) with:

- **Status tooltip** — `Server: ip:port | Latency: X ms` (updated by the 5s
  failover monitor)
- **Next Server →** — rotates to the fastest HTTPS-capable live proxy
  (same as `N`, no confirmation — tray rotation is meant to be frictionless)
- **Disconnect** — restores previous system settings
- **Quit Tray** — disconnects + restores, keeps the TUI open

Requires `pystray`, `Pillow`, `python-xlib` (in `requirements.txt`). If they are
missing, the app runs headless — tray simply doesn't start and you get a
warning on connect.

> **GNOME note:** stock GNOME Shell has no tray area. Install the
> "AppIndicator and KStatusNotifierItem Support" extension (pre-installed on
> Ubuntu). Wayland needs the same extension.

## Security & ethics

- Public free lists only; no paid services, no botnets.
- The tool never sends credentials/cookies/personal data through proxies
  (echo services only).
- Header leak check is active; transparent proxies are excluded from exports.
- Respect target-site rate limits.
- System-wide proxy changes are backed up and always restored on exit —
  including on crash.

## Privacy

These are public free proxies operated by unknown third parties. **Never send
credentials, cookies, or personal data through them.** This tool only tests
connectivity via echo endpoints (IP-only services) and never sends anything
else through the proxy.

## Tests

```bash
source .venv/bin/activate
python test_validator.py     # anonymity classification + https normalize + collector (assert-based)
python test_proxyctl.py      # backup/restore logic (gsettings mocked, never touches real settings)
python test_tray.py          # TrayManager: import-resistance, callbacks, icon drawing
python smoke_test.py         # headless: 300 proxies, 20+ alive, FakeCtl connect simulation, export, SVG
```

## License

MIT — see [LICENSE](LICENSE).
