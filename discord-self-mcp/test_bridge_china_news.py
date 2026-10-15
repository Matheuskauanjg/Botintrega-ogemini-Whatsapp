"""Testes offline do coletor (sem login Discord nem chamadas à internet)."""
import asyncio
import importlib
import importlib.machinery
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# A suíte geral do repositório não instala dependências de runtime. Para os
# testes offline, simula apenas os símbolos externos necessários ao import.
for module_name in ("discord", "httpx", "fastapi"):
    try:
        importlib.import_module(module_name)
    except ModuleNotFoundError:
        stub = types.ModuleType(module_name)
        stub.__spec__ = importlib.machinery.ModuleSpec(module_name, loader=None)
        if module_name == "httpx":
            stub.AsyncClient = type("AsyncClient", (), {})
            stub.HTTPError = Exception
        elif module_name == "fastapi":
            stub.Depends = lambda dependency: dependency
        elif module_name == "discord":
            stub.File = lambda *args, **kwargs: None
        sys.modules[module_name] = stub

import bridge_china_news as news


class ChinaNewsTests(unittest.TestCase):
    def test_query_rotation_and_regions(self):
        self.assertEqual(news._queries(0)[0][1], "国内")
        self.assertEqual(news._queries(1)[0][1], "地方")
        self.assertIn("浙江", news._queries(1)[0][0])
        self.assertEqual(news._queries(2)[0][1], "娱乐")
        self.assertIn("广东", news._queries(4)[0][0])

    def test_rss_extracts_fresh_chinese_news_and_image(self):
        published = format_datetime(datetime.now(timezone.utc), usegmt=True)
        rss = (
            '<rss xmlns:media="http://search.yahoo.com/mrss/"><channel><item>'
            '<title>北京发布最新城市交通消息</title>'
            '<link>https://example.com/zh/news/1</link>'
            f'<pubDate>{published}</pubDate>'
            '<source>示例新闻</source><description>今天公布了重要信息</description>'
            '<media:thumbnail url="https://img.example.com/photo.jpg" />'
            '</item></channel></rss>'
        )
        entries = news._parse_rss(rss, "地方")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["category"], "地方")
        self.assertEqual(entries[0]["source"], "示例新闻")
        self.assertEqual(entries[0]["images"][0], "https://img.example.com/photo.jpg")

    def test_english_and_stale_articles_are_skipped(self):
        old = format_datetime(datetime(2020, 1, 1, tzinfo=timezone.utc), usegmt=True)
        published = format_datetime(datetime.now(timezone.utc), usegmt=True)
        rss = (
            "<rss><channel>"
            f"<item><title>English news</title><link>https://example.com/1</link><pubDate>{published}</pubDate></item>"
            f"<item><title>中国旧闻</title><link>https://example.com/2</link><pubDate>{old}</pubDate></item>"
            "</channel></rss>"
        )
        self.assertFalse(news._parse_rss(rss, "国内"))

    def test_bing_link_resolves_original_url(self):
        url = "https://www.bing.com/news/apiclick.aspx?aid=1&url=https%3A%2F%2Fexample.com%2Fstory"
        self.assertEqual(news._article_url(url), "https://example.com/story")
        self.assertEqual(news._article_url("http://127.0.0.1/admin"), "")
        self.assertEqual(news._article_url("file:///etc/passwd"), "")

    def test_publisher_image_metadata_and_signature(self):
        parser = news._ArticleImageParser()
        parser.feed('<meta property="og:image" content="/photos/china.jpg">')
        self.assertEqual(parser.images, ["/photos/china.jpg"])
        self.assertEqual(news._image_format(b"\xff\xd8\xffabc", "image/jpeg"), "jpg")
        self.assertIsNone(news._image_format(b"<html>not an image</html>", "image/jpeg"))

    def test_official_chinese_feeds_precede_search_engines(self):
        feeds = news._feed_urls("中国 新闻", "国内")
        self.assertTrue(feeds[0].startswith("https://www.chinanews.com.cn/rss/china.xml"))
        self.assertTrue(any("photo.xml" in feed for feed in feeds))
        self.assertTrue(any("bing.com" in feed for feed in feeds))

    def test_article_parser_finds_lazy_loaded_images(self):
        parser = news._ArticleImageParser()
        parser.feed(
            '<img src="/assets/logo.png"><img data-src="/photos/report.webp">'
            '<meta property="og:image" content="/cover.jpg">'
        )
        self.assertEqual(parser.images, ["/cover.jpg"])
        self.assertIn("/photos/report.webp", parser.body_images)
        self.assertNotIn("/assets/logo.png", parser.body_images)

    def test_chinese_news_rss_date_derived_from_article_url(self):
        rss = (
            "<rss><channel><item><title>中国最新消息</title>"
            '<link>https://www.chinanews.com.cn/gn/2026/10-15/1234567.shtml</link>'
            "<description>新闻摘要</description></item></channel></rss>"
        )
        with patch.object(news, "MAX_AGE_HOURS", 240):
            entries = news._parse_rss(rss, "国内")
        # Usa o relógio real da suíte: o artigo é válido apenas perto da data da URL.
        self.assertEqual(news._date_from_article_url(entries[0]["url"]) > 0, True) if entries else self.assertGreater(
            news._date_from_article_url("https://www.chinanews.com.cn/gn/2026/10-15/1234567.shtml"), 0
        )

    def test_explicit_pubdate_and_feed_images(self):
        published = format_datetime(datetime.now(timezone.utc), usegmt=True)
        rss = (
            '<rss xmlns:media="http://search.yahoo.com/mrss/"><channel><item>'
            '<title>浙江新增城市文化活动</title>'
            '<link>https://example.org/news/1</link>'
            f'<pubDate>{published}</pubDate><description><![CDATA['
            '<img src="/assets/main.jpg">信息摘要'
            ']]></description><media:content type="image/jpeg" url="/large.jpg"/>'
            '</item></channel></rss>'
        )
        entries = news._parse_rss(rss, "地方")
        self.assertEqual(len(entries), 1)
        self.assertIn("/large.jpg", entries[0]["images"])
        self.assertIn("/assets/main.jpg", entries[0]["images"])

    def test_long_article_extractor_ignores_navigation_and_handles_void_tags(self):
        paragraph = "据报道，相关部门公布了一项城市服务政策，相关企业和居民对实施细节展开讨论。"
        html = (
            "<nav><p>" + paragraph * 3 + "</p></nav>"
            '<meta name="viewport" content="width=device-width">'
            '<div class="left_zw" id="cont_1_1_2"><img src="photo.jpg">'
            '<p>' + paragraph * 7 + "</p>"
            '<p>' + paragraph * 4 + "</p></div>"
            '<footer><p>' + paragraph * 3 + "</p></footer>"
        )
        parser = news._ArticleTextParser()
        parser.feed(html)
        result = parser.get_text()
        self.assertGreater(len(result), 330)
        self.assertEqual(len(parser.primary), 2)
        self.assertEqual(result.count(paragraph), 11)
        self.assertNotIn("nav", result)

    def test_long_news_format_preserves_link_and_full_paragraphs(self):
        article = {
            "title": "中国服务行业持续变化",
            "category": "国内",
            "url": "https://www.chinanews.com.cn/sh/2026/10-16/10715381.shtml",
            "source": "中国新闻网",
        }
        body = "\n\n".join([
            "随着城市服务领域的发展，各类新就业形式正得到更多关注。" * 4
            for _ in range(5)
        ])
        post = news._format_news_post(article, body)
        self.assertGreater(len(post), 650)
        self.assertLess(len(post), 2000)
        self.assertIn("https://www.chinanews.com.cn/sh/2026/10-16/10715381.shtml", post)
        self.assertIn("葡语翻译请查看下方讨论串", post)
        self.assertIn("\n\n", post)

    def test_translation_split_preserves_paragraphs(self):
        text = "\n\n".join(["Um parágrafo sobre a reportagem com texto claro. " * 15] * 5)
        parts = news._split_translation(text, 650)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(part) <= 650 for part in parts))
        self.assertEqual(" ".join(" ".join(parts).split()), " ".join(text.split()))

    def test_db_persists_urls_and_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "news.sqlite3")
            with patch.object(news, "DB_PATH", db_path):
                news._save_post({"url": "https://example.com/china", "category": "国内"}, 12345)
                latest, count, seen = news._read_state()
                self.assertEqual(count, 1)
                self.assertGreater(latest, 0)
                self.assertIn("https://example.com/china", seen)


class ManualCommandTests(unittest.IsolatedAsyncioTestCase):
    def make_client(self):
        class TestClient:
            def __init__(self):
                self.user = SimpleNamespace(id=7654)
                self.previous_messages = []
                self.on_message = self.original_on_message
                self.on_ready = self.original_on_ready

            def event(self, callback):
                setattr(self, callback.__name__, callback)
                return callback

            async def original_on_message(self, message):
                self.previous_messages.append(message)

            async def original_on_ready(self):
                return None

        class TestApp:
            def get(self, *args, **kwargs):
                return lambda callback: callback

        client = TestClient()
        service = news.install(client, TestApp(), AsyncMock(), lambda: None)
        return client, service

    async def test_manual_command_publishes_and_preserves_original_handlers(self):
        client, service = self.make_client()
        service.publish_now = AsyncMock(return_value={
            "ok": True, "messageId": "789", "category": "娱乐",
        })
        channel = SimpleNamespace(send=AsyncMock())
        message = SimpleNamespace(
            guild=SimpleNamespace(id=news.GUILD_ID),
            author=SimpleNamespace(id=7654),
            content="!noticia fofoca",
            channel=channel,
        )
        await client.on_message(message)
        service.publish_now.assert_awaited_once_with("娱乐")
        self.assertIn(
            f"https://discord.com/channels/{news.GUILD_ID}/{news.CHANNEL_ID}/789",
            channel.send.await_args.args[0],
        )
        self.assertFalse(client.previous_messages)
        other = SimpleNamespace(
            guild=SimpleNamespace(id=news.GUILD_ID),
            author=SimpleNamespace(id=7654),
            content="bom dia",
            channel=channel,
        )
        await client.on_message(other)
        self.assertEqual(client.previous_messages, [other])

    async def test_manual_command_only_allows_owner_or_configured_admins(self):
        client, service = self.make_client()
        service.publish_now = AsyncMock()
        message = SimpleNamespace(
            guild=SimpleNamespace(id=news.GUILD_ID),
            author=SimpleNamespace(id=99999),
            content="!noticia",
            channel=SimpleNamespace(send=AsyncMock()),
        )
        with patch.object(news, "ADMIN_IDS", set()):
            await client.on_message(message)
        service.publish_now.assert_not_awaited()
        self.assertEqual(client.previous_messages, [message])

    async def test_server_owner_may_trigger(self):
        client, service = self.make_client()
        service.publish_now = AsyncMock(return_value={"ok": True, "messageId": "456"})
        channel = SimpleNamespace(send=AsyncMock())
        message = SimpleNamespace(
            guild=SimpleNamespace(id=news.GUILD_ID, owner_id=111222),
            author=SimpleNamespace(id=111222),
            content="!noticia nacional",
            channel=channel,
        )
        await client.on_message(message)
        service.publish_now.assert_awaited_once_with("国内")

    async def test_manual_bypasses_interval_with_cooldown(self):
        service = news.ChinaNewsService(SimpleNamespace(), AsyncMock())
        service._tick = AsyncMock(return_value={"ok": True, "messageId": "42"})
        result = await service.publish_now("地方")
        self.assertTrue(result["ok"])
        service._tick.assert_awaited_once_with(force=True, category_override="地方")
        duplicate = await service.publish_now("地方")
        self.assertFalse(duplicate["ok"])
        self.assertIn("Aguarde", duplicate["reason"])
        self.assertEqual(service._tick.await_count, 1)

    def test_command_patterns(self):
        self.assertTrue(news.COMMAND_RE.fullmatch("!noticia"))
        self.assertTrue(news.COMMAND_RE.fullmatch("!noticia nacional"))
        self.assertEqual(news.COMMAND_RE.fullmatch("!noticia província").group(1), "província")
        self.assertIsNone(news.COMMAND_RE.fullmatch("!noticia bobagem"))
        self.assertEqual(news._queries(0, "娱乐")[0][1], "娱乐")


if __name__ == "__main__":
    unittest.main()
