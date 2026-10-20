"""Offline photo selection regressions, including the recurring ChinaNews logo failure."""
import io
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock
from types import SimpleNamespace
from PIL import Image, ImageDraw

import bridge_china_news as news
from news_photo_quality import is_news_photo, image_fingerprint, has_duplicate_photo, reject_url
from news_images import image_queries


def photo(seed=7, size=(850, 550), quality=90):
    randomizer = random.Random(seed)
    image = Image.new("RGB", (17, 11))
    image.putdata([(randomizer.randrange(256), randomizer.randrange(256), randomizer.randrange(256))
                   for _ in range(17 * 11)])
    image = image.resize(size, Image.Resampling.BICUBIC)
    bio = io.BytesIO()
    image.save(bio, "JPEG", quality=quality)
    return bio.getvalue()


def news_logo():
    image = Image.new("RGB", (840, 360), "black")
    draw = ImageDraw.Draw(image)
    draw.text((60, 120), "CHINA NEWS CHINA NEWS", fill="red")
    bio = io.BytesIO()
    image.save(bio, "JPEG")
    return bio.getvalue()


class NewsPhotoTests(unittest.TestCase):
    def test_photo_and_logo_quality(self):
        self.assertTrue(is_news_photo(photo()), "photographic test image should pass")
        self.assertFalse(is_news_photo(news_logo()), "logo must be filtered")
        self.assertFalse(is_news_photo(photo(size=(200, 150))), "small image must fail")
        self.assertFalse(is_news_photo(photo(size=(1200, 220))), "banners must fail")
        self.assertFalse(is_news_photo(b"\xff\xd8\xffjunk"))

    def test_filters_metadata_logos_even_when_mime_is_image(self):
        self.assertTrue(reject_url("https://www.chinanews.com.cn/static/images/logo.jpg"))
        self.assertTrue(reject_url("https://image.example.com/icons/banner.jpeg"))
        self.assertFalse(reject_url("https://image.example.com/photos/three_gorges_ship.jpg"))

    def test_near_duplicate_images(self):
        original = image_fingerprint(photo(21, quality=93))
        compressed = image_fingerprint(photo(21, quality=65))
        another = image_fingerprint(photo(22))
        self.assertTrue(has_duplicate_photo(compressed, {original}))
        self.assertFalse(has_duplicate_photo(another, {original}, max_distance=6))

    def test_database_migration_and_deduplication(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.object(news, "DB_PATH", str(Path(root) / "news.sqlite")):
                a = {"url": "https://example.com/a", "category": "国内"}
                self.assertTrue(news._image_not_repeated(a, (photo(1), "jpg"), set()))
                news._save_post(a, 123)
                used = news._read_recent_image_hashes()
                self.assertEqual(len(used), 1)
                b = {"url": "https://example.com/b", "category": "国内"}
                self.assertFalse(news._image_not_repeated(b, (photo(1, quality=70), "jpg"), used))
                self.assertTrue(news._image_not_repeated(b, (photo(31), "jpg"), used))

    def test_no_generic_china_landmark_picture(self):
        queries = image_queries({"title": "今日突发重要会议举行", "category": "国内"})
        self.assertEqual(len(queries), 1)
        self.assertNotIn("landmark city", str(queries))
        specific = image_queries({"title": "三峡水运新通道取得进展", "category": "国内"})
        self.assertTrue(any("Three Gorges" in query or "shipping" in query for query in specific))


class ArticlePhotoTests(unittest.IsolatedAsyncioTestCase):
    async def test_article_skips_generic_logo_and_repeated_photo(self):
        html = ('<meta property="og:image" content="/images/logo.jpg">'
                '<meta property="og:image" content="/photos/first.jpg">'
                '<meta property="og:image" content="/photos/second.jpg">')
        response = SimpleNamespace(
            text=html, url="https://example.org/news1",
            headers={"content-type": "text/html"}, raise_for_status=lambda: None
        )
        client = SimpleNamespace(get=AsyncMock(return_value=response))
        candidate1, candidate2 = photo(12), photo(13)
        article = {"url": "https://example.org/news1", "images": []}
        used = {image_fingerprint(candidate1)}
        async def download(http, url, referrer=""):
            return ((candidate1, "jpg") if "/first.jpg" in url
                    else (candidate2, "jpg") if "/second.jpg" in url else None)
        with patch.object(news, "_download_image", side_effect=download) as mock_dl:
            selected = await news._article_image(client, article, used)
        self.assertEqual(selected[0], candidate2)
        self.assertEqual(mock_dl.await_count, 2)
        self.assertEqual(article["_image_hash"], f"{image_fingerprint(candidate2):016x}")


if __name__ == "__main__":
    unittest.main()
