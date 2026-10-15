"""Leitura de RSS e imagens reais (sem postar no Discord). Teste de conectividade opcional."""
import asyncio
import os
import re

import httpx

from bridge_china_news import _article_image, _parse_rss, USER_AGENT


async def main():
    samples = (
        ("国内", "https://www.chinanews.com.cn/rss/china.xml"),
        ("国内", "https://www.chinanews.com.cn/rss/photo.xml"),
        ("娱乐", "https://www.chinanews.com.cn/rss/culture.xml"),
    )
    async with httpx.AsyncClient(
        timeout=12.0,
        follow_redirects=True,
        headers={"user-agent": USER_AGENT, "accept-language": "zh-CN,zh;q=0.9"},
    ) as client:
        total_articles, image_found, body_found = 0, False, False
        for category, url in samples:
            try:
                response = await client.get(url)
                response.raise_for_status()
                articles = _parse_rss(response.text, category)
                total_articles += len(articles)
                print(f"FEED {url} status={response.status_code} articles={len(articles)}")
                for article in articles[:2]:
                    try:
                        found = await _article_image(client, article)
                    except Exception as exc:
                        print(f"IMAGE candidate error={type(exc).__name__}")
                        continue
                    print(f"IMAGE {'ok' if found else 'unavailable'} source={article['source']}")
                    cjk_length = len(re.findall(r"[\u3400-\u9fff]", article.get("body_text", "")))
                    print(f"ARTICLE extracted_cjk_chars={cjk_length} url={article['url']}")
                    if cjk_length >= 220:
                        body_found = True
                    if found:
                        image_found = True
                        break
            except httpx.HTTPError as exc:
                print(f"FEED {url} error={type(exc).__name__}")
        print(f"RESULT articles={total_articles} photo_valid={image_found} article_text_valid={body_found}")
        if not total_articles:
            raise RuntimeError("No recent articles returned from official feeds")
        if not image_found:
            raise RuntimeError("Recent articles returned, but none of the sampled photos downloaded")
        if not body_found:
            raise RuntimeError("No full-length Chinese article body extracted from sample")


if __name__ == "__main__":
    asyncio.run(main())
