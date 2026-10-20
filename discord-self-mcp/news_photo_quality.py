"""Validates photographic news images and detects near-duplicates without storing user data."""
from __future__ import annotations
import io
import re
from collections import Counter
from urllib.parse import unquote, urlsplit
from PIL import Image, ImageOps, UnidentifiedImageError

NOT_PHOTO = re.compile(
    r"(^|[\/_.-])(logo|logotype|brand|favicon|avatar|icon|banner|masthead|"
    r"headerlogo|site-logo|watermark|sprite|weixin|qr(code)?|weibo|share|"
    r"placeholder|defaultpic|default[-_]image|advert|ad[-_]img|badge)(?=[\/_.-]|$)",
    flags=re.I,
)


def reject_url(url: str) -> bool:
    path = unquote(urlsplit(str(url)).path).lower()
    return bool(NOT_PHOTO.search(path))


def is_news_photo(content: bytes) -> bool:
    """Reject header logos, skinny banners, tiny thumbnails and single-color images."""
    try:
        with Image.open(io.BytesIO(content)) as source:
            source.load()
            width, height = source.size
            aspect = width / max(1, height)
            if width < 440 or height < 240 or aspect > 2.65 or aspect < 0.48:
                return False
            small = ImageOps.exif_transpose(source).convert("RGB")
            small.thumbnail((64, 64))
            # A logo geralmente ocupa fundo sólido com 1-3 cores dominantes.
            colors = Counter((r // 32, g // 32, b // 32) for r, g, b in small.getdata())
            dominant_share = max(colors.values()) / max(1, sum(colors.values()))
            return dominant_share <= 0.58 and len(colors) >= 15
    except (UnidentifiedImageError, OSError, ValueError):
        return False


def image_fingerprint(content: bytes) -> int:
    """Difference-hash (64 bits). Works across similar JPEG/WebP recompressions."""
    with Image.open(io.BytesIO(content)) as im:
        gray = ImageOps.exif_transpose(im).convert("L").resize((9, 8))
        px = list(gray.getdata())
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | int(px[row * 9 + col] > px[row * 9 + col + 1])
    return bits


def has_duplicate_photo(fingerprint: int, previous: set[int], max_distance: int = 6) -> bool:
    return any((fingerprint ^ other).bit_count() <= max_distance for other in previous)
