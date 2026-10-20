"""Busca imagens ilustrativas abertas quando o veículo não fornece uma foto utilizável.

As imagens alternativas NUNCA são anunciadas como registros do acontecimento:
a publicação inclui crédito, licença e aviso de que são ilustrativas.
"""
from __future__ import annotations

import os
import re
from html import unescape
from urllib.parse import urlsplit

import httpx

EXTERNAL_IMAGES_ENABLED = os.getenv("CHINA_NEWS_EXTERNAL_IMAGES", "true").lower() in {"true", "1", "yes", "on"}

# Tradução pequena e determinística de assuntos frequentes; sem necessidade de API de IA.
LOCATIONS = (
    ("北京", "Beijing"), ("上海", "Shanghai"), ("广州", "Guangzhou"),
    ("深圳", "Shenzhen"), ("杭州", "Hangzhou"), ("天津", "Tianjin"),
    ("重庆", "Chongqing"), ("成都", "Chengdu"), ("武汉", "Wuhan"),
    ("南京", "Nanjing"), ("苏州", "Suzhou"), ("西安", "Xi'an"),
    ("香港", "Hong Kong"), ("澳门", "Macau"), ("台湾", "Taiwan"),
    ("浙江", "Zhejiang"), ("广东", "Guangdong"), ("四川", "Sichuan"),
    ("云南", "Yunnan"), ("江苏", "Jiangsu"), ("福建", "Fujian"),
    ("湖北", "Hubei"), ("陕西", "Shaanxi"), ("山东", "Shandong"),
    ("广西", "Guangxi"),
)
TOPICS = (
    ("地铁", "metro station"), ("高铁", "high speed railway"), ("铁路", "railway"),
    ("交通", "public transportation"), ("机场", "airport"),
    ("汽车", "automobiles"), ("电动车", "electric vehicles"),
    ("人工智能", "artificial intelligence"), ("科技", "technology"),
    ("航天", "space program"), ("卫星", "satellite"), ("经济", "economy"),
    ("学校", "school"), ("教育", "university"),
    ("医院", "hospital"), ("医疗", "healthcare"), ("旅游", "tourism"),
    ("博物馆", "museum"), ("文化", "cultural heritage"),
    ("电影", "cinema"), ("明星", "red carpet event"),
    ("娱乐", "entertainment venue"), ("演唱会", "concert stage"),
    ("足球", "football stadium"), ("体育", "sports stadium"),
    ("台风", "typhoon weather"), ("洪水", "flood"),
    ("地震", "earthquake damage"), ("消防", "firefighters"),
    ("公园", "park"), ("城市", "city skyline"), ("建筑", "architecture"),
)


def image_queries(article: dict) -> list[str]:
    """Combina a manchete original e um tema/local reconhecível em inglês."""
    title = re.sub(r"[\s【】（）()《》：“”‘’、，。；！？!?|]+", " ", article.get("title", "")).strip()
    location = next((english for chinese, english in LOCATIONS if chinese in title), "China")
    topic = next((english for chinese, english in TOPICS if chinese in title), "")
    if not topic:
        topic = "cinema and entertainment" if article.get("category") == "娱乐" else "landmark city"
    candidates = [title[:75], f"{location} {topic}"]
    return [q for q in dict.fromkeys(candidates) if q and len(q) >= 3]


def _safe_public_url(value: str) -> bool:
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or not host:
            return False
        if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
            return False
        # URLs fornecidas por terceiros não devem apontar para IPs internos.
        import ipaddress
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return True
        return ip.is_global
    except ValueError:
        return False


def _credit(value: str, limit: int = 90) -> str:
    return re.sub(r"[\r\n<>@*_`]", " ", unescape(re.sub(r"<[^>]+>", " ", str(value or "")))).strip()[:limit]


def _openverse_candidates(payload: dict) -> list[dict]:
    candidates = []
    results = payload.get("results", []) if isinstance(payload, dict) else []
    if not isinstance(results, list):
        return []
    for item in results[:8]:
        if not isinstance(item, dict) or item.get("mature"):
            continue
        license_name = (item.get("license") or "").lower()
        if license_name not in {"cc0", "pdm", "by", "by-sa"}:
            continue
        if (item.get("width") or 900) < 380 or (item.get("height") or 600) < 220:
            continue
        title = str(item.get("title") or "")
        if re.search(r"logo|icon|clipart|watermark|banner|poster", title, flags=re.I):
            continue
        page = item.get("foreign_landing_url") or ""
        urls = [item.get("thumbnail") or "", item.get("url") or ""]
        urls = [url for url in dict.fromkeys(urls) if _safe_public_url(url)]
        if not _safe_public_url(page) or not urls:
            continue
        candidates.append({
            "urls": urls,
            "page": page,
            "creator": _credit(item.get("creator") or "autor não informado"),
            "license": _credit(license_name.upper(), 30),
        })
    return candidates


def _commons_candidates(payload: dict) -> list[dict]:
    pages = ((payload.get("query") or {}).get("pages") or {}) if isinstance(payload, dict) else {}
    if not isinstance(pages, dict):
        return []
    candidates = []
    for page in list(pages.values())[:8]:
        for info in page.get("imageinfo", [])[:1]:
            ext = info.get("extmetadata") or {}
            license_name = ((ext.get("LicenseShortName") or {}).get("value") or "").strip()
            # Wikimedia contém imagens de terceiros; só utilizamos licenças abertas.
            if not re.search(r"CC\s*(BY|0)|public domain|domínio público", license_name, flags=re.I):
                continue
            file_url = info.get("thumburl") or info.get("url") or ""
            page_url = info.get("descriptionurl") or ""
            if not _safe_public_url(file_url) or not _safe_public_url(page_url):
                continue
            creator = _credit((ext.get("Artist") or {}).get("value") or "Wikimedia Commons")
            candidates.append({
                "urls": [file_url],
                "page": page_url,
                "creator": creator or "Wikimedia Commons",
                "license": _credit(license_name, 30),
            })
    return candidates


async def find_illustrative_image(http: httpx.AsyncClient, article: dict, download) -> tuple[bytes, str] | None:
    """Busca e baixa somente imagens com licença conhecida; registra a atribuição."""
    if not EXTERNAL_IMAGES_ENABLED:
        return None

    queries = image_queries(article)
    for provider in ("openverse", "commons"):
        for query in queries:
            try:
                if provider == "openverse":
                    response = await http.get(
                        "https://api.openverse.org/v1/images/",
                        params={"q": query, "page_size": 8, "license": "cc0,pdm,by,by-sa"},
                        timeout=8,
                    )
                    response.raise_for_status()
                    candidates = _openverse_candidates(response.json())
                else:
                    response = await http.get(
                        "https://commons.wikimedia.org/w/api.php",
                        params={
                            "action": "query", "generator": "search",
                            "gsrsearch": query, "gsrnamespace": 6, "gsrlimit": 8,
                            "prop": "imageinfo", "iiprop": "url|extmetadata",
                            "iiurlwidth": 1000, "format": "json",
                        },
                        timeout=8,
                    )
                    response.raise_for_status()
                    candidates = _commons_candidates(response.json())
            except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
                print(f"[ChinaNews] Busca ilustrativa {provider} indisponível: {type(exc).__name__}", flush=True)
                continue

            for item in candidates[:3]:
                for url in item["urls"]:
                    picture = await download(http, url, item["page"])
                    if picture:
                        article["_illustrative"] = {
                            "creator": item["creator"],
                            "license": item["license"],
                            "url": item["page"],
                            "provider": provider,
                        }
                        print(f"[ChinaNews] Imagem ilustrativa obtida: {provider} ({query[:45]})", flush=True)
                        return picture
    return None
