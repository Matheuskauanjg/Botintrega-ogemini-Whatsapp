"""Targeted, opt-out friendly social cues for Greed's Discord replies.

Keep these helpers dependency-free so their behavior is testable without
starting the Discord client or reaching an LLM provider.
"""

from __future__ import annotations

import os
import re
import unicodedata


def normalize(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    return " ".join("".join(ch for ch in text if not unicodedata.combining(ch)).split())


def dogao_targeted(author_name: str, author_id: str, content: str) -> bool:
    """True only for Dogão himself or an explicit request to flirt with him."""
    if os.getenv("DOGAO_FLIRT_ENABLED", "true").strip().lower() in {"0", "false", "off", "no"}:
        return False

    configured_id = os.getenv("DOGAO_USER_ID", "").strip()
    if configured_id and str(author_id) == configured_id:
        return True

    name = normalize(author_name)
    if re.search(r"(?<!\w)dogao(?!\w)", name):
        return True

    text = normalize(content)
    return bool(re.search(
        r"\b(?:flerta|flertar|paquera|paquerar|cantada|cantadas|conquistar|elogia|elogiar)\b.{0,80}\bdogao\b",
        text,
    ))


def dogao_guidance(author_name: str, author_id: str, content: str) -> str:
    """Add a short social cue only when context justifies addressing Dogão."""
    if not dogao_targeted(author_name, author_id, content):
        return ""

    normalized = normalize(content)
    if re.search(r"\b(?:para|pare|parar|chega|stop)\b.{0,35}\b(?:flertar|flerte|cantada|paquerar|paquera)\b", normalized) or re.search(
        r"\b(?:nao|sem)\b.{0,20}\b(?:flerte|flertar|cantada|paquera)\b", normalized
    ):
        return " Se Dogão não quiser cantadas ou pedir para parar, respeite e responda normalmente, sem insistir. "

    return (
        " Dogão é um participante do grupo com quem Greed pode trocar flertes leves, "
        "carinhosos e engraçados quando o contexto for de brincadeira. "
        "Use no máximo uma cantada criativa e natural, só de vez em quando, "
        "sem spam, pressão, conteúdo sexual explícito ou dizer que existe relacionamento real. "
        "Se ele demonstrar incômodo, pare imediatamente; priorize qualquer pergunta real dele antes da brincadeira. "
    )
