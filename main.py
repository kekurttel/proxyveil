#!/usr/bin/env python3
"""ProxyVeil: free proxy collection + anonymity/security verification + TUI."""
import argparse, asyncio, sys

import aiohttp

from collector import Proxy, collect, get_country

COUNTRY_CACHE = {}


async def get_real_ip(session):
    """Learn our real IP via the echo chain (transparent detection)."""
    from validator import ECHO_URLS
    for url in ECHO_URLS:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status == 200:
                    body = (await r.text(errors="replace")).strip()
                    if "{" in body:
                        try:
                            data = await r.json()
                            return str(data.get("ip") or data.get("YourFuckingIPAddress") or "").strip()
                        except Exception:
                            return body
                    return body.strip()
        except Exception:
            continue
    return ""


async def enrich_countries(proxies):
    """Fill countries for live proxies via ip-api/ipwho chain (proxy IP queried)."""
    import aiohttp
    async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as s:
        for p in proxies:
            if p.status == "alive" and not p.country:
                p.country = await get_country(p, s, COUNTRY_CACHE)


def main():
    ap = argparse.ArgumentParser(
        description="ProxyVeil: free proxy collector + verifier + system-wide rotation")
    ap.add_argument("--concurrency", type=int, default=50,
                    help="concurrent tests (default 50)")
    ap.add_argument("--no-countries", action="store_true", help="skip country detection")
    ap.add_argument("--auto", action="store_true", help="collect and start test automatically")
    ap.add_argument("--trusted-only", action="store_true",
                    help="only fetch high-trust sources (monosans/proxifly)")
    args = ap.parse_args()

    import aiohttp
    import validator

    async def run():
        print("[*] detecting real IP...")
        async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"}) as s:
            real_ip = await get_real_ip(s)
        print(f"[*] real IP: {real_ip or 'not found'}")

        print("[*] fetching proxy sources...")
        proxies = await collect(trusted_only=args.trusted_only)
        print(f"[+] {len(proxies)} proxies collected (deduplicated)")

        counter = {"collected": len(proxies), "tested": 0, "alive": 0, "dead": 0, "transparent": 0}

        from ui import ProxyApp
        app = ProxyApp(proxies, counter, real_ip)
        app.concurrency = args.concurrency

        async def auto_run():
            if args.auto:
                await asyncio.sleep(0.5)
                app.action_toggle_test()
        asyncio.create_task(auto_run())

        await app.run_async()
        print("[*] exited")

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\n[cancelled]")


if __name__ == "__main__":
    main()
