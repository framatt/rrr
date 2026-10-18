#!/usr/bin/env python3
"""Torrent source search and WebTorrent bridge management.

The player normally resolves streams from the catalog feed.  When that fails
(or the user asks for another source) this module searches public torrent
indexes, resolves an IMDb id for Stremio-style addons, and talks to the local
Node WebTorrent bridge that performs the actual swarm download.
"""

from __future__ import annotations

import atexit
import copy
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

ROOT = Path(__file__).resolve().parent
BRIDGE_DIR = ROOT / "torrent_bridge"
CACHE_DIR = ROOT / ".app_cache"
IMDB_CACHE_PATH = CACHE_DIR / "torrent-imdb.json"
BRIDGE_LOG_PATH = CACHE_DIR / "torrent-bridge.log"
BRIDGE_DATA_DIR = CACHE_DIR / "torrents"

TORRENTIO_BASE = os.environ.get("TORRENTIO_BASE", "https://torrentio.strem.fun").rstrip("/")
APIBAY_BASE = os.environ.get("APIBAY_BASE", "https://apibay.org").rstrip("/")
YTS_BASE = os.environ.get("YTS_BASE", "https://yts.mx").rstrip("/")
EZTV_BASE = os.environ.get("EZTV_BASE", "https://eztv.re").rstrip("/")
IMDB_SUGGEST_BASE = os.environ.get(
    "IMDB_SUGGEST_BASE", "https://v2.sg.media-imdb.com/suggestion/x"
).rstrip("/")
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "").strip()
BRIDGE_URL = os.environ.get("TORRENT_BRIDGE_URL", "").strip().rstrip("/")
BRIDGE_PORT = int(os.environ.get("TORRENT_BRIDGE_PORT", "0") or 0)
BRIDGE_TOKEN = os.environ.get("TORRENT_BRIDGE_TOKEN", "").strip()
NODE_BIN = os.environ.get("NODE_BIN", "").strip()

DEFAULT_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://tracker.dler.org:6969/announce",
    "udp://open.dstud.io:6969/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://tracker.tiny-vps.com:6969/announce",
    "udp://explodie.org:6969/announce",
    "udp://tracker1.bt.moack.co.kr:80/announce",
    "udp://tracker.qu.ax:6969/announce",
    "udp://tracker.leech.ie:1337/announce",
    "udp://tracker.ololosh.space:6969/announce",
    "udp://tracker.theoks.net:6969/announce",
    "wss://tracker.openwebtorrent.com",
    "wss://tracker.btorrent.xyz",
    "wss://tracker.webtorrent.dev",
]

QUALITY_RE = re.compile(
    r"(?<!\d)(2160p|1440p|1080p|720p|480p|360p|4k|uhd)(?!\d)", re.IGNORECASE
)
SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(TB|GB|MB|KB)", re.IGNORECASE)
SEED_RE = re.compile("[\U0001f464\U0001f465]\\s*(\\d+)")
PROVIDER_RE = re.compile("\u2699\ufe0f?\\s*([^\\n]+)")
INFO_HASH_RE = re.compile(r"^[a-fA-F0-9]{40}$")
IMDB_RE = re.compile(r"^tt\d{5,10}$", re.IGNORECASE)
VIDEO_EXT_RE = re.compile(r"\.(mp4|m4v|webm|mkv|mov|avi|ts|m2ts|flv|wmv|ogv)$", re.IGNORECASE)
WEB_FRIENDLY_RE = re.compile(r"\.(mp4|m4v|webm|ogv)$", re.IGNORECASE)

SEARCH_TTL = 10 * 60
IMDB_TTL = 30 * 86400
MAX_RESULTS = 160

_tls = threading.local()
_cache_lock = threading.Lock()
_search_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_imdb_lock = threading.Lock()
_imdb_cache: dict[str, dict[str, Any]] | None = None

_bridge_lock = threading.Lock()
_bridge_state: dict[str, Any] = {
    "proc": None,
    "url": "",
    "token": "",
    "port": 0,
    "log": None,
    "started_at": 0.0,
    "last_error": "",
}


class TorrentError(RuntimeError):
    """Raised when torrent search or the local bridge fails."""


def _session() -> requests.Session:
    session = getattr(_tls, "session", None)
    if session is None:
        session = requests.Session()
        session.trust_env = False
        session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) PrivateMirror/1.0",
                "Accept": "application/json,text/plain,*/*",
            }
        )
        _tls.session = session
    return session


def _get_json(url: str, params: dict[str, Any] | None = None, timeout: float = 14.0) -> Any:
    response = _session().get(url, params=params, timeout=(4.0, timeout))
    response.raise_for_status()
    return response.json()


def normalize_title(value: Any) -> str:
    text = str(value or "").lower()
    text = re.sub(r"\(\s*(?:19|20)\d{2}\s*\)", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_quality(text: Any) -> tuple[str, int]:
    match = QUALITY_RE.search(str(text or ""))
    if not match:
        return "", 0
    raw = match.group(1).lower()
    if raw in ("4k", "uhd"):
        return "2160p", 2160
    value = int(raw[:-1])
    return f"{value}p", value


def parse_size(text: Any) -> int:
    match = SIZE_RE.search(str(text or ""))
    if not match:
        return 0
    value = float(match.group(1))
    unit = match.group(2).upper()
    factor = {"KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}[unit]
    return int(value * factor)


def parse_seeds(text: Any) -> int:
    match = SEED_RE.search(str(text or ""))
    if match:
        return int(match.group(1))
    return 0


def magnet_for(info_hash: str, name: str = "", trackers: list[str] | None = None) -> str:
    parts = [f"xt=urn:btih:{info_hash}"]
    if name:
        parts.append("dn=" + quote(str(name)[:220], safe=""))
    seen: set[str] = set()
    for tracker in DEFAULT_TRACKERS + list(trackers or []):
        if not tracker or tracker in seen:
            continue
        seen.add(tracker)
        parts.append("tr=" + quote(tracker, safe=":/?&=%"))
    return "magnet:?" + "&".join(parts)


def _info_hash(value: Any) -> str:
    text = str(value or "").strip()
    return text.lower() if INFO_HASH_RE.match(text) else ""


def info_hash_from_magnet(magnet: str) -> str:
    match = re.search(r"urn(?:%3a|:)btih(?:%3a|:)([a-fA-F0-9]{40})", str(magnet or ""), re.IGNORECASE)
    return match.group(1).lower() if match else ""


def _web_friendly(name: Any) -> bool:
    return bool(WEB_FRIENDLY_RE.search(str(name or "")))


def _result(
    *,
    info_hash: str,
    title: str,
    provider: str,
    quality: str,
    quality_value: int,
    size_bytes: int,
    seeds: int,
    peers: int,
    file_index: int | None = None,
    file_hint: str = "",
    filename: str = "",
    source: str = "",
    magnet: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    info_hash = _info_hash(info_hash)
    if not info_hash:
        return None
    label = filename or title
    result = {
        "infoHash": info_hash,
        "title": title.strip() or filename or info_hash,
        "provider": provider,
        "source": source,
        "quality": quality,
        "qualityValue": quality_value,
        "sizeBytes": max(0, int(size_bytes or 0)),
        "seeders": max(0, int(seeds or 0)),
        "peers": max(0, int(peers or seeds or 0)),
        "fileIndex": file_index if isinstance(file_index, int) and file_index >= 0 else None,
        "fileHint": file_hint or filename or "",
        "filename": filename or "",
        "webFriendly": _web_friendly(label),
        "magnet": magnet or magnet_for(info_hash, label),
    }
    if extra:
        result.update(extra)
    return result


# -- IMDb resolution --------------------------------------------------------


def _load_imdb_cache() -> dict[str, dict[str, Any]]:
    global _imdb_cache
    if _imdb_cache is not None:
        return _imdb_cache
    data: dict[str, dict[str, Any]] = {}
    try:
        if IMDB_CACHE_PATH.exists():
            parsed = json.loads(IMDB_CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                data = {
                    str(key): value
                    for key, value in parsed.items()
                    if isinstance(value, dict) and value.get("imdbId")
                }
    except (OSError, ValueError):
        data = {}
    _imdb_cache = data
    return data


def _save_imdb_cache() -> None:
    if _imdb_cache is None:
        return
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    temp = IMDB_CACHE_PATH.with_name(IMDB_CACHE_PATH.name + f".{secrets.token_hex(4)}.tmp")
    try:
        temp.write_text(json.dumps(_imdb_cache, ensure_ascii=False, indent=1), encoding="utf-8")
        temp.replace(IMDB_CACHE_PATH)
    except OSError:
        temp.unlink(missing_ok=True)


def _imdb_key(media_type: str, tmdb_id: Any, title: str, year: Any) -> str:
    if tmdb_id:
        return f"tmdb:{media_type}:{tmdb_id}"
    return f"title:{media_type}:{normalize_title(title)}:{str(year or '')[:4]}"


def _pick_imdb(results: list[dict[str, Any]], media_type: str, title: str, year: Any) -> str:
    wanted = normalize_title(title)
    wanted_year = str(year or "")[:4]
    if not wanted:
        return ""
    best_id = ""
    best_score = -1.0
    for rank, item in enumerate(results[:20]):
        imdb_id = str(item.get("id") or "")
        if not IMDB_RE.match(imdb_id):
            continue
        candidate = normalize_title(item.get("l") or item.get("name"))
        if not candidate:
            continue
        if candidate == wanted:
            score = 70.0
        elif wanted in candidate or candidate in wanted:
            score = 32.0
        else:
            continue
        candidate_year = str(item.get("y") or item.get("yr") or "")[:4]
        if wanted_year and candidate_year == wanted_year:
            score += 45
        elif wanted_year and candidate_year.isdigit() and abs(int(wanted_year) - int(candidate_year)) <= 1:
            score += 12
        qid = str(item.get("qid") or "")
        if media_type == "tv":
            if qid in ("tvSeries", "tvMiniSeries", "tvMovie"):
                score += 28
            elif qid:
                score -= 10
        else:
            if qid in ("movie", "tvMovie"):
                score += 28
            elif qid:
                score -= 10
        score -= rank * 0.5
        if score > best_score:
            best_score = score
            best_id = imdb_id
    return best_id if best_score >= 0 else ""


def _imdb_from_tmdb(media_type: str, tmdb_id: Any) -> str:
    if not (TMDB_API_KEY and tmdb_id):
        return ""
    try:
        data = _get_json(
            f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}/external_ids",
            params={"api_key": TMDB_API_KEY},
            timeout=10,
        )
    except (requests.RequestException, ValueError):
        return ""
    imdb_id = str(data.get("imdb_id") or "")
    return imdb_id if IMDB_RE.match(imdb_id) else ""


def _imdb_from_suggestion(media_type: str, title: str, year: Any) -> str:
    query = re.sub(r"\s+", " ", str(title or "")).strip()[:80]
    if not query:
        return ""
    try:
        data = _get_json(f"{IMDB_SUGGEST_BASE}/{quote(query, safe='')}.json", timeout=10)
    except (requests.RequestException, ValueError):
        return ""
    results = data.get("d") if isinstance(data, dict) else None
    if not isinstance(results, list):
        return ""
    return _pick_imdb(results, media_type, title, year)


def resolve_imdb(
    media_type: str,
    tmdb_id: Any = None,
    title: str = "",
    year: Any = "",
    *,
    allow_network: bool = True,
) -> str:
    media_type = "tv" if str(media_type).lower() == "tv" else "movie"
    key = _imdb_key(media_type, tmdb_id, title, year)
    now = time.time()
    with _imdb_lock:
        cached = _load_imdb_cache().get(key)
    if cached and now - float(cached.get("at") or 0) < IMDB_TTL:
        return str(cached.get("imdbId") or "")
    if not allow_network:
        return ""
    imdb_id = _imdb_from_tmdb(media_type, tmdb_id) or _imdb_from_suggestion(media_type, title, year)
    if imdb_id:
        with _imdb_lock:
            cache = _load_imdb_cache()
            cache[key] = {"imdbId": imdb_id, "at": now}
            _save_imdb_cache()
    return imdb_id


# -- providers --------------------------------------------------------------


def _torrentio(imdb_id: str, media_type: str, season: int, episode: int) -> list[dict[str, Any]]:
    if media_type == "tv":
        url = f"{TORRENTIO_BASE}/stream/series/{imdb_id}:{int(season)}:{int(episode)}.json"
    else:
        url = f"{TORRENTIO_BASE}/stream/movie/{imdb_id}.json"
    data = _get_json(url, timeout=18)
    streams = data.get("streams") if isinstance(data, dict) else None
    if not isinstance(streams, list):
        return []
    results: list[dict[str, Any]] = []
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        info_hash = _info_hash(stream.get("infoHash"))
        if not info_hash:
            continue
        name = str(stream.get("name") or "")
        title_text = str(stream.get("title") or "")
        behavior = stream.get("behaviorHints") or {}
        lines = [line.strip() for line in title_text.splitlines() if line.strip()]
        filename = str(behavior.get("filename") or "").strip()
        if not filename and len(lines) > 1 and VIDEO_EXT_RE.search(lines[1]):
            filename = lines[1]
        display = lines[0] if lines else filename or info_hash
        quality, quality_value = parse_quality(f"{name} {display} {filename}")
        source_match = PROVIDER_RE.search(title_text)
        source = source_match.group(1).strip() if source_match else ""
        seeds = parse_seeds(title_text)
        size_bytes = parse_size(title_text)
        file_index = stream.get("fileIdx")
        result = _result(
            info_hash=info_hash,
            title=display,
            provider="Torrentio",
            source=source or "addon",
            quality=quality,
            quality_value=quality_value,
            size_bytes=size_bytes,
            seeds=seeds,
            peers=seeds,
            file_index=int(file_index) if isinstance(file_index, int) else None,
            file_hint=filename,
            filename=filename,
        )
        if result:
            results.append(result)
    return results


def _apibay(
    queries: list[str], media_type: str, season: int, episode: int, year: str = ""
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen_queries = set()
    for query in queries:
        query = re.sub(r"\s+", " ", str(query or "")).strip()
        if not query or query.lower() in seen_queries:
            continue
        seen_queries.add(query.lower())
        try:
            data = _get_json(f"{APIBAY_BASE}/q.php", params={"q": query}, timeout=12)
        except (requests.RequestException, ValueError):
            continue
        if not isinstance(data, list):
            continue
        for row in data[:60]:
            if not isinstance(row, dict):
                continue
            info_hash = _info_hash(row.get("info_hash"))
            if not info_hash or info_hash == "0" * 40:
                continue
            name = str(row.get("name") or "")
            if not name or name.lower().startswith("no results"):
                continue
            lowered = name.lower()
            if media_type == "tv":
                episode_tag = f"s{int(season):02d}e{int(episode):02d}"
                alt_tag = f"{int(season)}x{int(episode):02d}"
                season_tag = f"s{int(season):02d}"
                if not (
                    re.search(rf"(?<![a-z0-9]){re.escape(episode_tag)}(?![0-9])", lowered)
                    or re.search(rf"(?<![a-z0-9]){re.escape(alt_tag)}(?![0-9])", lowered)
                    or re.search(rf"(?<![a-z0-9]){re.escape(season_tag)}(?![0-9])", lowered)
                    or "complete series" in lowered
                ):
                    continue
            elif year:
                years = re.findall(r"(?:19|20)\d{2}", lowered)
                if years and year not in years:
                    continue
            quality, quality_value = parse_quality(name)
            try:
                size_bytes = int(row.get("size") or 0)
            except (TypeError, ValueError):
                size_bytes = 0
            try:
                seeds = int(row.get("seeders") or 0)
                peers = seeds + int(row.get("leechers") or 0)
            except (TypeError, ValueError):
                seeds = peers = 0
            hint = f"S{int(season):02d}E{int(episode):02d}" if media_type == "tv" else ""
            result = _result(
                info_hash=info_hash,
                title=name,
                provider="The Pirate Bay",
                source="apibay",
                quality=quality,
                quality_value=quality_value,
                size_bytes=size_bytes,
                seeds=seeds,
                peers=peers,
                file_hint=hint,
                filename=name,
            )
            if result:
                results.append(result)
    return results


def _yts(title: str, year: Any, imdb_id: str) -> list[dict[str, Any]]:
    query = imdb_id or str(title or "")
    if not query:
        return []
    try:
        data = _get_json(
            f"{YTS_BASE}/api/v2/list_movies.json",
            params={"query_term": query, "limit": 20, "sort_by": "seeds"},
            timeout=14,
        )
    except (requests.RequestException, ValueError):
        return []
    movies = ((data.get("data") or {}) if isinstance(data, dict) else {}).get("movies") or []
    if not isinstance(movies, list):
        return []
    wanted = normalize_title(title)
    wanted_year = str(year or "")[:4]
    results: list[dict[str, Any]] = []
    for movie in movies:
        if not isinstance(movie, dict):
            continue
        if imdb_id and str(movie.get("imdb_code") or "").lower() != imdb_id.lower():
            continue
        if not imdb_id and wanted and normalize_title(movie.get("title")) != wanted:
            continue
        if not imdb_id and wanted_year and str(movie.get("year") or "") != wanted_year:
            continue
        for torrent in movie.get("torrents") or []:
            if not isinstance(torrent, dict):
                continue
            info_hash = _info_hash(torrent.get("hash"))
            if not info_hash:
                continue
            quality = str(torrent.get("quality") or "")
            quality_value = int(re.sub(r"\D", "", quality) or 0)
            try:
                size_bytes = int(torrent.get("size_bytes") or 0)
            except (TypeError, ValueError):
                size_bytes = 0
            try:
                seeds = int(torrent.get("seeds") or 0)
                peers = seeds + int(torrent.get("peers") or 0)
            except (TypeError, ValueError):
                seeds = peers = 0
            name = f"{movie.get('title_long') or movie.get('title') or ''} [{quality} {torrent.get('type') or ''}]".strip()
            result = _result(
                info_hash=info_hash,
                title=name,
                provider="YTS",
                source="yts",
                quality=quality or parse_quality(name)[0],
                quality_value=quality_value or parse_quality(name)[1],
                size_bytes=size_bytes,
                seeds=seeds,
                peers=peers,
                filename=name,
                extra={"codec": torrent.get("video_codec") or ""},
            )
            if result:
                result["webFriendly"] = True
                results.append(result)
    return results


def _eztv(imdb_id: str, season: int, episode: int) -> list[dict[str, Any]]:
    numeric = imdb_id[2:] if imdb_id.lower().startswith("tt") else imdb_id
    if not numeric.isdigit():
        return []
    try:
        data = _get_json(
            f"{EZTV_BASE}/api/get-torrents",
            params={"imdb_id": numeric, "limit": 100, "page": 1},
            timeout=16,
        )
    except (requests.RequestException, ValueError):
        return []
    rows = data.get("torrents") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    results: list[dict[str, Any]] = []
    wanted_episode = f"s{int(season):02d}e{int(episode):02d}"
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            row_season = int(row.get("season") or 0)
            row_episode = int(row.get("episode") or 0)
        except (TypeError, ValueError):
            row_season = row_episode = 0
        filename = str(row.get("filename") or re.sub(r"\[[^\]]+\]$", "", str(row.get("title") or "")))
        pack = row_episode == 0
        matches = row_season == int(season) and (row_episode == int(episode) or pack)
        if not matches and wanted_episode in filename.lower():
            matches = True
        if not matches:
            continue
        info_hash = _info_hash(row.get("hash"))
        if not info_hash:
            continue
        magnet = str(row.get("magnet_url") or "")
        try:
            seeds = int(row.get("seeds") or 0)
            peers = seeds + int(row.get("peers") or 0)
        except (TypeError, ValueError):
            seeds = peers = 0
        try:
            size_bytes = int(row.get("size_bytes") or 0)
        except (TypeError, ValueError):
            size_bytes = 0
        quality, quality_value = parse_quality(filename)
        result = _result(
            info_hash=info_hash,
            title=filename,
            provider="EZTV",
            source="eztv",
            quality=quality,
            quality_value=quality_value,
            size_bytes=size_bytes,
            seeds=seeds,
            peers=peers,
            file_hint=wanted_episode,
            filename=filename,
            magnet=magnet,
        )
        if result:
            results.append(result)
    return results


def _merge_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[Any, dict[str, Any]] = {}
    for result in results:
        info_hash = result["infoHash"]
        file_index = result.get("fileIndex")
        key: Any = info_hash
        current = merged.get(key)
        if current is not None:
            current_index = current.get("fileIndex")
            if current_index is not None and file_index is not None and current_index != file_index:
                key = (info_hash, file_index)
                current = merged.get(key)
        if not current:
            result["providers"] = [result["provider"]] if result.get("provider") else []
            merged[key] = result
            continue
        providers = set(current.get("providers") or [current.get("provider")])
        providers.add(result.get("provider"))
        current["providers"] = sorted(name for name in providers if name)
        if result.get("seeders", 0) > current.get("seeders", 0):
            current["seeders"] = result["seeders"]
            current["peers"] = max(current.get("peers") or 0, result.get("peers") or 0)
        if result.get("sizeBytes", 0) > current.get("sizeBytes", 0):
            current["sizeBytes"] = result["sizeBytes"]
        if result.get("fileIndex") is not None and current.get("fileIndex") is None:
            current["fileIndex"] = result["fileIndex"]
        if result.get("fileHint") and not current.get("fileHint"):
            current["fileHint"] = result["fileHint"]
        if result.get("webFriendly"):
            current["webFriendly"] = True
        if result.get("qualityValue", 0) > current.get("qualityValue", 0):
            current["quality"] = result["quality"]
            current["qualityValue"] = result["qualityValue"]
    ordered = list(merged.values())
    ordered.sort(
        key=lambda item: (
            int(item.get("seeders") or 0),
            int(item.get("qualityValue") or 0),
            int(item.get("sizeBytes") or 0),
        ),
        reverse=True,
    )
    return ordered[:MAX_RESULTS]


def _split_query(query: str) -> tuple[str, str]:
    text = re.sub(r"\s+", " ", str(query or "")).strip()
    year = ""
    match = re.search(r"\(?\b((?:19|20)\d{2})\)?\s*$", text)
    if match:
        year = match.group(1)
        text = text[: match.start()].strip(" -()[]")
    return text, year


def search(
    media_type: str = "movie",
    *,
    tmdb_id: Any = None,
    title: str = "",
    year: Any = "",
    season: int = 1,
    episode: int = 1,
    query: str = "",
    imdb_id: str = "",
    refresh: bool = False,
) -> dict[str, Any]:
    media_type = "tv" if str(media_type).lower() == "tv" else "movie"
    try:
        season = max(1, int(season or 1))
        episode = max(1, int(episode or 1))
    except (TypeError, ValueError):
        season = episode = 1

    if query and not title:
        title, parsed_year = _split_query(query)
        year = year or parsed_year
    title = str(title or "").strip()
    year = str(year or "").strip()[:4]
    if not title and not imdb_id:
        raise TorrentError("A title or IMDb id is required.")

    imdb_id = str(imdb_id or "").strip()
    if imdb_id and not IMDB_RE.match(imdb_id):
        raise TorrentError("The IMDb id is invalid.")
    cache_key = json.dumps(
        {
            "type": media_type,
            "tmdbId": str(tmdb_id or ""),
            "title": title,
            "year": year,
            "season": season,
            "episode": episode,
            "imdb": imdb_id,
            "q": str(query or ""),
        },
        sort_keys=True,
    )
    now = time.time()
    if not refresh:
        with _cache_lock:
            cached = _search_cache.get(cache_key)
        if cached and now - cached[0] < SEARCH_TTL:
            payload = copy.deepcopy(cached[1])
            payload["cached"] = True
            return payload

    resolved_imdb = imdb_id or resolve_imdb(media_type, tmdb_id, title, year)
    provider_status: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []

    jobs: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        if resolved_imdb:
            jobs["torrentio"] = pool.submit(_torrentio, resolved_imdb, media_type, season, episode)
            if media_type == "tv":
                jobs["eztv"] = pool.submit(_eztv, resolved_imdb, season, episode)
        else:
            provider_status.append(
                {
                    "name": "Torrentio",
                    "ok": False,
                    "count": 0,
                    "error": "No IMDb id could be resolved for this title.",
                }
            )
        if media_type == "movie":
            jobs["yts"] = pool.submit(_yts, title, year, resolved_imdb)
        queries = [title]
        if year:
            queries.append(f"{title} {year}")
        if media_type == "tv":
            queries.append(f"{title} S{season:02d}E{episode:02d}")
            queries.append(f"{title} {season}x{episode:02d}")
        else:
            queries.append(f"{title} {year}".strip())
        jobs["apibay"] = pool.submit(_apibay, queries, media_type, season, episode, year)

        labels = {
            "torrentio": "Torrentio",
            "eztv": "EZTV",
            "yts": "YTS",
            "apibay": "The Pirate Bay",
        }
        for name, future in jobs.items():
            try:
                rows = future.result() or []
            except Exception as exc:  # noqa: BLE001 - provider failure is data
                provider_status.append(
                    {
                        "name": labels.get(name, name),
                        "ok": False,
                        "count": 0,
                        "error": str(exc)[:200],
                    }
                )
                continue
            results.extend(rows)
            provider_status.append(
                {
                    "name": labels.get(name, name),
                    "ok": True,
                    "count": len(rows),
                    "error": "",
                }
            )

    merged = _merge_results(results)
    payload = {
        "ok": True,
        "type": media_type,
        "title": title,
        "year": year,
        "season": season,
        "episode": episode,
        "imdbId": resolved_imdb,
        "providers": provider_status,
        "results": merged,
        "count": len(merged),
        "cached": False,
        "bridge": bridge_info(),
        "generatedAt": now,
    }
    with _cache_lock:
        _search_cache[cache_key] = (now, copy.deepcopy(payload))
        if len(_search_cache) > 120:
            oldest = sorted(_search_cache.items(), key=lambda item: item[1][0])[:20]
            for key, _ in oldest:
                _search_cache.pop(key, None)
    return payload


# -- WebTorrent bridge ------------------------------------------------------


def _find_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _node_binary() -> str:
    if NODE_BIN:
        return NODE_BIN
    return shutil.which("node") or shutil.which("node.exe") or ""


def bridge_installed() -> bool:
    return (
        (BRIDGE_DIR / "server.mjs").exists()
        and (BRIDGE_DIR / "node_modules" / "webtorrent").exists()
        and (BRIDGE_DIR / "node_modules" / "parse-torrent").exists()
    )


def _bridge_base() -> str:
    if BRIDGE_URL:
        return BRIDGE_URL
    state_url = str(_bridge_state.get("url") or "")
    if state_url:
        return state_url
    return ""


def _bridge_headers() -> dict[str, str]:
    token = BRIDGE_TOKEN or str(_bridge_state.get("token") or "")
    return {"X-Bridge-Token": token} if token else {}


def _log_tail(lines: int = 12) -> str:
    try:
        if BRIDGE_LOG_PATH.exists():
            text = BRIDGE_LOG_PATH.read_text(encoding="utf-8", errors="replace")
            return "\n".join(text.splitlines()[-lines:])
    except OSError:
        pass
    return ""


def _health(url: str) -> dict[str, Any] | None:
    try:
        response = _session().get(f"{url}/health", headers=_bridge_headers(), timeout=1.2)
        if response.status_code == 200:
            data = response.json()
            if isinstance(data, dict) and data.get("ok"):
                return data
    except (requests.RequestException, ValueError):
        return None
    return None


def ensure_bridge() -> dict[str, Any]:
    global _bridge_state
    with _bridge_lock:
        url = _bridge_base()
        if url:
            info = _health(url)
            if info:
                return info
            if BRIDGE_URL:
                raise TorrentError(f"WebTorrent bridge at {url} is not responding.")
            stale = _bridge_state.get("proc")
            if stale is not None and stale.poll() is None:
                try:
                    stale.terminate()
                    stale.wait(timeout=3)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        stale.kill()
                    except OSError:
                        pass
            stale_log = _bridge_state.get("log")
            if stale_log is not None:
                try:
                    stale_log.close()
                except OSError:
                    pass
            _bridge_state["url"] = ""
            _bridge_state["proc"] = None
            _bridge_state["log"] = None

        if not bridge_installed():
            raise TorrentError(
                "The WebTorrent bridge is not installed. Run `npm install` (or `npm.cmd install` on Windows) "
                "inside the torrent_bridge folder, then retry."
            )
        node = _node_binary()
        if not node:
            raise TorrentError("Node.js 18+ is required for WebTorrent playback but `node` was not found.")

        port = BRIDGE_PORT or _find_port()
        token = BRIDGE_TOKEN or secrets.token_urlsafe(24)
        base = f"http://127.0.0.1:{port}"
        env = os.environ.copy()
        env.update(
            {
                "TORRENT_BRIDGE_PORT": str(port),
                "TORRENT_BRIDGE_HOST": "127.0.0.1",
                "TORRENT_BRIDGE_TOKEN": token,
                "TORRENT_BRIDGE_DATA": str(BRIDGE_DATA_DIR),
                "TORRENT_METADATA_TIMEOUT": os.environ.get("TORRENT_METADATA_TIMEOUT", "30000"),
            }
        )
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        BRIDGE_DATA_DIR.mkdir(parents=True, exist_ok=True)
        log = open(BRIDGE_LOG_PATH, "ab", buffering=0)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            proc = subprocess.Popen(
                [node, str(BRIDGE_DIR / "server.mjs")],
                cwd=str(BRIDGE_DIR),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )
        except OSError as exc:
            log.close()
            raise TorrentError(f"Could not start the WebTorrent bridge: {exc}") from exc

        _bridge_state = {
            "proc": proc,
            "url": base,
            "token": token,
            "port": port,
            "log": log,
            "started_at": time.time(),
            "last_error": "",
        }

        deadline = time.time() + 18
        while time.time() < deadline:
            if proc.poll() is not None:
                tail = _log_tail()
                _bridge_state["last_error"] = tail or f"Bridge exited with code {proc.returncode}."
                log.close()
                _bridge_state["proc"] = None
                raise TorrentError(f"The WebTorrent bridge stopped during startup. {tail}".strip())
            info = _health(base)
            if info:
                return info
            time.sleep(0.3)
        _bridge_state["last_error"] = _log_tail() or "Bridge did not become ready in time."
        raise TorrentError(f"WebTorrent bridge did not start. {_bridge_state['last_error']}".strip())


def bridge_info() -> dict[str, Any]:
    url = _bridge_base()
    running = False
    details: dict[str, Any] = {}
    if url:
        info = _health(url)
        if info:
            running = True
            details = info
    proc = _bridge_state.get("proc")
    return {
        "installed": bridge_installed(),
        "available": bool(running),
        "running": running,
        "url": url,
        "node": _node_binary(),
        "version": details.get("webtorrent") or "",
        "torrents": details.get("torrents"),
        "dataDir": details.get("dataDir") or str(BRIDGE_DATA_DIR),
        "error": str(_bridge_state.get("last_error") or ""),
        "installHint": "" if bridge_installed() else "cd torrent_bridge && npm install",
        "processAlive": bool(proc and proc.poll() is None),
    }


def _bridge_request(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: float = 60.0,
    retry: bool = True,
) -> dict[str, Any]:
    ensure_bridge()
    url = f"{_bridge_base()}{path}"
    try:
        response = _session().request(
            method,
            url,
            json=payload,
            headers=_bridge_headers(),
            timeout=(10.0, timeout),
        )
    except requests.RequestException as exc:
        _bridge_state["last_error"] = str(exc)[:300]
        if retry:
            _stop_bridge()
            return _bridge_request(method, path, payload, timeout=timeout, retry=False)
        raise TorrentError(f"WebTorrent bridge request failed: {exc}") from exc
    try:
        data = response.json() if response.content else {}
    except ValueError:
        data = {}
    if response.status_code >= 400:
        message = (data or {}).get("error") or f"Bridge returned HTTP {response.status_code}."
        raise TorrentError(str(message))
    if not isinstance(data, dict):
        raise TorrentError("WebTorrent bridge returned an invalid response.")
    return data


def add_torrent(
    magnet: str,
    *,
    file_index: int | None = None,
    file_hint: str = "",
    timeout: float = 60.0,
) -> dict[str, Any]:
    magnet = str(magnet or "").strip()
    encoded_xt = bool(re.search(r"xt=urn(?:%3a|:)btih(?:%3a|:)", magnet, re.IGNORECASE))
    if not magnet.startswith("magnet:?") or not encoded_xt:
        raise TorrentError("A valid magnet URI is required.")
    if len(magnet) > 12000:
        raise TorrentError("The magnet URI is too long.")
    payload: dict[str, Any] = {"magnet": magnet}
    if isinstance(file_index, int) and file_index >= 0:
        payload["fileIndex"] = file_index
    if file_hint:
        payload["fileHint"] = str(file_hint)[:300]
    return _bridge_request("POST", "/add", payload, timeout=timeout)


def torrent_status(info_hash: str) -> dict[str, Any]:
    info_hash = _info_hash(info_hash)
    if not info_hash:
        raise TorrentError("A valid info hash is required.")
    return _bridge_request("GET", f"/status/{info_hash}", timeout=20)


def select_file(info_hash: str, index: int) -> dict[str, Any]:
    info_hash = _info_hash(info_hash)
    if not info_hash or not isinstance(index, int) or index < 0:
        raise TorrentError("A valid torrent and file index are required.")
    return _bridge_request("POST", "/select", {"id": info_hash, "index": index}, timeout=60)


def remove_torrent(info_hash: str, keep_data: bool = False) -> dict[str, Any]:
    info_hash = _info_hash(info_hash)
    if not info_hash:
        return {"ok": True, "removed": False}
    url = _bridge_base()
    if not url or not _health(url):
        return {"ok": True, "removed": False}
    return _bridge_request(
        "POST",
        "/remove",
        {"id": info_hash, "keepData": bool(keep_data)},
        timeout=15,
    )


def open_stream(
    magnet: str,
    info_hash: str,
    index: int,
    range_header: str | None = None,
) -> requests.Response:
    info_hash = _info_hash(info_hash)
    if not info_hash or not isinstance(index, int) or index < 0:
        raise TorrentError("A valid torrent and file index are required.")
    ensure_bridge()
    url = f"{_bridge_base()}/stream/{info_hash}/{index}"
    headers = dict(_bridge_headers())
    if range_header:
        headers["Range"] = range_header
    try:
        response = _session().get(url, headers=headers, stream=True, timeout=(20.0, None))
    except requests.RequestException as exc:
        raise TorrentError(f"Could not reach the WebTorrent bridge: {exc}") from exc
    if response.status_code < 400:
        return response
    response.close()
    if response.status_code == 404 and magnet:
        add_torrent(magnet, file_index=index, timeout=45)
        try:
            response = _session().get(url, headers=headers, stream=True, timeout=(20.0, None))
        except requests.RequestException as exc:
            raise TorrentError(f"Could not reach the WebTorrent bridge: {exc}") from exc
        if response.status_code < 400:
            return response
        response.close()
    message = "The torrent stream is unavailable."
    try:
        body = response.json()
        message = body.get("error") or message
    except ValueError:
        pass
    raise TorrentError(message)


def _stop_bridge() -> None:
    global _bridge_state
    with _bridge_lock:
        proc = _bridge_state.get("proc")
        log = _bridge_state.get("log")
        _bridge_state["proc"] = None
        _bridge_state["url"] = "" if not BRIDGE_URL else _bridge_state.get("url")
        if proc and proc.poll() is None:
            try:
                if os.name == "nt":
                    proc.terminate()
                else:
                    proc.terminate()
                proc.wait(timeout=4)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    proc.kill()
                except OSError:
                    pass
        if log is not None:
            try:
                log.close()
            except OSError:
                pass


def stop_bridge() -> None:
    _stop_bridge()


atexit.register(_stop_bridge)
