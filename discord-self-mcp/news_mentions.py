"""Marcações contextuais conservadoras para notícias: nomes ou interesses cadastrados."""
from __future__ import annotations
import json
import os
import re
import unicodedata

MENTIONS_ENABLED = os.getenv("CHINA_NEWS_MENTIONS_ENABLED", "true").lower() in {"true","1","yes","on"}
MAX_MENTIONS = max(1, min(5, int(os.getenv("CHINA_NEWS_MAX_MENTIONS_PER_POST", "2"))))
EXCLUDED_NICKNAMES = {"admin", "moderador", "moderator", "gamer", "chines", "china", "news", "bot", "greed", "user", "membro", "staff", "owner", "usuario", "member"}


def _fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", str(text).casefold())
                   if not unicodedata.combining(c)).strip()


def _contains_term(text: str, term: str) -> bool:
    word = _fold(term)
    if re.search(r"[\u3400-\u9fff]", word):
        return len(word) >= 2 and word in text
    if len(word) < 4:
        return False
    return bool(re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text))


def mention_rules() -> dict[str, list[str]]:
    """JSON: {"123456789012345678": ["足球", "世界杯", "futebol"]}."""
    raw = os.getenv("CHINA_NEWS_MENTION_RULES", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        print("[ChinaNews] CHINA_NEWS_MENTION_RULES inválido: JSON esperado", flush=True)
        return {}
    if not isinstance(parsed, dict):
        return {}
    result = {}
    for user_id, keywords in parsed.items():
        if not re.fullmatch(r"\d{15,22}", str(user_id)) or not isinstance(keywords, list):
            continue
        cleaned = [str(k).strip() for k in keywords
                   if isinstance(k, str) and 2 <= len(k.strip()) <= 60]
        if cleaned:
            result[str(user_id)] = cleaned[:20]
    return result


def matching_members(article: dict, guild, own_id: int | None = None, rules=None) -> list[str]:
    """Procura interesses configurados e nomes próprios não genéricos no título/resumo.
    Não adivinha preferências pessoais ou vínculos com celebridades."""
    if not MENTIONS_ENABLED:
        return []
    rules = mention_rules() if rules is None else rules
    title = _fold(article.get("title") or "")
    description = _fold(article.get("description") or "")
    text = title + " " + description
    # Requer membros reais do servidor, evitando ping de IDs externos/sem acesso.
    members = getattr(guild, "members", []) or []
    indexed = {str(getattr(member, "id")): member for member in members
               if getattr(member, "id", None) is not None}
    getter = getattr(guild, "get_member", None)
    if callable(getter):
        for user_id in rules:
            if user_id not in indexed:
                try:
                    member = getter(int(user_id))
                    if member is not None:
                        indexed[user_id] = member
                except (ValueError, TypeError):
                    pass
    relevant = []
    for user_id, member in indexed.items():
        if user_id == str(own_id) or getattr(member, "bot", False):
            continue
        terms = rules.get(user_id, [])
        if any(_contains_term(text, term) for term in terms):
            relevant.append(user_id)
            continue
        # Uma referência nominal explícita na manchete é relevante, mas
        # apelidos genéricos e parciais nunca devem disparar ping.
        names = {str(getattr(member, "display_name", "") or ""),
                 str(getattr(member, "name", "") or "")}
        if any(len(_fold(name)) >= 5
               and _fold(name) not in EXCLUDED_NICKNAMES
               and _contains_term(title, name) for name in names):
            relevant.append(user_id)
    return relevant[:MAX_MENTIONS]
