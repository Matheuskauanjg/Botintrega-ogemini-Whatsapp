"""Testes offline do coletor (sem login Discord nem chamadas à internet)."""
import os
import tempfile
import unittest
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import patch

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

    def test_db_persists_urls_and_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "news.sqlite3")
            with patch.object(news, "DB_PATH", db_path):
                news._save_post({"url": "https://example.com/china", "category": "国内"}, 12345)
                latest, count, seen = news._read_state()
                self.assertEqual(count, 1)
                self.assertGreater(latest, 0)
                self.assertIn("https://example.com/china", seen)


if __name__ == "__main__":
    unittest.main()
