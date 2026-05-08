"""audimo-audiobooks — free audiobook source addon.

Sources:
  - Internet Archive (direct MP3/M4B stream, free, no debrid)
  - LibriVox (public domain zip, free, no debrid)
  - AudiobookBay (magnet links, needs debrid or local libtorrent)

AudiobookBay code is imported from audimo-indexers when running
natively (co-located dev). In the shipped binary it is bundled
directly.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from sources import archive_org, librivox

# ── AudiobookBay: import from indexers repo if available, else stub ──
try:
    _indexers_path = Path(__file__).resolve().parent.parent / "audimo-indexers"
    if _indexers_path.is_dir() and str(_indexers_path) not in sys.path:
        sys.path.insert(0, str(_indexers_path))
    from indexers.audiobookbay import search_audiobookbay, _abb_fetch_magnet
    _HAS_ABB = True
except ImportError:
    _HAS_ABB = False

PORT = int(os.environ.get("AUDIMO_ADDON_PORT", 9008))
SETTINGS_PATH = Path(os.environ.get("AUDIMO_ADDON_DATA", Path.home() / ".audimo-audiobooks")) / "settings.json"
MANIFEST_PATH = Path(__file__).resolve().parent / "manifest.json"

_DEFAULT_SETTINGS = {
    "src_archive_enabled": True,
    "src_librivox_enabled": True,
    "src_audiobookbay_enabled": True,
}

app = FastAPI(title="audimo-audiobooks")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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

    if cfg.get("src_audiobookbay_enabled", True) and _HAS_ABB:
        tasks.append(_abb_search_wrapped(cfg, title, author))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    sources = []
    for r in results:
        if isinstance(r, list):
            sources.extend(r)

    return {"sources": sources}


async def _abb_search_wrapped(cfg: dict, title: str, author: str) -> list[dict]:
    try:
        ctx = {"title": title, "artist": author, "kind": "audiobook"}
        raw = await search_audiobookbay(cfg, ctx)
        # Stamp with our addon_id
        for s in raw:
            s["addon_id"] = "audimo-audiobooks"
        return raw
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
    if source_id.startswith("abb:") and _HAS_ABB:
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


@app.get("/health")
async def health():
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
