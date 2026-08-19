"""Proxy kaynak toplayıcı: bedava kamu listeleri -> (ip, port, protocol) seti."""
import asyncio, ipaddress, json, time
from dataclasses import dataclass, field
from urllib.parse import quote

import aiohttp
from bs4 import BeautifulSoup

# Kaynak tanımları. `kind`: raw | json | html
# thespeedX: HTTP/SOCKS4/SOCKS5 raw (günlük, doğrulanmamış -> medium).
# monosans: her saat doğrulanır, proxies.json ülke içerir (high).
# proxifly: protocol bazlı ayrı dosyalar (high).
SOURCES = [
    {"kind": "raw", "name": "monosans-http",
     "url": "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
     "protocol": "http", "trust": "high"},
    {"kind": "raw", "name": "monosans-socks4",
     "url": "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks4.txt",
     "protocol": "socks4", "trust": "high"},
    {"kind": "raw", "name": "monosans-socks5",
     "url": "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
     "protocol": "socks5", "trust": "high"},
    {"kind": "json", "name": "monosans-geo",
     "url": "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies.json",
     "protocol": None, "trust": "high"},  # ülke içerir (geolocation.country.iso_code)
    {"kind": "raw", "name": "proxifly-http",
     "url": "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt",
     "protocol": "http", "trust": "high"},
    # {"kind": "raw", "name": "proxifly-https",  # https listesi: ölü (server dead),
    #  "url": "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/protocols/https/data.txt",
    #  "protocol": "https", "trust": "high"},  # CONNECT tüneli: http gibi davranır
    {"kind": "raw", "name": "proxifly-socks4",
     "url": "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks4/data.txt",
     "protocol": "socks4", "trust": "high"},
    {"kind": "raw", "name": "proxifly-socks5",
     "url": "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks5/data.txt",
     "protocol": "socks5", "trust": "high"},
    {"kind": "raw", "name": "thespeedX-http",
     "url": "https://raw.githubusercontent.com/thespeedX/proxy-list/master/http.txt",
     "protocol": "http", "trust": "medium"},
    {"kind": "raw", "name": "thespeedX-socks4",
     "url": "https://raw.githubusercontent.com/thespeedX/proxy-list/master/socks4.txt",
     "protocol": "socks4", "trust": "medium"},
    {"kind": "raw", "name": "thespeedX-socks5",
     "url": "https://raw.githubusercontent.com/thespeedX/proxy-list/master/socks5.txt",
     "protocol": "socks5", "trust": "medium"},
    # {"kind": "html", "name": "free-proxy-list.net",
    #  "url": "https://free-proxy-list.net/", "protocol": "http", "trust": "medium"},
]

CACHE_SECONDS = 300

@dataclass
class Proxy:
    host: str
    port: int
    protocol: str            # http | https | socks4 | socks5 (https = CONNECT tüneli)
    country: str = ""        # kaynak sağlamıyorsa boş; test sonrası ip-api ile doldurulur
    source: str = ""
    trust: str = "medium"    # high (monosans/proxifly) | medium (thespeedX)
    # test sonuçları
    status: str = "pending"  # pending|testing|alive|dead
    anon: str = ""           # elite|anonymous|transparent (test sonrası)
    https: bool = False
    latency_ms: float = 0.0
    ext_ip: str = ""         # proxy üzerinden görülen IP
    headers: dict = field(default_factory=dict)

    @property
    def key(self):
        return (self.host, self.port, self.protocol)

    def addr(self):
        return f"{self.host}:{self.port}"


def _valid(host, port, proto):
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return 1 <= int(port) <= 65535 and proto in ("http", "https", "socks4", "socks5")


def _parse_raw(text, protocol, source, trust="medium"):
    seen, out = set(), []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        proto = protocol
        if line.startswith(("http://", "https://", "socks4://", "socks5://")):
            scheme, _, rest = line.partition("://")
            # https:// ile yazan proxifly dosyaları: bu dosyanın protokolünü kullan
            # (src["protocol"]="https" için http:// scheme'ini https yapma)
            proto = scheme if scheme == protocol or not protocol else protocol
            line = rest
        if ":" not in line:
            continue
        host, _, rest = line.partition(":")
        port = rest.split()[0]  # tab ile ülke eklenmişse at (monosans geo)
        if not _valid(host, port, proto):
            continue
        key = (host, int(port), proto)
        if key in seen:
            continue
        seen.add(key)
        out.append(Proxy(host=host, port=int(port), protocol=proto,
                         source=source, trust=trust))
    return out


async def _fetch(session, url, timeout=12):
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
        return await r.text(errors="replace")


async def _collect_source(session, src, cache):
    name, url = src["name"], src["url"]
    cache_key = url
    if cache_key in cache and time.time() - cache[cache_key][0] < CACHE_SECONDS:
        return cache[cache_key][1]
    try:
        if src["kind"] == "raw":
            text = await _fetch(session, url)
            proxies = _parse_raw(text, src["protocol"], name, src.get("trust", "medium"))
        elif src["kind"] == "json":
            text = await _fetch(session, url)
            data = json.loads(text)
            proxies = []
            items = data if isinstance(data, list) else data.get("data", [])
            for item in items:
                # monosans proxies.json: {protocol, host, port, geolocation:{country:{iso_code}}}
                # geonode API: {ip, port, protocols:[...], country}
                if "host" in item:  # monosans
                    proto, host, port = item["protocol"], item["host"], item["port"]
                    geo = item.get("geolocation") or {}
                    country = (geo.get("country") or {}).get("iso_code", "")
                else:              # geonode
                    proto = (item.get("protocols") or [""])[0].split("_")[0]
                    host, port = item.get("ip", ""), item.get("port")
                    country = item.get("country", "")
                if not _valid(host, port, proto):
                    continue
                p = Proxy(host=host, port=int(port), protocol=proto,
                          country=country.upper(), source=name,
                          trust=src.get("trust", "medium"))
                proxies.append(p)
        elif src["kind"] == "html":
            text = await _fetch(session, url)
            proxies = []
            soup = BeautifulSoup(text, "html.parser")
            for tr in soup.select("tbody tr"):
                tds = tr.find_all("td")
                if len(tds) < 5:
                    continue
                host = tds[0].get_text(strip=True)
                port = tds[1].get_text(strip=True)
                country = tds[3].get_text(strip=True)
                if not _valid(host, port, src["protocol"]):
                    continue
                proxies.append(Proxy(host=host, port=int(port), protocol=src["protocol"],
                                     country=country, source=name))
        else:
            return []
        cache[cache_key] = (time.time(), proxies)
        return proxies
    except Exception as e:
        print(f"[collector] source failed {name}: {e}")
        return []


async def collect(trusted_only: bool = False) -> list[Proxy]:
    """Tüm kaynakları eşzamanlı çek, aynı (host,port,proto) tekilleştir.
    trusted_only: sadece high trust kaynaklar."""
    seen, out = {}, []
    srcs = [s for s in SOURCES if not trusted_only or s.get("trust") == "high"]
    async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"}) as session:
        results = await asyncio.gather(*[_collect_source(session, s, {}) for s in srcs],
                                       return_exceptions=True)
        for proxies in results:
            if not isinstance(proxies, list):
                continue
            for p in proxies:
                if p.key not in seen:
                    seen[p.key] = p
                    out.append(p)
    return out


async def get_country(p: Proxy, session, cache: dict) -> str:
    """Ülke kodu: ip-api.com (http) -> ipwho.is (https) zinciri. Boş = bilinmiyor."""
    for url in (f"http://ip-api.com/json/{p.host}",
                f"https://ipwho.is/{p.host}"):
        if url in cache:
            return cache[url]
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=6),
                                   proxy=f"http://{p.addr()}") as r:
                if r.status == 200:
                    data = await r.json()
                    code = (data.get("countryCode") or data.get("country_code") or "").upper()
                    if code:
                        cache[url] = code
                        return code
        except Exception:
            continue
    return ""
