"""Testes offline para busca de fotos ilustrativas em fontes licenciadas."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from news_images import (
    _commons_candidates, _openverse_candidates, _safe_public_url,
    find_illustrative_image, image_queries,
)
from bridge_china_news import _format_news_post


def _response(payload):
    return SimpleNamespace(json=lambda: payload, raise_for_status=lambda: None)


class ImageSourceTests(unittest.TestCase):
    def test_queries_are_about_the_specific_article(self):
        queries = image_queries({
            "title": "北京地铁推出新的城市交通服务",
            "category": "地方",
        })
        self.assertIn("北京地铁", queries[0])
        self.assertIn("Beijing metro station", queries[1])

    def test_rejects_private_urls_and_non_open_licenses(self):
        self.assertFalse(_safe_public_url("http://127.0.0.1/image.jpg"))
        self.assertFalse(_safe_public_url("https://192.168.1.2/image.jpg"))
        self.assertTrue(_safe_public_url("https://upload.wikimedia.org/a.jpg"))
        payload = {"results": [
            {"license": "by-nc", "thumbnail": "https://cdn.example.com/1.jpg",
             "foreign_landing_url": "https://example.com/1"},
            {"license": "by", "mature": True, "thumbnail": "https://cdn.example.com/2.jpg",
             "foreign_landing_url": "https://example.com/2"},
            {"license": "cc0", "width": 100, "height": 100,
             "thumbnail": "https://cdn.example.com/3.jpg", "foreign_landing_url": "https://example.com/3"},
        ]}
        self.assertEqual(_openverse_candidates(payload), [])

    def test_commons_accepts_only_open_license(self):
        payload = {"query": {"pages": {
            "1": {"imageinfo": [{"thumburl": "https://upload.wikimedia.org/a.jpg",
                                  "descriptionurl": "https://commons.wikimedia.org/wiki/File:A.jpg",
                                  "extmetadata": {
                                      "LicenseShortName": {"value": "CC BY-SA 4.0"},
                                      "Artist": {"value": "<a href='/author'>Photographer</a>"}
                                  }}]},
            "2": {"imageinfo": [{"thumburl": "https://upload.wikimedia.org/b.jpg",
                                  "descriptionurl": "https://commons.wikimedia.org/wiki/File:B.jpg",
                                  "extmetadata": {"LicenseShortName": {"value": "All rights reserved"}}}]},
        }}}
        results = _commons_candidates(payload)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["creator"], "Photographer")

    def test_discord_post_marks_external_photo_as_illustration(self):
        article = {
            "title": "北京地铁宣布新服务", "category": "国内",
            "source": "Example News", "url": "https://example.com/news",
            "_illustrative": {
                "creator": "Photographer", "license": "CC BY-SA 4.0",
                "url": "https://commons.wikimedia.org/wiki/File:A.jpg",
            },
        }
        content = _format_news_post(article, "据报道，这是一次重要的消息公布。" * 20)
        self.assertIn("示意配图", content)
        self.assertIn("Photographer", content)
        self.assertIn("CC BY-SA 4.0", content)
        self.assertIn("<https://commons.wikimedia.org/wiki/File:A.jpg>", content)
        self.assertLessEqual(len(content), 2000)


class ExternalSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_openverse_fallback_downloads_and_credits(self):
        article = {"title": "北京地铁推出新的城市交通服务", "category": "地方"}
        payload = {"results": [{
            "license": "by", "width": 1200, "height": 800,
            "thumbnail": "https://images.example.com/beijing-metro.jpg",
            "foreign_landing_url": "https://example.com/photo/beijing",
            "creator": "Example photographer", "title": "Beijing metro station"
        }]}
        http = SimpleNamespace(get=AsyncMock(return_value=_response(payload)))
        download = AsyncMock(return_value=(b"\xff\xd8\xff", "jpg"))
        picture = await find_illustrative_image(http, article, download)
        self.assertEqual(picture[1], "jpg")
        self.assertEqual(article["_illustrative"]["provider"], "openverse")
        download.assert_awaited_once()
        self.assertEqual(download.await_args.args[1], "https://images.example.com/beijing-metro.jpg")

    async def test_falls_back_to_commons_when_openverse_has_no_photos(self):
        article = {"title": "杭州新的城市交通服务", "category": "地方"}
        commons = {"query": {"pages": {"1": {"imageinfo": [{
            "thumburl": "https://upload.wikimedia.org/example.jpg",
            "descriptionurl": "https://commons.wikimedia.org/wiki/File:Example.jpg",
            "extmetadata": {"LicenseShortName": {"value": "Public domain"}},
        }]}}}}
        http = SimpleNamespace(get=AsyncMock(side_effect=[
            _response({"results": []}), _response({"results": []}),
            _response(commons),
        ]))
        download = AsyncMock(return_value=(b"\xff\xd8\xff", "jpg"))
        picture = await find_illustrative_image(http, article, download)
        self.assertEqual(picture[1], "jpg")
        self.assertEqual(article["_illustrative"]["provider"], "commons")

    async def test_can_disable_external_images(self):
        article = {"title": "上海新闻", "category": "国内"}
        http = SimpleNamespace(get=AsyncMock())
        with patch("news_images.EXTERNAL_IMAGES_ENABLED", False):
            self.assertIsNone(await find_illustrative_image(http, article, AsyncMock()))
        http.get.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
