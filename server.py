"""audimo-audiobooks — free audiobook source addon.

Sources:
  - Internet Archive (direct MP3/M4B stream, free, no debrid)
  - LibriVox (public domain zip, free, no debrid)
  - AudiobookBay (magnet links, needs debrid or local libtorrent)

AudiobookBay logic is vendored under ``sources/`` so the shipped
PyInstaller binary doesn't depend on the audimo-indexers repo
sitting alongside it at runtime.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.parse
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from sources import archive_org, librivox, bep15
from sources.audiobookbay import search_audiobookbay, _abb_fetch_magnet

# Canonical UDP tracker pool used to BEP-15 verify torrents that don't
# carry their own working trackers. Mirrors the indexers addon's
# _shared.TRACKERS — these are well-known, long-lived public trackers
# that index a wide swath of public torrents and respond reliably.
_TRACKERS = (
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.tracker.cl:1337/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://explodie.org:6969/announce",
    "udp://tracker.torrent.eu.org:451/announce",
)

# AudiobookBay seeder verify config. Search returns up to ~9 rows
# typically; fetching the magnet for each is one HTTP round-trip per
# row, so we cap concurrency to 3 to stay polite. The overall budget
# bounds total search latency — anything not done by the deadline
# stays on the indexer-supplied count (zero, in ABB's case).
_VERIFY_FETCH_SEM = asyncio.Semaphore(3)
_VERIFY_OVERALL_TIMEOUT_S = 8.0
_INFO_HASH_RE = re.compile(r"urn:btih:([0-9a-fA-F]{40})", re.IGNORECASE)

PORT = int(os.environ.get("AUDIMO_ADDON_PORT", 9008))
SETTINGS_PATH = Path(os.environ.get("AUDIMO_ADDON_DATA", Path.home() / ".audimo-audiobooks")) / "settings.json"
MANIFEST_PATH = Path(__file__).resolve().parent / "manifest.json"

_DEFAULT_SETTINGS = {
    "src_archive_enabled": True,
    "src_librivox_enabled": True,
    "src_audiobookbay_enabled": True,
}

app = FastAPI(title="audimo-audiobooks")
# ── DNS-rebinding defense + tightened CORS ───────────────────────
# Mirror of audimo-aio. See that file for the full rationale.
import ipaddress as _ipaddress
import re as _re

_BIND_HOST = (
    os.environ.get("AUDIMO_ADDON_HOST")
    or os.environ.get("TUNNEL_ADDON_HOST")
    or "127.0.0.1"
).strip()
_REMOTE_BIND = _BIND_HOST == "0.0.0.0"
_TRUSTED_HOSTS_ENV = {
    h.strip().lower()
    for h in (os.environ.get("AUDIMO_ADDON_TRUSTED_HOSTS") or "").split(",")
    if h.strip()
}
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_REMOTE_HOST_RE = _re.compile(
    r"^([\w-]+\.)*(local|lan|home)$|^([\w-]+\.)*ts\.net$",
    _re.IGNORECASE,
)


def _strip_host_port(host_header: str) -> str:
    h = (host_header or "").strip()
    if h.startswith("["):
        idx = h.find("]")
        return h[1:idx].lower() if idx > 0 else h.lower()
    if ":" in h:
        return h.split(":", 1)[0].lower()
    return h.lower()


def _host_allowed(host_header: str) -> bool:
    h = _strip_host_port(host_header)
    if not h:
        return False
    if h in _LOOPBACK_HOSTS or h in _TRUSTED_HOSTS_ENV:
        return True
    if not _REMOTE_BIND:
        return False
    try:
        ip = _ipaddress.ip_address(h)
        return ip.is_private or ip.is_loopback
    except ValueError:
        pass
    return bool(_REMOTE_HOST_RE.match(h))


@app.middleware("http")
async def _host_allowlist(request: Request, call_next):
    if not _host_allowed(request.headers.get("host", "")):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "Host not allowed"}, status_code=421)
    return await call_next(request)


_CORS_EXTRA = [
    o.strip()
    for o in (os.environ.get("AUDIMO_ADDON_CORS_EXTRA") or "").split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=(
        r"^(https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?"
        r"|(tauri|app)://([\w-]+\.)?localhost"
        r"|https?://([\w-]+\.)?(local|lan|home)(:\d+)?"
        r"|https?://([\w-]+\.)*ts\.net(:\d+)?)$"
    ),
    allow_origins=_CORS_EXTRA,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Settings ─────────────────────────────────────────────────────────

def _load_settings() -> dict:
    try:
        if SETTINGS_PATH.exists():
            return {**_DEFAULT_SETTINGS, **json.loads(SETTINGS_PATH.read_text())}
    except Exception:
        pass
    return dict(_DEFAULT_SETTINGS)


def _save_settings(s: dict) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(s, indent=2))


# ── Routes ────────────────────────────────────────────────────────────

@app.get("/manifest.json")
async def manifest():
    try:
        data = json.loads(MANIFEST_PATH.read_text())
    except Exception:
        data = {"id": "audimo-audiobooks", "version": "0.1.0"}
    return JSONResponse(data)


@app.get("/settings")
async def get_settings():
    return _load_settings()


@app.post("/settings")
async def post_settings(request: Request):
    body = await request.json()
    s = {**_load_settings(), **body}
    _save_settings(s)
    return s


@app.post("/resolve/sources")
async def resolve_sources(request: Request):
    body = await request.json()
    # Accept both { track: {...} } and flat { title, artist, kind }
    track = body.get("track") or body
    title = (track.get("title") or "").strip()
    author = (track.get("artist") or "").strip()
    kind = (track.get("kind") or "").lower()

    # Only handle audiobook requests
    if kind and kind != "audiobook":
        return {"sources": []}

    if not title:
        return {"sources": []}

    cfg = _load_settings()
    tasks = []

    if cfg.get("src_archive_enabled", True):
        tasks.append(archive_org.search(title, author, limit=3))

    if cfg.get("src_librivox_enabled", True):
        tasks.append(librivox.search(title, author, limit=2))

    if cfg.get("src_audiobookbay_enabled", True):
        tasks.append(_abb_search_wrapped(cfg, title, author))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    sources = []
    for r in results:
        if isinstance(r, list):
            sources.extend(r)

    return {"sources": sources}


_ABB_SKIP = {
    "collection", "omnibus", "box set", "complete works", "collected",
    "trilogy", "anthology", "bundle", "compendium", "complete collection",
    "fiction collection", "hindi", "urdu",
}


_STOPWORDS = {"the", "a", "an", "of", "in", "on", "by", "to", "and", "for", "with"}


def _abb_title_ok(source_name: str, search_title: str) -> bool:
    n = source_name.lower()
    st = search_title.lower()
    for phrase in _ABB_SKIP:
        if phrase in n:
            return False
    # Strip author (everything after " - ")
    title_part = n.split(" - ")[0] if " - " in n else n
    # Comma-separated or semicolon-separated multiple titles
    if title_part.count(",") >= 1 or title_part.count(";") >= 1:
        return False
    # " & " always joins two distinct titles
    if " & " in title_part:
        return False
    # Any significant words (len > 2, not stopwords) appearing BEFORE the first
    # word of the search title indicate a second book prepended to this one
    st_words = [w for w in st.split() if len(w) > 2]
    if st_words:
        first_word = st_words[0]
        idx = title_part.find(first_word)
        if idx > 0:
            before = title_part[:idx]
            alien = [w for w in before.split() if len(w) > 2 and w not in _STOPWORDS]
            if alien:
                return False
    # Must contain all search title words
    matched = sum(1 for w in st_words if w in title_part)
    return matched >= max(1, len(st_words) - 1)


def _parse_magnet(magnet: str) -> tuple[str, list[str]]:
    """Pull info_hash + tracker list out of a magnet URI. Returns
    ``(info_hash_hex, [tracker_url, …])``; either may be empty if the
    magnet is malformed."""
    m = _INFO_HASH_RE.search(magnet or "")
    info_hash = m.group(1).lower() if m else ""
    trackers: list[str] = []
    try:
        qs = urllib.parse.urlparse(magnet).query
        for k, v in urllib.parse.parse_qsl(qs, keep_blank_values=False):
            if k == "tr" and v:
                trackers.append(v)
    except Exception:
        pass
    return info_hash, trackers


async def _abb_verify_one(cfg: dict, source: dict) -> dict:
    """Fetch the AudiobookBay detail page for this row's slug, parse
    the magnet, BEP-15 announce against the magnet's trackers ∪ the
    canonical pool, and stamp ``info_hash`` + a real ``seeders`` count
    on the source. AudiobookBay's HTML listing pages don't expose
    seeder counts so without this every row would show 0 — which is
    indistinguishable from "dead torrent" in the source picker.

    Failure modes are silent: a row that can't be verified passes
    through unchanged. Caller is expected to bound the overall verify
    budget so a slow tracker pool doesn't block search."""
    slug = (source.get("topic_id") or "").strip()
    if not slug:
        return source
    try:
        async with _VERIFY_FETCH_SEM:
            magnet = await _abb_fetch_magnet(cfg, slug) or ""
    except Exception:
        return source
    if not magnet:
        return source
    info_hash, magnet_trackers = _parse_magnet(magnet)
    if not info_hash:
        return source
    # Magnet trackers + canonical pool, dedup. Magnet trackers come
    # first so a torrent's "real" trackers get priority within
    # bep15.verify_torrent's max_trackers cap.
    seen: set[str] = set()
    tracker_pool: list[str] = []
    for tr in [*magnet_trackers, *_TRACKERS]:
        if tr not in seen:
            seen.add(tr)
            tracker_pool.append(tr)
    try:
        health = await bep15.verify_torrent(
            info_hash,
            tracker_pool,
            per_tracker_timeout=4.0,
            max_trackers=5,
        )
    except Exception:
        return source
    out = {**source, "info_hash": info_hash}
    if health.get("seeders", 0) > 0:
        out["seeders"] = int(health["seeders"])
    if health.get("peers"):
        out["peers"] = health["peers"]
    return out


async def _abb_search_wrapped(cfg: dict, title: str, author: str) -> list[dict]:
    try:
        ctx = {"title": title, "artist": author, "kind": "audiobook"}
        raw = await search_audiobookbay(cfg, ctx)
        out = []
        for s in raw:
            if not _abb_title_ok(s.get("name", ""), title):
                continue
            # Stamp the dispatch key resolve.stream looks for (`abb:<slug>`).
            # The indexers-shaped result carries the slug as `topic_id`;
            # without re-projecting it as source_id, the source picker
            # would show the row but resolve.stream would fall through
            # to "Unknown source" and the user couldn't play it.
            slug = (s.get("topic_id") or "").strip()
            if slug:
                s["source_id"] = f"abb:{slug}"
            s["addon_id"] = "audimo-audiobooks"
            out.append(s)

        if not out:
            return out

        # Verify pass: fetch each row's magnet, BEP-15 announce, stamp
        # real seeders. Bounded by a single overall timeout so a slow
        # tracker pool can't blow the search latency budget; any rows
        # that didn't verify by the deadline stay on the indexer's
        # zero-seeder placeholder rather than getting dropped.
        tasks = [asyncio.create_task(_abb_verify_one(cfg, s)) for s in out]
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=_VERIFY_OVERALL_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            for t in tasks:
                if not t.done():
                    t.cancel()
        verified: list[dict] = []
        for original, t in zip(out, tasks):
            if t.done() and not t.cancelled() and not t.exception():
                verified.append(t.result())
            else:
                verified.append(original)
        # Sort by seeders desc — the source picker's default ordering
        # already does this for torrent rows, but stamping it here
        # means the same order survives addon → orchestrator merge
        # even when the picker's ranker is set to the default.
        verified.sort(key=lambda s: int(s.get("seeders") or 0), reverse=True)
        return verified
    except Exception:
        return []


@app.post("/resolve/stream")
async def resolve_stream(request: Request):
    body = await request.json()
    source = body.get("source") or {}
    source_id = source.get("source_id") or ""

    async def _sse(stream_url: str, name: str):
        yield f"data: {json.dumps({'type': 'progress', 'message': 'Connecting…', 'pct': 10})}\n\n"
        yield f"data: {json.dumps({'type': 'ready', 'status': 'done', 'streamUrl': stream_url, 'source': name, 'pct': 100})}\n\n"

    async def _error(msg: str):
        yield f"data: {json.dumps({'type': 'error', 'status': 'error', 'message': msg})}\n\n"

    if source_id.startswith("ia:"):
        url = await archive_org.stream(source_id)
        if url:
            return StreamingResponse(_sse(url, "Internet Archive"), media_type="text/event-stream")
        return StreamingResponse(_error("Could not resolve Internet Archive URL"), media_type="text/event-stream")

    if source_id.startswith("lv:"):
        url = await librivox.stream(source_id)
        if url:
            return StreamingResponse(_sse(url, "LibriVox"), media_type="text/event-stream")
        return StreamingResponse(_error("Could not resolve LibriVox URL"), media_type="text/event-stream")

    # AudiobookBay: source carries a magnet directly
    magnet = source.get("link") or ""
    if magnet.startswith("magnet:"):
        async def _torrent_sse():
            yield f"data: {json.dumps({'type': 'unsupported', 'code': 'torrent_no_debrid', 'magnet': magnet, 'status': 'unsupported'})}\n\n"
        return StreamingResponse(_torrent_sse(), media_type="text/event-stream")

    # ABB: fetch magnet from slug if source_id starts with "abb:"
    if source_id.startswith("abb:"):
        slug = source_id.split(":", 1)[1]
        cfg = _load_settings()
        magnet = await _abb_fetch_magnet(cfg, slug) or ""
        if magnet:
            async def _abb_torrent_sse():
                yield f"data: {json.dumps({'type': 'unsupported', 'code': 'torrent_no_debrid', 'magnet': magnet, 'status': 'unsupported'})}\n\n"
            return StreamingResponse(_abb_torrent_sse(), media_type="text/event-stream")

    async def _not_found():
        yield f"data: {json.dumps({'type': 'error', 'status': 'error', 'message': 'Unknown source'})}\n\n"
    return StreamingResponse(_not_found(), media_type="text/event-stream")


# ── cache.resolve ──────────────────────────────────────────────
#
# Core's resolve flow: when a play hits a cached library row whose
# `addon_id` is ours, core delegates to this endpoint for a fresh
# stream URL. Internet Archive + LibriVox sources return permanent
# upstream URLs, so the cached `streamUrl` is still valid — we just
# echo it back. AudiobookBay magnet entries can't be re-resolved
# without a debrid backend; core's frontend treats a missing
# streamUrl as "addon couldn't refresh" and falls back to whatever
# is on the cached entry, so we surface a concrete error in the
# response shape rather than 404'ing the call.

@app.post("/cache/resolve")
async def cache_resolve(request: Request):
    body = await request.json()
    # Core sends the entire cached entry payload (per addon protocol
    # §6). Most fields are opaque to us; we only care about pulling
    # back a stream URL.
    stream_url = body.get("streamUrl") or body.get("stream_url") or ""
    source = body.get("source") or "Audiobooks"
    mime = body.get("mime_type") or body.get("mimeType") or "audio/mpeg"

    if stream_url:
        return {
            "streamUrl": stream_url,
            "mimeType": mime,
            "source": source,
        }

    # AudiobookBay-sourced rows might have only a magnet link saved
    # — re-resolution would need libtorrent or debrid, neither of
    # which this addon can do. Tell core, so the frontend can
    # surface a useful message instead of pretending playback works.
    return {
        "status": "error",
        "message": "Audiobook source requires libtorrent or debrid to re-resolve — replay it from the original search to refresh.",
    }


@app.get("/configure", response_class=HTMLResponse)
async def configure():
    """Minimal HTML form for the three source toggles. Posts back to
    /settings, then notifies the parent window so the AddonsView shows
    a Saved toast. Settings persist locally in `SETTINGS_PATH` — this
    addon doesn't carry secrets in the URL like the indexer addon
    does, so the form doesn't need to round-trip a new addon URL."""
    s = _load_settings()
    def chk(k: str) -> str:
        return "checked" if s.get(k, True) else ""
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Audiobooks settings</title>
<style>
  body {{ font: 14px -apple-system, system-ui, sans-serif; background:#1a1816; color:#e6e2db; padding:32px; max-width:560px; margin:0 auto; }}
  h1 {{ font-size:18px; margin:0 0 6px; }}
  p.lede {{ color:#8a857d; margin:0 0 24px; font-size:13px; line-height:1.5; }}
  label {{ display:flex; align-items:center; gap:10px; padding:14px 0; border-bottom:1px solid #26231f; cursor:pointer; }}
  label:last-of-type {{ border-bottom:none; }}
  input[type=checkbox] {{ width:18px; height:18px; accent-color:#c9a96a; }}
  .name {{ font-weight:600; }}
  .desc {{ color:#8a857d; font-size:12px; margin-top:2px; }}
  button {{ background:transparent; color:#e6e2db; border:1px solid #c9a96a; padding:10px 20px; font:inherit; cursor:pointer; margin-top:24px; }}
  button:hover {{ background:#c9a96a; color:#1a1816; }}
  #status {{ color:#c9a96a; margin-left:14px; font-size:12px; }}
</style>
</head><body>
<h1>Audiobook sources</h1>
<p class="lede">Toggle which sources the addon queries. Internet Archive and LibriVox stream directly; AudiobookBay returns magnet links that need debrid or the bundled libtorrent server.</p>
<form id="f">
  <label><input type="checkbox" name="src_archive_enabled" {chk("src_archive_enabled")}>
    <span><div class="name">Internet Archive</div><div class="desc">Free direct streams. No account.</div></span></label>
  <label><input type="checkbox" name="src_librivox_enabled" {chk("src_librivox_enabled")}>
    <span><div class="name">LibriVox</div><div class="desc">Public-domain volunteer recordings. Free.</div></span></label>
  <label><input type="checkbox" name="src_audiobookbay_enabled" {chk("src_audiobookbay_enabled")}>
    <span><div class="name">AudiobookBay</div><div class="desc">Magnet links. Needs a debrid backend or the bundled libtorrent server.</div></span></label>
  <button type="submit">Save</button>
  <span id="status"></span>
</form>
<script>
document.getElementById('f').addEventListener('submit', async (e) => {{
  e.preventDefault();
  const data = {{}};
  for (const cb of document.querySelectorAll('input[type=checkbox]')) {{
    data[cb.name] = cb.checked;
  }}
  const r = await fetch('./settings', {{ method:'POST', headers:{{'content-type':'application/json'}}, body: JSON.stringify(data) }});
  document.getElementById('status').textContent = r.ok ? 'Saved.' : 'Save failed.';
  // Notify the AddonsView opener so it shows a toast. The addon URL
  // didn't change (settings are local) but the parent listens for any
  // postMessage with a `url` field — re-sending the current URL is a
  // benign no-op on the registry side.
  if (r.ok && window.opener) {{
    try {{ window.opener.postMessage({{ url: window.location.origin }}, '*'); }} catch (e) {{}}
  }}
}});
</script>
</body></html>
"""
    return HTMLResponse(html)


@app.get("/health")
async def health():
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    # Default to 127.0.0.1; ``0.0.0.0`` exposes the addon to the LAN
    # and requires an explicit opt-in via AUDIMO_ADDON_HOST.
    _host = (
        os.getenv("AUDIMO_ADDON_HOST")
        or os.getenv("TUNNEL_ADDON_HOST")
        or "127.0.0.1"
    )
    uvicorn.run(app, host=_host, port=PORT)
