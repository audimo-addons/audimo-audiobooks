"""Internet Archive audiobook source.

Queries IA's Solr search for audio items tagged as audiobooks.
Returns direct MP3/M4B stream URLs — no torrent, no debrid required.
Prefers single-file items (one big MP3) over chapter-split items.
"""
from __future__ import annotations

import re

import httpx

IA_BASE = "https://archive.org"
_IA_AUDIO_EXTS = (".mp3", ".m4b", ".m4a", ".ogg", ".opus", ".flac")
_IA_STOPWORDS = {"the", "and", "for", "with", "from", "a", "an", "of", "in", "on", "by", "to"}


def _ia_query(title: str, author: str) -> str:
    safe_title = title.replace('"', "")
    q = f'title:"{safe_title}" AND mediatype:audio'
    if author:
        safe_author = re.split(r"\s+", author.strip())[-1].replace('"', "")
        if safe_author:
            q += f' AND creator:"{safe_author}"'
    return q


def _ia_best_file(files: list[dict]) -> dict | None:
    audio = [f for f in files if f.get("name", "").lower().endswith(_IA_AUDIO_EXTS)]
    if not audio:
        return None
    # Prefer the largest single-file (fewest chapters = one big MP3 is the full book)
    audio.sort(key=lambda f: int(f.get("size") or 0), reverse=True)
    return audio[0]


async def search(title: str, author: str, limit: int = 5) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"{IA_BASE}/advancedsearch.php",
                params={
                    "q": _ia_query(title, author),
                    "fl": "identifier,title,creator,subject",
                    "output": "json",
                    "rows": limit * 3,
                    "sort": "downloads desc",
                },
            )
            if r.status_code != 200:
                return []
            docs = r.json().get("response", {}).get("docs") or []
            results = []
            for doc in docs:
                identifier = doc.get("identifier") or ""
                if not identifier:
                    continue
                # Fetch file list to find the actual audio file
                meta_r = await c.get(f"{IA_BASE}/metadata/{identifier}", timeout=6)
                if meta_r.status_code != 200:
                    continue
                files = meta_r.json().get("files") or []
                best = _ia_best_file(files)
                if not best:
                    continue
                fname = best["name"]
                stream_url = f"{IA_BASE}/download/{identifier}/{fname}"
                size_bytes = int(best.get("size") or 0)
                results.append({
                    "addon_id": "audimo-audiobooks",
                    "source_id": f"ia:{identifier}:{fname}",
                    "kind": "http",
                    "link": stream_url,
                    "link_type": "direct",
                    "name": doc.get("title") or title,
                    "source": "Internet Archive",
                    "seeders": 0,
                    "size": size_bytes,
                    "free": True,
                })
                if len(results) >= limit:
                    break
            return results
    except Exception:
        return []


async def stream(source_id: str) -> str | None:
    # source_id = "ia:{identifier}:{filename}"
    parts = source_id.split(":", 2)
    if len(parts) != 3:
        return None
    _, identifier, fname = parts
    return f"{IA_BASE}/download/{identifier}/{fname}"
