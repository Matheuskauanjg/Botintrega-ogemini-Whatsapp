"""Testes da relevância de membros e do limite diário das notícias."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import bridge_china_news as news
from news_mentions import matching_members, mention_rules


def guild():
    return SimpleNamespace(members=[
        SimpleNamespace(id=111111111111111111, display_name="Rafael", name="rafael_90", bot=False),
        SimpleNamespace(id=222222222222222222, display_name="Borges", name="borges_02", bot=False),
        SimpleNamespace(id=333333333333333333, display_name="Greed", name="greed", bot=False),
    ])


class NewsMentionTests(unittest.TestCase):
    def test_keyword_only_for_relevant_member(self):
        article = {"title": "中国足球联赛迎来新赛季", "description": "体育新闻"}
        rules = {"111111111111111111": ["足球"], "222222222222222222": ["电影"]}
        self.assertEqual(matching_members(article, guild(), rules=rules), ["111111111111111111"])

    def test_exact_name_not_generic_alias(self):
        article = {"title": "Rafael lança nova coleção", "description": ""}
        self.assertEqual(matching_members(article, guild(), rules={}), ["111111111111111111"])
        self.assertEqual(matching_members({"title":"the greed economy", "description":""}, guild(), rules={}), [])

    def test_reject_nonmember_invalid_rules_and_partial_keywords(self):
        with patch.dict("news_mentions.os.environ", {
            "CHINA_NEWS_MENTION_RULES": '{"111111111111111111":["电影"],"123":["足球"]}'
        }):
            self.assertEqual(mention_rules(), {"111111111111111111": ["电影"]})
        article = {"title":"电影院", "description":"电影"}
        rules = {"111111111111111111": ["足球"], "444444444444444444":["电影"]}
        self.assertEqual(matching_members(article, guild(), rules=rules), [])

    def test_once_per_day_and_next_day(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(news, "DB_PATH", str(Path(tmp) / "news.db")):
            article = {"url":"https://example.org/story", "category":"地方",
                       "_mention_ids":["111111111111111111"]}
            news._save_post(article, 11)
            self.assertEqual(news._seen_mentions_db(), {"111111111111111111"})
            with patch("news.matching_members", return_value=["111111111111111111", "222222222222222222"]):
                self.assertEqual(news._select_daily_mentions(article, guild(),
                    news._seen_mentions_db(), 0), ["222222222222222222"])
            tomorrow = datetime.now(timezone.utc) + timedelta(days=2)
            with patch("news.datetime") as mocked_datetime:
                mocked_datetime.now.return_value = tomorrow
                mocked_datetime.side_effect = datetime
                self.assertEqual(news._seen_mentions_db(), set())

    def test_post_contains_a_single_discord_ping(self):
        article = {"url":"https://example.org/story", "category":"地方", "source":"Some news",
                   "title":"中国最新消息", "_mention_ids":["111111111111111111"]}
        formatted = news._format_news_post(article, "重大新闻影响了交通发展。" * 28)
        self.assertEqual(formatted.count("<@111111111111111111>"), 1)
        self.assertIn("🔔 相关成员：", formatted)
        self.assertLess(len(formatted), 2000)


class HistoryMentionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_recover_today_from_previous_posts(self):
        recent = datetime.now(timezone.utc)
        async def history(limit):
            yield SimpleNamespace(
                author=SimpleNamespace(id=123), created_at=recent,
                content=news.MARKER + " · 地方\n🔔 相关成员：<@111111111111111111>\n"
                        "🔗 原文：<https://example.org/news>"
            )
        used = set()
        latest, urls = await news._last_discord_post(SimpleNamespace(history=history), 123, used)
        self.assertEqual(used, {"111111111111111111"})
        self.assertGreater(latest, 0)
        self.assertEqual(urls, {"https://example.org/news"})


if __name__ == "__main__":
    unittest.main()
