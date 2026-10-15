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
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, quote, urljoin, urlsplit

import discord
import httpx
from fastapi import Depends

CHANNEL_ID = int(os.getenv("CHINA_NEWS_CHANNEL_ID", "1560415874114920559"))
GUILD_ID = int(os.getenv("CHINA_NEWS_GUILD_ID", "1251266361569710222"))
MANUAL_COOLDOWN = max(10, int(os.getenv("CHINA_NEWS_MANUAL_COOLDOWN_SECONDS", "30")))
ADMIN_IDS = {item.strip() for item in os.getenv("CHINA_NEWS_ADMIN_IDS", "").split(",") if item.strip()}
COMMAND_RE = re.compile(r"^!noticia(?:\s+(nacional|prov[ií]ncia|fofoca))?\s*$", re.IGNORECASE)
CATEGORY_ALIASES = {
    "nacional": "国内",
    "provincia": "地方",
    "província": "地方",
    "fofoca": "娱乐",
}
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
    """Prioriza a imagem principal do artigo, com fallback em fotos do corpo."""
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.images: list[str] = []
        self.body_images: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "meta":
            name = (values.get("property") or values.get("name") or "").lower()
            if name in {"og:image", "og:image:url", "twitter:image", "twitter:image:src"}:
                url = values.get("content")
                if url:
                    self.images.append(url)
        elif tag == "link" and "image_src" in (values.get("rel") or "").lower():
            if values.get("href"):
                self.images.append(values["href"])
        elif tag == "img":
            # Jornais chineses frequentemente carregam fotos via atributos lazy-load.
            for attribute in ("data-original", "data-src", "data-echo", "data-lazy", "src"):
                url = values.get(attribute) or ""
                if url and not url.startswith("data:") and not re.search(
                    r"logo|avatar|icon|sprite|banner|tracking|pixel|advert|qr(code)?",
                    url, flags=re.I
                ):
                    if url not in self.body_images:
                        self.body_images.append(url)
                    break


def _feed_image(item: ET.Element) -> list[str]:
    candidates: list[str] = []
    for elem in item.iter():
        kind = elem.tag.rsplit("}", 1)[-1].lower()
        if kind in ("thumbnail", "content", "enclosure", "image"):
            url = elem.attrib.get("url") or elem.attrib.get("href") or ""
            mime = (elem.attrib.get("type") or "").lower()
            if url and (not mime or mime.startswith("image/")):
                candidates.append(html.unescape(url))
    description = item.findtext("description") or ""
    candidates.extend(re.findall(r'<img[^>]+src=["\x27]([^"\x27]+)', description, flags=re.I))
    return [html.unescape(url) for url in dict.fromkeys(candidates) if url.strip()]


def _date_from_article_url(url: str) -> float:
    """Datas explícitas de URLs de jornais, sem supor que artigos antigos são atuais."""
    found = re.search(r"/(20\d{2})/(\d{2})[-/](\d{2})(?:/|[-.])", url)
    if not found:
        return 0
    try:
        return datetime(
            *(int(part) for part in found.groups()),
            tzinfo=timezone(timedelta(hours=8)),
        ).timestamp()
    except ValueError:
        return 0


def _published_timestamp(item: ET.Element, url: str) -> float:
    value = item.findtext("pubDate") or ""
    if not value:
        for element in item.iter():
            if element.tag.rsplit("}", 1)[-1].lower() in {"date", "published", "updated"}:
                value = (element.text or "").strip()
                if value:
                    break
    if value:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone(timedelta(hours=8)))
            return date.timestamp()
        except (TypeError, ValueError, OverflowError):
            pass
        try:
            date = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone(timedelta(hours=8)))
            return date.timestamp()
        except (TypeError, ValueError, OverflowError):
            pass
    return _date_from_article_url(url)


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
        published = _published_timestamp(item, url)
        age = time.time() - published
        if not published or age > MAX_AGE_HOURS * 3600 or age < -3600:
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


# RSS oficiais publicados pelo próprio China News Service (中新网).
# As buscas Bing/Google ficam como fontes de reserva, não como únicas fontes.
DIRECT_FEEDS = {
    "国内": ("china.xml", "society.xml", "importnews.xml", "photo.xml"),
    "地方": ("df.xml", "society.xml", "scroll-news.xml", "photo.xml"),
    "娱乐": ("culture.xml", "ent.xml", "photo.xml"),
}


def _feed_urls(query: str, category: str) -> list[str]:
    direct = [
        f"https://www.chinanews.com.cn/rss/{name}"
        for name in DIRECT_FEEDS.get(category, ())
    ]
    search = [
        "https://www.bing.com/news/search?q=" + quote(query)
        + "&format=rss&mkt=zh-CN&setlang=zh-Hans",
        "https://news.google.com/rss/search?q=" + quote(query)
        + "&hl=zh-CN&gl=CN&ceid=CN:zh-Hans",
    ]
    return direct + search


async def _rss_results(http: httpx.AsyncClient, query: str, category: str) -> list[dict]:
    errors: list[str] = []
    # Procurar matérias em várias fontes, em vez de retornar somente a primeira
    # com resultados (que pode não ter absolutamente nenhuma fotografia).
    articles: dict[str, dict] = {}
    for feed in _feed_urls(query, category):
        try:
            response = await http.get(feed, follow_redirects=True)
            response.raise_for_status()
            parsed = _parse_rss(response.text, category)
            for item in parsed[:35]:
                articles.setdefault(item["url"], item)
            if len(articles) >= 40:
                break
        except (httpx.HTTPError, ValueError) as exc:
            errors.append(f"{urlsplit(feed).hostname}: {type(exc).__name__}")
    if errors:
        print("[ChinaNews] Feed errors: " + "; ".join(errors[:5]), flush=True)
    print(f"[ChinaNews] category={category} valid_rss_articles={len(articles)}", flush=True)
    return sorted(articles.values(), key=lambda item: item["published"], reverse=True)[:50]


def _queries(post_count: int, category_override: str | None = None) -> list[tuple[str, str]]:
    category = category_override or CATEGORIES[post_count % len(CATEGORIES)]
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


async def _download_image(
    http: httpx.AsyncClient, url: str, referrer: str = ""
) -> tuple[bytes, str] | None:
    if not _public_url(url):
        return None
    try:
        headers = {"Referer": referrer} if referrer else {}
        async with http.stream("GET", url, follow_redirects=True, headers=headers) as response:
            response.raise_for_status()
            kind = response.headers.get("content-type", "").lower()
            if kind and not kind.startswith(("image/", "application/octet-stream")):
                return None
            data = bytearray()
            async for block in response.aiter_bytes():
                data.extend(block)
                if len(data) > 5 * 1024 * 1024:
                    return None
            ext = _image_format(bytes(data), kind)
            return (bytes(data), ext) if ext else None
    except (httpx.HTTPError, ValueError):
        return None


async def _article_image(http: httpx.AsyncClient, article: dict) -> tuple[bytes, str] | None:
    urls: list[str] = []
    # Foto de capa extraída do artigo é preferível a miniaturas genéricas.
    try:
        response = await http.get(article["url"], follow_redirects=True)
        response.raise_for_status()
        if "text/html" in response.headers.get("content-type", ""):
            parser = _ArticleImageParser()
            parser.feed(response.text[:500000])
            urls.extend(urljoin(str(response.url), image) for image in parser.images)
            urls.extend(urljoin(str(response.url), image) for image in parser.body_images[:15])
    except (httpx.HTTPError, ValueError):
        pass
    urls.extend(urljoin(article["url"], image) for image in article["images"])
    for url in dict.fromkeys(urls):
        image = await _download_image(http, url, article["url"])
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
        self._publish_lock = asyncio.Lock()
        self._last_manual_attempt = 0.0
        self.status: dict = {
            "enabled": ENABLED,
            "channelId": str(CHANNEL_ID),
            "guildId": str(GUILD_ID),
            "manualCommand": "!noticia [nacional|provincia|fofoca]",
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
                async with self._publish_lock:
                    await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.status["lastError"] = f"{type(exc).__name__}: {exc}"[:250]
                print(f"[ChinaNews] {self.status['lastError']}", flush=True)
            await asyncio.sleep(60)

    async def publish_now(self, category: str | None = None) -> dict:
        """Publicação manual: dispensa as 2h, mas mantém deduplicação e proteção antispam."""
        if self._publish_lock.locked():
            return {"ok": False, "reason": "Já estou procurando uma notícia. Tente novamente."}
        async with self._publish_lock:
            now = time.monotonic()
            remaining = MANUAL_COOLDOWN - (now - self._last_manual_attempt)
            if remaining > 0:
                return {"ok": False, "reason": f"Aguarde {int(remaining) + 1}s para usar o comando novamente."}
            self._last_manual_attempt = now
            return await self._tick(force=True, category_override=category)

    async def _tick(self, force: bool = False, category_override: str | None = None) -> dict:
        last_sent, count, seen = await asyncio.to_thread(_read_state)
        if not force and time.time() - last_sent < INTERVAL:
            return {"ok": False, "reason": "intervalo"}
        channel = self.client.get_channel(CHANNEL_ID) or await self.client.fetch_channel(CHANNEL_ID)
        if getattr(channel, "guild", None) is None or not hasattr(channel, "send"):
            raise RuntimeError("Canal indisponível ou não é um canal de texto de servidor")
        last_discord = await _last_discord_post(channel, self.client.user.id)
        if not force and time.time() - max(last_sent, last_discord) < INTERVAL:
            return {"ok": False, "reason": "intervalo"}
        self.status["lastAttempt"] = datetime.now(timezone.utc).isoformat()
        self.status["lastMode"] = "manual" if force else "scheduled"
        async with httpx.AsyncClient(
            timeout=16, follow_redirects=True,
            headers={"user-agent": USER_AGENT, "accept-language": "zh-CN,zh;q=0.9"},
        ) as http:
            for query, category in _queries(count, category_override):
                results = await _rss_results(http, query, category)
                for article in results[:30]:
                    if article["url"] in seen:
                        continue
                    image = await _article_image(http, article)
                    if not image:
                        continue
                    translation = await self.translator(article["title"], article["description"])
                    if not translation:
                        self.status["lastError"] = "Provedores de tradução indisponíveis"
                        return {"ok": False, "reason": self.status["lastError"]}
                    message_id = await _publish(channel, article, image, translation)
                    await asyncio.to_thread(_save_post, article, message_id)
                    self.status["lastPosted"] = datetime.now(timezone.utc).isoformat()
                    self.status["lastError"] = None
                    self.status["lastArticle"] = article["url"]
                    print(f"[ChinaNews] Publicado ({category}) {article['url']}", flush=True)
                    return {"ok": True, "messageId": str(message_id), "category": category, "url": article["url"]}
        self.status["lastError"] = "Nenhuma notícia recente com imagem verificável disponível; consulte logs [ChinaNews]"
        return {"ok": False, "reason": self.status["lastError"]}


def install(client, app, translator, auth_dependency) -> ChinaNewsService:
    service = ChinaNewsService(client, translator)
    # discord.Client (classe base usada pelo bridge) não possui add_listener().
    # Encadeia o on_ready existente sem substituir a inicialização atual.
    previous_on_ready = getattr(client, "on_ready", None)

    @client.event
    async def on_ready() -> None:
        if previous_on_ready is not None:
            await previous_on_ready()
        await service.on_ready()

    # Empilha o comando no fim da cadeia de on_message, sem afetar os outros comandos.
    previous_on_message = getattr(client, "on_message", None)

    @client.event
    async def on_message(message) -> None:
        content = str(getattr(message, "content", "") or "").strip()
        match = COMMAND_RE.fullmatch(content)
        if match and getattr(getattr(message, "guild", None), "id", None) == GUILD_ID:
            author_id = str(getattr(getattr(message, "author", None), "id", ""))
            own_id = str(getattr(getattr(client, "user", None), "id", ""))
            server_owner_id = str(getattr(message.guild, "owner_id", ""))
            if author_id and (
                author_id == own_id or author_id == server_owner_id or author_id in ADMIN_IDS
            ):
                category = CATEGORY_ALIASES.get((match.group(1) or "").lower())
                try:
                    result = await service.publish_now(category)
                    if result.get("ok"):
                        link = (
                            f"https://discord.com/channels/{GUILD_ID}/{CHANNEL_ID}/"
                            f"{result['messageId']}"
                        )
                        reply = f"📰 Notícia publicada com imagem e tópico de tradução! {link}"
                    else:
                        reply = f"⚠️ Notícia não publicada: {result.get('reason', 'erro desconhecido')}"
                except Exception as exc:
                    service.status["lastError"] = f"{type(exc).__name__}: {exc}"[:250]
                    print(f"[ChinaNews] Falha no comando: {service.status['lastError']}", flush=True)
                    reply = "⚠️ Falha ao publicar a notícia. Verifique os logs do serviço."
                try:
                    await message.channel.send(reply)
                except Exception as exc:
                    print(f"[ChinaNews] Falha ao responder comando: {type(exc).__name__}", flush=True)
                return
        if previous_on_message is not None:
            await previous_on_message(message)

    @app.get("/api/china-news/status", dependencies=[Depends(auth_dependency)])
    async def china_news_status() -> dict:
        return dict(service.status)

    return service
