"""Regressões da detecção de conversas: testes independentes do Discord."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from chat_routing import directly_mentions, replies_to_self, active_followup, session_key


def fake(content="", reference=None, mentions=None):
    guild = SimpleNamespace(id=1)
    channel = SimpleNamespace(id=3, guild=guild, fetch_message=AsyncMock())
    return SimpleNamespace(
        guild=guild, channel=channel, author=SimpleNamespace(id=2),
        content=content, mentions=mentions or [], reference=reference,
    )


class ChatRoutingTests(unittest.TestCase):
    def test_mentions_work_without_resolved_members(self):
        self.assertTrue(directly_mentions(fake("<@!380410689702854660> você está aí?"), 380410689702854660))
        self.assertTrue(directly_mentions(fake("oi", mentions=[SimpleNamespace(id=123)]), 123))
        self.assertFalse(directly_mentions(fake("oi"), 123))

    def test_followup_only_same_member_channel_and_before_expiry(self):
        message = fake("e como funciona?")
        sessions = {session_key(message): 500.0}
        self.assertTrue(active_followup(message, sessions, 490))
        self.assertFalse(active_followup(fake("!noticia"), sessions, 490))
        self.assertFalse(active_followup(fake("!g ping"), sessions, 490))
        self.assertFalse(active_followup(fake(""), sessions, 490))
        different = fake("olá")
        different.author.id = 999
        self.assertFalse(active_followup(different, sessions, 490))
        self.assertFalse(active_followup(message, sessions, 500))
        self.assertNotIn(session_key(message), sessions)


class ReplyReferenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolved_direct_reply(self):
        m = fake("por que?", SimpleNamespace(resolved=SimpleNamespace(author=SimpleNamespace(id=123)), message_id=444))
        self.assertTrue(await replies_to_self(m, 123))
        self.assertFalse(await replies_to_self(m, 999))
        m.channel.fetch_message.assert_not_awaited()

    async def test_uncached_reference_fetch(self):
        m = fake("só mais uma coisa", SimpleNamespace(message_id=444, resolved=None))
        m.channel.fetch_message.return_value = SimpleNamespace(author=SimpleNamespace(id=123))
        self.assertTrue(await replies_to_self(m, 123))
        m.channel.fetch_message.assert_awaited_once_with(444)

    async def test_fetch_failure_does_not_crash(self):
        m = fake("oi", SimpleNamespace(message_id=444, resolved=None))
        m.channel.fetch_message.side_effect = RuntimeError("missing")
        self.assertFalse(await replies_to_self(m, 123))


if __name__ == "__main__":
    unittest.main()
