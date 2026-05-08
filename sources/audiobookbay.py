"""AudiobookBay (audiobook-specific tracker; listings-only).

Public WordPress-themed site. The search page returns post stubs
with title + slug; we expose the slug as `topic_id`. resolve.stream
fetches the detail page on demand and reassembles the magnet from
the info hash + tracker list (the literal magnet:?… URI was
stripped from page templates years ago).
"""
from __future__ import annotations

import re
import urllib.parse

import httpx


ABB_DEFAULT_BASE = "https://audiobookbay.fi"
ABB_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)
_ABB_BOOKMARK_RE = re.compile(
    r'<a\s+[^>]*?href="([^"]+)"[^>]*?\brel="bookmark"[^>]*>([^<]+)</a>',
    re.IGNORECASE,
)
_ABB_INFOHASH_RE = re.compile(
    r"<td>\s*Info\s*Hash\s*:?\s*</td>\s*<td>\s*([a-fA-F0-9]{40})\s*</td>",
    re.IGNORECASE,
)
_ABB_TRACKER_RE = re.compile(
    r"<td(?:\s+[^>]*)?>\s*((?:udp|http|wss?)://[^\s<]+)\s*</td>",
    re.IGNORECASE,
)
_ABB_MAGNET_RE = re.compile(
    r'magnet:\?xt=urn:btih:([A-Fa-f0-9]{40}|[A-Z2-7]{32})[^"\'\s<>]*',
    re.IGNORECASE,
)
_ABB_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "into",
    "audiobook", "book", "vol", "volume", "edition", "of", "in",
    "on", "at", "by", "to", "an", "is", "as", "or",
}


def _abb_strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", s).strip()


def _abb_base(cfg: dict) -> str:
    base = (cfg.get("audiobookbay_base") or ABB_DEFAULT_BASE).strip()
    return base.rstrip("/")


def _abb_query_words(s: str) -> set[str]:
    toks = re.split(r"[^a-z0-9]+", (s or "").lower())
    return {w for w in toks if len(w) >= 4 and w not in _ABB_STOPWORDS}


def _abb_build_query(title: str, artist: str) -> str:
    """Empirically WP search behaves best with 2-3 distinctive lowercase
    tokens drawn from the title (and one author word as tiebreaker)."""
    title_toks: list[str] = []
    seen: set[str] = set()
    for w in re.split(r"\s+", (title or "").strip()):
        stripped = re.sub(r"[^A-Za-z0-9']", "", w).lower()
        if not stripped or stripped in seen or stripped in _ABB_STOPWORDS:
            continue
        if len(stripped) < 3:
            continue
        seen.add(stripped)
        title_toks.append(stripped)
        if len(title_toks) >= 3:
            break
    author_tok = ""
    for w in reversed(re.split(r"\s+", (artist or "").strip())):
        stripped = re.sub(r"[^A-Za-z0-9']", "", w).lower()
        if stripped and len(stripped) >= 4 and stripped not in seen:
            author_tok = stripped
            break
    if author_tok:
        title_toks.append(author_tok)
    return " ".join(title_toks)


_ABB_AUTHOR_DASH_RE = re.compile(r"\s+[-–—]\s+(.+?)\s+[-–—]\s+", re.IGNORECASE)


async def search_audiobookbay_books(cfg: dict, q: str, limit: int = 30) -> list[dict]:
    """Discovery search — returns book-shaped results (title/author/cover)
    rather than the source-shaped entries `search_audiobookbay` returns
    for resolve.sources.

    Same WP search backend; we just shape the output for the
    audiobook-discovery view instead of the source picker. AudiobookBay
    listings don't surface a separate cover URL in the results page,
    so cover stays empty — the frontend falls back to a Monogram.

    Author parsing is a best-effort string split — listings are titled
    in many forms ("Author - Title", "Title by Author", "Title
    (Unabridged)"). When parsing fails we leave author empty rather
    than guessing.
    """
    if not q:
        return []
    base = _abb_base(cfg)
    try:
        async with httpx.AsyncClient(
            timeout=15, follow_redirects=True,
            headers={"User-Agent": ABB_UA},
        ) as client:
            r = await client.get(f"{base}/", params={"s": q})
            if r.status_code != 200:
                return []
            body = r.text
    except Exception:
        return []
    out: list[dict] = []
    seen_slugs: set[str] = set()
    for href, raw_title in _ABB_BOOKMARK_RE.findall(body):
        title_text = _abb_strip_tags(raw_title)
        if not title_text:
            continue
        slug = href.rstrip("/").rsplit("/", 1)[-1]
        if not slug or slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        # Best-effort author split. ABB titles vary
        # ("Title - Author", "Author - Title", multi-dash variants
        # like "Title - Edition - Author"). Heuristic: split on " - "
        # and take the last chunk as author IF it looks name-shaped
        # (short, ≤4 words, contains a capital letter or period).
        # Otherwise leave author blank — the frontend renders "—" and
        # the user can still recognise the book.
        author = ""
        title_clean = title_text
        if " - " in title_text:
            parts = [p.strip() for p in title_text.split(" - ") if p.strip()]
            if len(parts) >= 2:
                tail = parts[-1]
                word_count = len(tail.split())
                looks_like_name = (
                    len(tail) <= 60
                    and word_count <= 5
                    and any(c.isupper() for c in tail)
                )
                if looks_like_name:
                    author = tail
                    title_clean = " - ".join(parts[:-1])
        out.append({
            "title": title_clean,
            "author": author,
            "cover": "",
            "isbn": "",
            "year": 0,
            "subjects": ["audiobookbay"],
            "_abb_slug": slug,
        })
        if len(out) >= limit:
            break
    return out


async def search_audiobookbay(cfg: dict, ctx: dict) -> list[dict]:
    """Returns lazy entries — info_hash is empty until resolve.stream
    fetches the detail page. Filters out the recent-uploads fallback
    by requiring at least one query token to overlap the title."""
    if ctx.get("kind") != "audiobook":
        return []
    base = _abb_base(cfg)
    artist = ctx.get("artist", "")
    title = ctx.get("title", "")
    q = _abb_build_query(title, artist)
    if not q:
        return []
    try:
        async with httpx.AsyncClient(
            timeout=15, follow_redirects=True,
            headers={"User-Agent": ABB_UA},
        ) as client:
            r = await client.get(f"{base}/", params={"s": q})
            if r.status_code != 200:
                print(f"[abb] search status={r.status_code} q={q!r}")
                return []
            body = r.text
    except Exception as e:
        print(f"[abb] search error q={q!r}: {type(e).__name__}: {e}")
        return []

    query_tokens = _abb_query_words(f"{title} {artist}")
    out: list[dict] = []
    seen_slugs: set[str] = set()
    for href, raw_title in _ABB_BOOKMARK_RE.findall(body):
        title_text = _abb_strip_tags(raw_title)
        if not title_text:
            continue
        slug = href.rstrip("/").rsplit("/", 1)[-1]
        if not slug or slug in seen_slugs:
            continue
        if query_tokens and not (query_tokens & _abb_query_words(title_text)):
            continue
        seen_slugs.add(slug)
        out.append({
            "name": title_text,
            "seeders": 0,
            "size": 0,
            "rd_link": "",
            "link_type": "magnet",
            "source": "audiobookbay",
            "info_hash": "",
            "topic_id": slug,
        })
    print(f"[abb] q={q!r} returned={len(out)} (post-filter)")
    return out[:25]


async def _abb_fetch_magnet(cfg: dict, slug: str) -> str | None:
    """Fetch one detail page and reconstruct a magnet URI from the
    info hash + tracker list. Falls back to a bare info-hash magnet
    if no trackers are listed — libtorrent finds peers via DHT/PEX."""
    base = _abb_base(cfg)
    try:
        async with httpx.AsyncClient(
            timeout=15, follow_redirects=True,
            headers={"User-Agent": ABB_UA},
        ) as client:
            body = ""
            for path in (f"/abss/{slug}/", f"/audio-books/{slug}/", f"/{slug}/"):
                r = await client.get(f"{base}{path}")
                if r.status_code == 200:
                    body = r.text
                    break
            if not body:
                print(f"[abb] detail no usable path slug={slug}")
                return None
            m = _ABB_MAGNET_RE.search(body)
            if m:
                return m.group(0)
            ih_m = _ABB_INFOHASH_RE.search(body)
            if not ih_m:
                print(f"[abb] detail no info hash slug={slug}")
                return None
            info_hash = ih_m.group(1).lower()
            trackers = _ABB_TRACKER_RE.findall(body)
            dedup: list[str] = []
            seen: set[str] = set()
            for t in trackers:
                t = t.strip()
                if not t or t in seen:
                    continue
                seen.add(t)
                dedup.append(t)
                if len(dedup) >= 30:
                    break
            tr_params = "".join(
                "&tr=" + urllib.parse.quote(t, safe="") for t in dedup
            )
            magnet = f"magnet:?xt=urn:btih:{info_hash}{tr_params}"
            print(f"[abb] detail slug={slug} ih={info_hash[:12]} trackers={len(dedup)}")
            return magnet
    except Exception as e:
        print(f"[abb] detail error slug={slug}: {type(e).__name__}: {e}")
    return None
