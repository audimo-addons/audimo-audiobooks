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
_IA_SKIP = {
    "collection", "omnibus", "box set", "complete works", "collected",
    "trilogy", "anthology", "bundle", "compendium", "complete collection",
    "fiction collection", "hindi", "urdu", "edition)",
}
_ASCII_RE = re.compile(r"[^\x00-\x7F]")


def _ia_query(title: str, author: str) -> str:
    safe_title = title.replace('"', "")
    q = f'title:"{safe_title}" AND mediatype:audio'
    if author:
        safe_author = re.split(r"\s+", author.strip())[-1].replace('"', "")
        if safe_author:
            q += f' AND creator:"{safe_author}"'
    return q


def _ia_title_ok(doc_title: str, search_title: str) -> bool:
    """Return True if doc_title is a plausible match for search_title."""
    dt = doc_title.lower()
    st = search_title.lower()
    # Drop non-ASCII (foreign-language editions)
    if _ASCII_RE.search(doc_title):
        return False
    # Drop collections/compilations
    for phrase in _IA_SKIP:
        if phrase in dt:
            return False
    # The search title's words must all appear in the doc title
    st_words = [w for w in st.split() if len(w) > 2]
    if st_words and not all(w in dt for w in st_words):
        return False
    return True


# Format preference order. We rank MP3 / M4B above FLAC because IA's
# CDN occasionally returns 500 on Range-less GETs of large FLAC files
# (and ffmpeg's HTTP demuxer doesn't send Range by default), and
# because Safari/WKWebView decodes MP3/M4B more reliably than IA-
# delivered FLAC even when the latter does serve. FLAC stays as a
# last-resort so items that only ship .flac still surface.
_IA_FORMAT_RANK = {
    ".m4b": 0,   # native audiobook container, has chapter atoms
    ".mp3": 1,   # universal browser support
    ".m4a": 2,
    ".aac": 3,
    ".ogg": 4,
    ".opus": 5,
    ".flac": 6,
}


def _ia_ext(name: str) -> str:
    n = (name or "").lower()
    i = n.rfind(".")
    return n[i:] if i >= 0 else ""


def _ia_best_file(files: list[dict]) -> dict | None:
    audio = [f for f in files if _ia_ext(f.get("name", "")) in _IA_FORMAT_RANK]
    if not audio:
        return None
    # Primary sort: format preference (low rank wins). Secondary: size
    # descending — when multiple files of the same format exist, the
    # biggest one is usually the complete single-file audiobook
    # instead of a per-chapter split.
    audio.sort(key=lambda f: (
        _IA_FORMAT_RANK.get(_ia_ext(f.get("name", "")), 99),
        -int(f.get("size") or 0),
    ))
    return audio[0]


# Non-English language codes we explicitly skip. IA uses a mix of
# MARC-21 (3-letter, "ger") and ISO 639-2 (also 3-letter) codes;
# we lowercase + match either. ``None`` / "" / "eng" / "en" all pass
# through — most popular English audiobooks ship without a language
# tag, and the false-positive cost of dropping them is too high.
_IA_NON_ENGLISH_LANGS = {
    "ger", "de", "deu",          # German
    "fre", "fra", "fr",          # French
    "spa", "es",                 # Spanish
    "ita", "it",                 # Italian
    "dut", "nld", "nl",          # Dutch
    "swe", "sv",                 # Swedish
    "rus", "ru",                 # Russian
    "por", "pt",                 # Portuguese
    "chi", "zho", "zh",          # Chinese
    "jpn", "ja",                 # Japanese
    "kor", "ko",                 # Korean
    "ara", "ar",                 # Arabic
    "hin", "hi",                 # Hindi
    "tur", "tr",                 # Turkish
    "pol", "pl",                 # Polish
    "ces", "cs", "cze",          # Czech
    "fin", "fi",                 # Finnish
    "nor", "nob", "nno", "no",   # Norwegian
    "dan", "da",                 # Danish
}

# Substring markers in titles that signal a non-English edition even
# when the language field is missing. Catches the dozens of German
# "Hörspiel" items that bury the popular English audiobook in
# downloads-desc sort.
_IA_NON_ENGLISH_TITLE_MARKERS = {
    "hörspiel", "horspiel", "komplettfassung", "ungekürzt",
    "abridged german", "deutsche", "französische", "italiana",
    "ediz. tedesca", "edizione tedesca",
}


def _ia_is_english(doc: dict) -> bool:
    """Conservative English filter: reject documents whose declared
    language is on the non-English block list OR whose title carries
    a language-specific marker. Allows everything else through —
    most IA audiobooks ship without a language tag and dropping them
    would silently kill English search results."""
    lang = (doc.get("language") or "").lower().strip()
    if lang in _IA_NON_ENGLISH_LANGS:
        return False
    if "," in lang:
        # Multilingual entries (e.g. "eng, ger") — accept if English is
        # one of the listed languages, reject if it's only non-English.
        parts = {p.strip() for p in lang.split(",")}
        if not (parts & {"eng", "en"}) and (parts & _IA_NON_ENGLISH_LANGS):
            return False
    title_l = (doc.get("title") or "").lower()
    for marker in _IA_NON_ENGLISH_TITLE_MARKERS:
        if marker in title_l:
            return False
    return True


async def search(title: str, author: str, limit: int = 5) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"{IA_BASE}/advancedsearch.php",
                params={
                    "q": _ia_query(title, author),
                    "fl": "identifier,title,creator,subject,language",
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
                doc_title = doc.get("title") or ""
                if not _ia_title_ok(doc_title, title):
                    continue
                # Filter out non-English editions — the popular 1984
                # search used to land on a German "Hörspiel" radio
                # play (~10x more downloads than the English audio
                # book in the same query, ranked first by IA's
                # downloads-desc sort).
                if not _ia_is_english(doc):
                    continue
                # Fetch file list to find the actual audio file
                meta_r = await c.get(f"{IA_BASE}/metadata/{identifier}", timeout=6)
                if meta_r.status_code != 200:
                    continue
                meta = meta_r.json()
                files = meta.get("files") or []
                best = _ia_best_file(files)
                if not best:
                    continue
                fname = best["name"]
                # Skip archive.org's load balancer (which sometimes
                # 302s to a broken mirror like dn721807.ca.archive.org
                # that 500s on every Range request). The metadata
                # endpoint hands us the canonical storage server +
                # directory; constructing the direct URL avoids the
                # roulette entirely.
                ia_server = meta.get("server") or meta.get("d1") or ""
                ia_dir = meta.get("dir") or ""
                if ia_server and ia_dir:
                    stream_url = f"https://{ia_server}{ia_dir}/{fname}"
                else:
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
