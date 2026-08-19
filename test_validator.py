"""Anonimlik sınıflandırma mantığı için assert tabanlı birim test.

Çalıştır: python test_validator.py
"""
import validator
from unittest.mock import patch

PASS = 0

def check(name, cond):
    global PASS
    assert cond, f"FAIL: {name}"
    PASS += 1
    print(f"  ok: {name}")

def run():
    global PASS
    print("== classify_anonymity ==")
    validator.REAL_IP = "94.235.98.23"

    # 1. Elite: gerçek IP yanıtta yok, forwarding header yok
    check("elite: temiz yanıt",
          validator.classify_anonymity({"content-type": "application/json"}, "1.2.3.4") == "elite")

    # 2. Elite: HTTP proxy çalışsa bile ekstra header yok
    check("elite: yanıt IP farklı, header yok",
          validator.classify_anonymity({"server": "nginx"}, "5.6.7.8") == "elite")

    # 3. Anonymous: X-Forwarded-For var ama gerçek IP yok → sızıntı yok
    check("anonymous: XFF var, gerçek IP yok",
          validator.classify_anonymity({"x-forwarded-for": "203.0.113.9"}, "203.0.113.9") == "anonymous")

    # 4. Anonymous: via header'ı var
    check("anonymous: via var",
          validator.classify_anonymity({"via": "1.1 proxy-42"}, "9.9.9.9") == "anonymous")

    # 5. Transparent: yanıt IP bizim gerçek IP'miz (sızıntı)
    check("transparent: yanıt IP = gerçek IP",
          validator.classify_anonymity({"server": "nginx"}, "94.235.98.23") == "transparent")

    # 6. Transparent: XFF içinde gerçek IP
    check("transparent: XFF'de gerçek IP",
          validator.classify_anonymity({"x-forwarded-for": "94.235.98.23, 10.0.0.5"}, "10.0.0.5") == "transparent")

    # 7. Real IP bilinmiyorsa (None) sızıntı tespiti yapılamaz → header'a bakar
    validator.REAL_IP = None
    check("real ip yok: header da yoksa elite",
          validator.classify_anonymity({"date": "x"}, "any.ip") == "elite")
    # 8. Real IP boş string: transparent sahtelemesin ("" in xff her zaman True bug'ı)
    validator.REAL_IP = ""
    check("real ip boş: transparent yanlış işaretlenmez",
          validator.classify_anonymity({"x-forwarded-for": "1.2.3.4"}, "1.2.3.4") == "anonymous")
    validator.REAL_IP = "94.235.98.23"

    print("== yanıt header süzme (FORWARD_HEADERS) ==")
    fwd = set(validator.FORWARD_HEADERS)
    check("forw header'ları tanımlı", len(fwd) >= 6)
    check("proxy-connection listede", "proxy-connection" in fwd)
    check("x-forwarded-for listede", "x-forwarded-for" in fwd)

    print("== echo zinciri sırası ==")
    check("ilk echo api64.ipify", validator.ECHO_URLS[0].startswith("http://api64.ipify.org"))
    check("https echo var", any(u.startswith("https://") for u in validator.HTTPS_ECHO_URLS))

    print("== https protokol normalizasyonu ==")
    import asyncio
    from unittest.mock import AsyncMock, patch

    async def t():
        # https protokolü proxy URL'sini http:// yapar (CONNECT tüneli)
        from collector import Proxy
        p = Proxy(host="1.2.3.4", port=8080, protocol="https")
        fake_client = AsyncMock()
        with patch("validator.aiohttp.ClientSession") as _:
            pass
        # _probe içi proxy_url doğrudan test edilemez — _probe'u çağır ve
        # aiohttp'in proxy parametresini yakala
        calls = {}

        class R:
            status = 200
            def __init__(self, calls):
                self._calls = calls
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False
            async def text(self, errors="replace"):
                return '{"ip":"9.9.9.9"}'
            async def json(self):
                return {"ip": "9.9.9.9"}
            headers = {}

        def fake_get(url, **kw):
            calls["proxy"] = kw.get("proxy")
            return R(calls)
        fake_client.get = fake_get
        ip, _, _ = await validator._probe(fake_client, p, "http://x/")
        check("https -> proxy url http://", calls.get("proxy") == "http://1.2.3.4:8080")
        check("https probe ip ok", ip == "9.9.9.9")
    asyncio.run(t())

    print("== collector: _valid https + trust ==")
    from collector import _valid, _parse_raw
    check("https protokol kabul", _valid("1.2.3.4", "8080", "https") is True)
    check("http kabul", _valid("1.2.3.4", "80", "http") is True)
    check("socks5 kabul", _valid("1.2.3.4", "1080", "socks5") is True)
    check("geçersiz proto red", _valid("1.2.3.4", "80", "ftp") is False)
    check("geçersiz host red", _valid("notanip", "80", "http") is False)
    parsed = _parse_raw("1.2.3.4:80\n5.6.7.8:8080 extra\nhttp://9.9.9.9:443\n",
                        "http", "test-src", trust="high")
    check("raw parse 3", len(parsed) == 3)
    check("tab ile ülke atılır", parsed[1].port == 8080)
    check("protocol parametresi baskın (src.protocol)", parsed[2].protocol == "http")
    check("trust aktarılır", parsed[0].trust == "high")
    check("source adı", parsed[0].source == "test-src")
    # proxifly-https: dosya http:// içerir ama protokol https olmalı (CONNECT tüneli)
    pf = _parse_raw("http://8.8.8.8:9002\n", "https", "proxifly-https")
    check("proxifly-https: http:// satırı https etiketlenir", pf[0].protocol == "https")
    # scheme'den protokol: protocol=None (proxifly-all tarzı)
    sc = _parse_raw("socks5://7.7.7.7:1080\n", None, "proxifly-all")
    check("scheme'den protokol", sc[0].protocol == "socks5")
    # proxifly-https kaynağı yorum satırında (server dead) — yukarıda srcs ile doğrulanır
    import collector as C
    srcs = C.SOURCES
    check("monosans-http kaynağı var", any(s["name"] == "monosans-http" for s in srcs))
    check("proxifly-https devre dışı (server dead)",
          all(s["name"] != "proxifly-https" for s in srcs))
    check("monosans-http trust=high",
          next(s for s in srcs if s["name"] == "monosans-http")["trust"] == "high")
    check("thespeedX trust=medium",
          next(s for s in srcs if s["name"] == "thespeedX-http")["trust"] == "medium")

    print(f"\n{PASS} test geçti ✓")


if __name__ == "__main__":
    run()