"""Boletim de notícias chinesas: fonte RSS real, foto da matéria e tradução em tópico."""
import asyncio
import html
import io
import ipaddress
import os
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, quote, urljoin, urlsplit

import discord
import httpx
from fastapi import Depends

CHANNEL_ID = int(os.getenv("CHINA_NEWS_CHANNEL_ID", "1560415874114920559"))
ENABLED = os.getenv("CHINA_NEWS_ENABLED", "true").lower() in {"true", "1", "yes", "on"}
INTERVAL = max(3600, int(os.getenv("CHINA_NEWS_INTERVAL_SECONDS", "7200")))
MAX_AGE_HOURS = max(12, int(os.getenv("CHINA_NEWS_MAX_AGE_HOURS", "96")))
DB_PATH = os.getenv("CHINA_NEWS_DB_PATH", "/data/china_news.sqlite3")
MARKER = "📰 中国新闻速报"
USER_AGENT = "Mozilla/5.0 (compatible; ChinaNewsDiscord/1.0; +https://github.com/)"
CATEGORIES = ("国内", "地方", "娱乐")
PROVINCES = ("浙江", "广东", "四川", "云南", "江苏", "福建", "湖北", "陕西", "山东", "广西")


def _clean(value: str, limit: int) -> str:
    value = re.sub(r"<[^>]+>", " ", html.unescape(value or ""))
    value = re.sub(r"\s+", " ", value).strip().replace("@", "@\u200b")
    return value[:limit].strip()


def _public_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme not in ("http", "https") or not hostname:
            return False
        if hostname in ("localhost", "localhost.localdomain") or hostname.endswith(".local"):
            return False
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            return True
        return address.is_global
    except ValueError:
        return False


def _article_url(raw: str) -> str:
    """Extrai URL do veículo quando o RSS Bing usar /apiclick.aspx?url=..."""
    parsed = urlsplit(html.unescape(raw or ""))
    if parsed.hostname and parsed.hostname.endswith("bing.com") and "apiclick" in parsed.path:
        raw = parse_qs(parsed.query).get("url", [""])[0]
    return raw if _public_url(raw) else ""


class _ArticleImageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.images: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "meta":
            name = (values.get("property") or values.get("name") or "").lower()
            if name in {"og:image", "og:image:url", "twitter:image", "twitter:image:src"}:
                if values.get("content"):
                    self.images.append(values["content"])
        if tag == "link" and "image_src" in (values.get("rel") or "").lower():
            if values.get("href"):
                self.images.append(values["href"])


def _feed_image(item: ET.Element) -> list[str]:
    candidates = []
    for elem in item.iter():
        kind = elem.tag.rsplit("}", 1)[-1].lower()
        if kind in ("thumbnail", "content", "enclosure", "image"):
            for key in ("url", "href"):
                if elem.attrib.get(key):
                    candidates.append(elem.attrib[key])
    description = item.findtext("description") or ""
    candidates.extend(re.findall(r'<img[^>]+src=["\x27]([^"\x27]+)', description, flags=re.I))
    return [html.unescape(url) for url in candidates if _public_url(html.unescape(url))]


def _parse_rss(body: str, category: str) -> list[dict]:
    entries = []
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []
    for item in root.findall(".//item"):
        title = _clean(item.findtext("title") or "", 220)
        url = _article_url(item.findtext("link") or "")
        if not url or not title or not re.search(r"[\u3400-\u9fff]", title):
            continue
        published_text = item.findtext("pubDate") or ""
        try:
            published = parsedate_to_datetime(published_text).timestamp()
        except (TypeError, ValueError, OverflowError):
            published = 0
        if not published or time.time() - published > MAX_AGE_HOURS * 3600:
            continue
        if published > time.time() + 3600:
            continue
        entries.append({
            "title": title,
            "url": url,
            "description": _clean(item.findtext("description") or "", 650),
            "source": _clean(item.findtext("source") or urlsplit(url).hostname or "", 80),
            "category": category,
            "published": published,
            "images": _feed_image(item),
        })
    return entries


def _open_db() -> sqlite3.Connection:
    path = Path(DB_PATH)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=5)
    except OSError:
        fallback = Path("/tmp/china_news.sqlite3")
        fallback.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(fallback, timeout=5)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS articles "
        "(url TEXT PRIMARY KEY, published_at REAL NOT NULL, message_id TEXT NOT NULL, category TEXT NOT NULL)"
    )
    connection.commit()
    return connection


def _read_state() -> tuple[float, int, set[str]]:
    with _open_db() as db:
        latest, count = db.execute(
            "SELECT COALESCE(MAX(published_at), 0), COUNT(*) FROM articles"
        ).fetchone()
        urls = {row[0] for row in db.execute(
            "SELECT url FROM articles WHERE published_at > ?", (time.time() - 30 * 86400,)
        )}
    return float(latest), int(count), urls


def _save_post(article: dict, message_id: int) -> None:
    with _open_db() as db:
        db.execute(
            "INSERT OR IGNORE INTO articles(url, published_at, message_id, category) VALUES (?, ?, ?, ?)",
            (article["url"], time.time(), str(message_id), article["category"]),
        )
        db.commit()


async def _rss_results(http: httpx.AsyncClient, query: str, category: str) -> list[dict]:
    feeds = (
        "https://www.bing.com/news/search?q=" + quote(query) + "&format=rss&mkt=zh-CN&setlang=zh-Hans",
        "https://news.google.com/rss/search?q=" + quote(query) + "&hl=zh-CN&gl=CN&ceid=CN:zh-Hans",
    )
    for feed in feeds:
        try:
            response = await http.get(feed)
            response.raise_for_status()
            parsed = _parse_rss(response.text, category)
            if parsed:
                return sorted(parsed, key=lambda item: item["published"], reverse=True)
        except (httpx.HTTPError, ValueError) as exc:
            print(f"[ChinaNews] RSS indisponível: {type(exc).__name__}", flush=True)
    return []


def _queries(post_count: int) -> list[tuple[str, str]]:
    category = CATEGORIES[post_count % len(CATEGORIES)]
    province = PROVINCES[(post_count // len(CATEGORIES)) % len(PROVINCES)]
    if category == "国内":
        return [("中国 国内 最新新闻 时政 社会", category), ("中国 今日 国内 要闻", category)]
    if category == "地方":
        return [(f"{province} 最新新闻 城市 社会", category), (f"{province} 今日新闻", category)]
    return [("中国 娱乐 明星 八卦 最新", category), ("中国 娱乐圈 影视 明星", category)]


def _image_format(content: bytes, content_type: str) -> str | None:
    if content.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "webp"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    return None


async def _download_image(http: httpx.AsyncClient, url: str) -> tuple[bytes, str] | None:
    if not _public_url(url):
        return None
    try:
        async with http.stream("GET", url, follow_redirects=True) as response:
            response.raise_for_status()
            if not response.headers.get("content-type", "").lower().startswith("image/"):
                return None
            data = bytearray()
            async for block in response.aiter_bytes():
                data.extend(block)
                if len(data) > 5 * 1024 * 1024:
                    return None
            ext = _image_format(bytes(data), response.headers.get("content-type", ""))
            return (bytes(data), ext) if ext else None
    except (httpx.HTTPError, ValueError):
        return None


async def _article_image(http: httpx.AsyncClient, article: dict) -> tuple[bytes, str] | None:
    urls = list(article["images"])
    try:
        response = await http.get(article["url"], follow_redirects=True)
        response.raise_for_status()
        if "text/html" in response.headers.get("content-type", ""):
            parser = _ArticleImageParser()
            parser.feed(response.text[:250000])
            urls.extend(urljoin(str(response.url), image) for image in parser.images)
    except (httpx.HTTPError, ValueError):
        pass
    for url in dict.fromkeys(urls):
        image = await _download_image(http, url)
        if image:
            return image
    return None


async def _last_discord_post(channel, own_id: int) -> float:
    """Protege contra posts repetidos quando o armazenamento reinicia."""
    try:
        async for message in channel.history(limit=45):
            if getattr(message.author, "id", None) == own_id and MARKER in (message.content or ""):
                return message.created_at.timestamp()
    except Exception as exc:
        print(f"[ChinaNews] Histórico indisponível: {type(exc).__name__}", flush=True)
    return 0.0


async def _publish(channel, article: dict, image: tuple[bytes, str], translation: str) -> int:
    category = article["category"]
    source = article["source"] or "Noticiário"
    content = (
        f"{MARKER} · {category}\n\n"
        f"**{article['title']}**\n"
        f"{article['description'][:540]}\n\n"
        f"🗞️ 来源：{source}\n"
        f"🔗 原文：<{article['url']}>\n"
        "🇧🇷 葡语翻译请查看下方讨论串。"
    )[:1900]
    file = discord.File(io.BytesIO(image[0]), filename=f"china-news.{image[1]}")
    posted = await channel.send(content=content, file=file)
    try:
        thread = await posted.create_thread(name=f"🇧🇷 Tradução | {article['title'][:63]}")
        translated = (
            f"🇧🇷 **Tradução da notícia — {category}**\n\n"
            f"{translation.strip()}\n\n"
            f"🗞️ **Fonte:** {source}\n🔗 **Notícia original:** <{article['url']}>\n"
            "ℹ️ Tradução do título e resumo publicados na fonte, não do texto integral."
        )
        for start in range(0, len(translated), 1850):
            await thread.send(translated[start:start + 1850])
    except Exception:
        # Não manter publicação sem o tópico de tradução.
        try:
            await posted.delete()
        except Exception:
            pass
        raise
    return posted.id


class ChinaNewsService:
    def __init__(self, client, translator) -> None:
        self.client = client
        self.translator = translator
        self.task: asyncio.Task | None = None
        self.status: dict = {
            "enabled": ENABLED,
            "channelId": str(CHANNEL_ID),
            "intervalSeconds": INTERVAL,
            "lastPosted": None,
            "lastError": None,
        }

    async def on_ready(self) -> None:
        if ENABLED and (self.task is None or self.task.done()):
            self.task = asyncio.create_task(self._worker(), name="china-news-worker")

    async def _worker(self) -> None:
        while True:
            try:
                await self.client.wait_until_ready()
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.status["lastError"] = f"{type(exc).__name__}: {exc}"[:250]
                print(f"[ChinaNews] {self.status['lastError']}", flush=True)
            await asyncio.sleep(60)

    async def _tick(self) -> None:
        last_sent, count, seen = await asyncio.to_thread(_read_state)
        if time.time() - last_sent < INTERVAL:
            return
        channel = self.client.get_channel(CHANNEL_ID) or await self.client.fetch_channel(CHANNEL_ID)
        if getattr(channel, "guild", None) is None or not hasattr(channel, "send"):
            raise RuntimeError("Canal indisponível ou não é um canal de texto de servidor")
        last_discord = await _last_discord_post(channel, self.client.user.id)
        if time.time() - max(last_sent, last_discord) < INTERVAL:
            return
        self.status["lastAttempt"] = datetime.now(timezone.utc).isoformat()
        async with httpx.AsyncClient(
            timeout=16, follow_redirects=True,
            headers={"user-agent": USER_AGENT, "accept-language": "zh-CN,zh;q=0.9"},
        ) as http:
            for query, category in _queries(count):
                results = await _rss_results(http, query, category)
                for article in results[:12]:
                    if article["url"] in seen:
                        continue
                    image = await _article_image(http, article)
                    if not image:
                        continue
                    translation = await self.translator(article["title"], article["description"])
                    if not translation:
                        self.status["lastError"] = "Provedores de tradução indisponíveis"
                        return
                    message_id = await _publish(channel, article, image, translation)
                    await asyncio.to_thread(_save_post, article, message_id)
                    self.status["lastPosted"] = datetime.now(timezone.utc).isoformat()
                    self.status["lastError"] = None
                    self.status["lastArticle"] = article["url"]
                    print(f"[ChinaNews] Publicado ({category}) {article['url']}", flush=True)
                    return
        self.status["lastError"] = "Nenhuma notícia recente com imagem real disponível"


def install(client, app, translator, auth_dependency) -> ChinaNewsService:
    service = ChinaNewsService(client, translator)
    client.add_listener(service.on_ready, "on_ready")

    @app.get("/api/china-news/status", dependencies=[Depends(auth_dependency)])
    async def china_news_status() -> dict:
        return dict(service.status)

    return service
