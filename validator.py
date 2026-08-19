"""Proxy doğrulayıcı: canlılık + anonimlik (elite/anonymous/transparent) + HTTPS + gecikme.

Anonimlik, proxy üzerinden echo servisine gidip yanıt header'larını inceleyerek
ölçülür:
- transparent: echo servisi bizim gerçek IP'mizi görüyor (servis tarafında sızıntı
  zaten oluşmuş) VEYA proxy kendi header'larımıza X-Forwarded-For eklemiş
- anonymous:   X-Forwarded-For vb. var ama gerçek IP yok
- elite:       hiçbir forwarding header yok, gerçek IP yok

Echo servislerine giden istek asla kimlik/çerez/kişisel veri içermez.
"""
import asyncio, time

import aiohttp

from collector import Proxy

# Echo servis zinciri: httpbin 503 dönerse/sırayı doldurmazsa sıradaki deneriz.
ECHO_URLS = [
    "http://api64.ipify.org?format=json",      # JSON {"ip": "..."}
    "http://wtfismyip.com/json",               # JSON YourFuckingIPAddress
    "http://ipinfo.io/ip",                     # plaintext IP
    "http://ifconfig.me/ip",                   # plaintext IP
]
HTTPS_ECHO_URLS = [
    "https://api64.ipify.org?format=json",
    "https://wtfismyip.com/json",
]
# Header sızıntısını tespit ettiğimiz header isimleri (case-insensitive karşılaştırılır).
FORWARD_HEADERS = ("x-forwarded-for", "x-forwarded", "forwarded", "proxy-connection",
                   "x-proxy-id", "via", "client-ip", "x-real-ip")

# test_proxy çağrılmadan önce run_batch tarafından set edilir.
REAL_IP = None


def classify_anonymity(seen_headers, response_ip):
    """Görülen header'lar ve yanıt IP'sine göre anonimlik seviyesi.

    seen_headers: proxy üzerinden alınan yanıt header'ları (lowercased keys).
    response_ip:  proxy üzerinden echo servisinin gördüğü IP (None olabilir).
    REAL_IP varsa ve yanıt IP'sine sızıyorsa -> transparent.
    X-Forwarded-For vb. eklenmişse -> anonymous.
    İkisi de yoksa -> elite.
    """
    leaked = bool(response_ip) and bool(REAL_IP) and response_ip == REAL_IP
    xff = seen_headers.get("x-forwarded-for", "")
    forwarded = any(h in seen_headers for h in FORWARD_HEADERS)
    if leaked or (REAL_IP and REAL_IP in xff):
        return "transparent"
    if forwarded:
        return "anonymous"
    return "elite"


async def _probe(client, p: Proxy, url, timeout=10):
    """Tek probe: echo URL'sine proxy üzerinden git. (ip, latency_ms, headers) döner.
    https protokolü CONNECT tüneli kullanır -> aiohttp proxy URL'si http:// olur."""
    proto = "http" if p.protocol == "https" else p.protocol
    proxy_url = f"{proto}://{p.addr()}"
    start = time.monotonic()
    async with client.get(url, proxy=proxy_url,
                          timeout=aiohttp.ClientTimeout(total=timeout),
                          ssl=False) as r:
        if r.status != 200:
            raise aiohttp.ClientError(f"HTTP {r.status}")
        body = (await r.text(errors="replace")).strip()
        if "{" in body:
            try:
                data = await r.json()
                ip = str(data.get("ip") or data.get("YourFuckingIPAddress") or "").strip()
            except Exception:
                ip = ""
        else:
            ip = body
        lat = (time.monotonic() - start) * 1000
        return ip, lat, dict(r.headers)


async def _collect_metrics(client, p: Proxy):
    """3 tekrar ortalama gecikme + HTTPS CONNECT + HTTPS header sızıntısı kontrolü.
    HTTPS echo 2 kez denenir (CONNECT zayıflığına karşı retry).
    Başarısızsa (0.0, False, {}) döner."""
    latencies, merged_headers = [], {}
    for i in range(3):
        for url in ECHO_URLS:
            try:
                ip, lat, headers = await _probe(client, p, url)
                if ip:
                    latencies.append(lat)
                    for k, v in headers.items():
                        if k.lower() in FORWARD_HEADERS:
                            merged_headers.setdefault(k.lower(), v)
                    break
            except Exception:
                continue
    if not latencies:
        return 0.0, False, {}
    # HTTPS: CONNECT tüneli + header sızıntısı (https echo'daki X-Forwarded-For)
    https_ok = False
    for attempt in range(2):  # CONNECT zayıflığına karşı 2 retry
        for url in HTTPS_ECHO_URLS:
            try:
                ip, _, headers = await _probe(client, p, url)
                if ip:
                    https_ok = True
                    for k, v in headers.items():
                        if k.lower() in FORWARD_HEADERS:
                            merged_headers.setdefault(k.lower(), v)
                    break
            except Exception:
                continue
        if https_ok:
            break
    return sum(latencies) / len(latencies), https_ok, merged_headers


async def verify_https(client, p: Proxy, timeout: float = 3.0) -> bool:
    """Bağlanılan proxy'nin HTTPS CONNECT ile gerçekten çalıştığını doğrula."""
    for url in HTTPS_ECHO_URLS:
        try:
            ip, _, _ = await _probe(client, p, url, timeout=timeout)
            if ip:
                return True
        except Exception:
            continue
    return False


async def test_proxy(client, p: Proxy):
    """Tek proxy'yi tam test et: canlılık -> anonimlik -> HTTPS -> gecikme.
    1 retry, tekrar başarısızsa dead."""
    p.status = "testing"
    try:
        # 1. canlılık
        ip, lat, headers = await _probe(client, p, ECHO_URLS[0])
        if not ip:
            raise aiohttp.ClientError("no ip")
        p.ext_ip = ip
        p.anon = classify_anonymity({k.lower(): v for k, v in headers.items()}, ip)
        # 2. gecikme ortalaması + HTTPS + sızıntı kontrolü
        avg, https_ok, extra = await _collect_metrics(client, p)
        merged = {k.lower(): v for k, v in headers.items()}
        merged.update(extra)
        p.anon = classify_anonymity(merged, ip)
        p.https = https_ok
        p.latency_ms = avg or lat
        p.status = "alive"
        return p
    except Exception:
        # 1 retry
        try:
            ip, lat, headers = await _probe(client, p, ECHO_URLS[0])
            if not ip:
                raise aiohttp.ClientError("no ip")
            avg, https_ok, extra = await _collect_metrics(client, p)
            merged = {k.lower(): v for k, v in headers.items()}
            merged.update(extra)
            p.ext_ip = ip
            p.anon = classify_anonymity(merged, ip)
            p.https = https_ok
            p.latency_ms = avg or lat
            p.status = "alive"
            return p
        except Exception:
            p.status = "dead"
            return p


async def run_batch(proxies: list[Proxy], real_ip: str, concurrency: int = 50,
                    on_done=None):
    """Tüm proxy'leri eşzamanlı test et. real_ip: gerçek IP (transparent tespiti için).
    HTTPS destekleyenler önce işlenir (öncelikli). on_done: her bittiğinde çağrılır."""
    global REAL_IP
    REAL_IP = real_ip
    sem = asyncio.Semaphore(concurrency)
    # sıralama: https-onaylı protokol adayları önce (protocol "https" veya http port 443/8443) — ipucu yoksa http önce
    order = sorted(proxies, key=lambda p: 0 if p.protocol == "https" else 1)
    async with aiohttp.ClientSession(
            headers={"User-Agent": "proxy-tester/1.0"},
            connector=aiohttp.TCPConnector(limit=concurrency, ssl=False)) as client:
        async def one(p):
            async with sem:
                res = await test_proxy(client, p)
                if on_done:
                    on_done(res)
                return res
        await asyncio.gather(*[one(p) for p in order])
    return proxies
