"""LibriVox audiobook source.

Public domain recordings only. The LibriVox API returns a zip URL
(full book) and individual chapter URLs. We use the zip for single-file
playback; chapter list is stored in source_payload for future chapter
navigation.
"""
from __future__ import annotations

import re

import httpx

LV_API = "https://librivox.org/api/feed/audiobooks"


def _lv_title_query(title: str) -> str:
    # API supports ^word anchored prefix search
    first_word = re.split(r"\s+", title.strip())[0]
    return f"^{first_word.lower()}"


async def search(title: str, author: str, limit: int = 3) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                LV_API,
                params={
                    "title": _lv_title_query(title),
                    "format": "json",
                    "extended": 1,
                    "limit": limit * 2,
                },
            )
            if r.status_code != 200:
                return []
            books = r.json().get("books") or []
            results = []
            title_lower = title.lower()
            for book in books:
                btitle = (book.get("title") or "").strip()
                if title_lower not in btitle.lower():
                    continue
                zip_url = book.get("url_zip_file") or ""
                if not zip_url:
                    continue
                authors = book.get("authors") or []
                author_str = ", ".join(
                    f"{a.get('first_name','')} {a.get('last_name','')}".strip()
                    for a in authors
                )
                results.append({
                    "addon_id": "audimo-audiobooks",
                    "source_id": f"lv:{book.get('id')}",
                    "kind": "http",
                    "link": zip_url,
                    "link_type": "zip",
                    "name": btitle,
                    "source": "LibriVox",
                    "seeders": 0,
                    "size": 0,
                    "free": True,
                    "author": author_str,
                })
                if len(results) >= limit:
                    break
            return results
    except Exception:
        return []


async def stream(source_id: str) -> str | None:
    # source_id = "lv:{book_id}" — fetch fresh zip URL
    parts = source_id.split(":", 1)
    if len(parts) != 2:
        return None
    book_id = parts[1]
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(LV_API, params={"id": book_id, "format": "json", "extended": 1})
            if r.status_code != 200:
                return None
            books = r.json().get("books") or []
            if not books:
                return None
            return books[0].get("url_zip_file") or None
    except Exception:
        return None
