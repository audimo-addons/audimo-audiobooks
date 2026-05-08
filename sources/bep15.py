"""BEP-15 UDP tracker client.

Just enough of the protocol to do a connect+announce round-trip and
read back seed/leech counts plus a compact peer list. Used by the
addon to verify torrents are actually alive before returning them
to the desktop, and to hand verified peer endpoints to the bundled
streaming server so libtorrent can skip DHT on the hot path.

We don't implement scrape (BEP-48 multi-info_hash) — announce returns
the same counts plus peers, and the per-source overhead is the same.
"""
from __future__ import annotations

import asyncio
import os
import random
import socket
import struct
from typing import Optional
from urllib.parse import urlsplit


# BEP-15 magic protocol id sent on every connect.
_MAGIC = 0x41727101980
_ACTION_CONNECT = 0
_ACTION_ANNOUNCE = 1

# Our peer-id. Fixed prefix is the standard "-XXVVVV-" convention; the
# random tail is regenerated per-process so trackers don't see us as
# the same peer across restarts.
_OUR_PEER_ID = b"-AU0010-" + os.urandom(12)


class _UDPProto(asyncio.DatagramProtocol):
    """Single-shot UDP receive: resolve `fut` with the first datagram
    or with an OSError raised by the kernel."""

    def __init__(self) -> None:
        self.fut: asyncio.Future = asyncio.get_running_loop().create_future()

    def datagram_received(self, data: bytes, addr) -> None:
        if not self.fut.done():
            self.fut.set_result(data)

    def error_received(self, exc: Exception) -> None:
        if not self.fut.done():
            self.fut.set_exception(exc)


async def _udp_round_trip(host: str, port: int, payload: bytes, *, timeout: float) -> bytes:
    loop = asyncio.get_running_loop()
    transport, proto = await loop.create_datagram_endpoint(
        _UDPProto, remote_addr=(host, port),
    )
    try:
        transport.sendto(payload)
        return await asyncio.wait_for(proto.fut, timeout=timeout)
    finally:
        transport.close()


async def announce_one(
    tracker_url: str,
    info_hash: bytes,
    *,
    timeout: float = 4.0,
) -> Optional[dict]:
    """Connect+announce against a single UDP tracker.

    Returns ``{"seeders", "leechers", "peers": [(ip, port), ...]}`` on
    success, or ``None`` when the tracker times out, returns garbage,
    or doesn't speak BEP-15. http(s):// trackers are not supported by
    this client and silently return None — caller is responsible for
    filtering the tracker list.
    """
    parts = urlsplit(tracker_url)
    if parts.scheme != "udp":
        return None
    host = parts.hostname
    port = parts.port or 80
    if not host:
        return None

    # connect
    tid = random.randint(0, 0xFFFFFFFF)
    pkt = struct.pack(">QII", _MAGIC, _ACTION_CONNECT, tid)
    try:
        resp = await _udp_round_trip(host, port, pkt, timeout=timeout)
    except (asyncio.TimeoutError, OSError, socket.gaierror):
        return None
    if len(resp) < 16:
        return None
    action, rtid, conn_id = struct.unpack(">IIQ", resp[:16])
    if action != _ACTION_CONNECT or rtid != tid:
        return None

    # announce
    tid = random.randint(0, 0xFFFFFFFF)
    key = random.randint(0, 0xFFFFFFFF)
    pkt = struct.pack(
        ">QII20s20sQQQiIIiH",
        conn_id,
        _ACTION_ANNOUNCE,
        tid,
        info_hash,
        _OUR_PEER_ID,
        0, 0, 0,        # downloaded / left / uploaded
        2,              # event = started
        0,              # ip (0 = use sender)
        key,
        50,             # num_want
        6881,           # listening port (we're not actually serving)
    )
    try:
        resp = await _udp_round_trip(host, port, pkt, timeout=timeout)
    except (asyncio.TimeoutError, OSError, socket.gaierror):
        return None
    if len(resp) < 20:
        return None
    action, rtid = struct.unpack(">II", resp[:8])
    if action != _ACTION_ANNOUNCE or rtid != tid:
        return None
    _interval, leechers, seeders = struct.unpack(">III", resp[8:20])

    peers: list[tuple[str, int]] = []
    body = resp[20:]
    # Compact peer format (BEP-23): 4-byte big-endian IPv4 + 2-byte port.
    for i in range(0, len(body) - 5, 6):
        ip = ".".join(str(b) for b in body[i:i + 4])
        peer_port = struct.unpack(">H", body[i + 4:i + 6])[0]
        if peer_port and not ip.startswith(("0.", "127.")):
            peers.append((ip, peer_port))

    return {"seeders": int(seeders), "leechers": int(leechers), "peers": peers}


async def verify_torrent(
    info_hash_hex: str,
    trackers: list[str],
    *,
    per_tracker_timeout: float = 4.0,
    max_trackers: int = 5,
) -> dict:
    """Run announce against up to ``max_trackers`` UDP trackers in
    parallel. Returns merged ``{seeders, leechers, peers}`` — seeders
    is the max across responding trackers (some return cached counts
    from minutes ago; the max is the freshest signal), peers is the
    union deduped on (ip, port). When every tracker fails the result
    is ``{seeders: 0, leechers: 0, peers: []}`` — caller can decide
    whether to drop the torrent or keep the indexer-supplied seeder
    count and try again later."""
    try:
        ih = bytes.fromhex(info_hash_hex)
    except ValueError:
        return {"seeders": 0, "leechers": 0, "peers": []}
    if len(ih) != 20:
        return {"seeders": 0, "leechers": 0, "peers": []}

    udp_trackers = [t for t in trackers if t.lower().startswith("udp://")]
    chosen = udp_trackers[:max_trackers]
    if not chosen:
        return {"seeders": 0, "leechers": 0, "peers": []}

    results = await asyncio.gather(
        *(announce_one(t, ih, timeout=per_tracker_timeout) for t in chosen),
        return_exceptions=True,
    )

    seeders = 0
    leechers = 0
    peer_set: set[tuple[str, int]] = set()
    responded = False
    for r in results:
        if isinstance(r, dict):
            responded = True
            if r["seeders"] > seeders:
                seeders = r["seeders"]
            if r["leechers"] > leechers:
                leechers = r["leechers"]
            for p in r["peers"]:
                peer_set.add(p)

    return {
        "seeders": seeders,
        "leechers": leechers,
        "peers": [{"ip": ip, "port": port} for ip, port in peer_set],
        "responded": responded,
    }
