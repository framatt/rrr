#!/usr/bin/env python3
"""Self-hosted NetMirror catalog browser and player.

Run:
  python app.py
  python app.py --host 0.0.0.0 --port 8080
"""

import argparse
import gzip
import hashlib
import hmac
import json
import mimetypes
import os
import queue
import random
import re
import secrets
import socket
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import requests
from flask import Flask, Response, abort, jsonify, request, send_file

import casting
import discovery
import download_catalog as dc
import downloads
import internet_lists
import library as library_module
import metadata
import recommender
import torrents
from casting import CastError

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
CACHE_DIR = ROOT / ".app_cache"
IMAGE_DIR = CACHE_DIR / "images"
IMAGE_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/assets")
app.config["JSON_SORT_KEYS"] = False

CATALOG_PATH = Path(os.environ.get("CATALOG_PATH", str(ROOT / "catalog.json")))
WORKER_PROXY = os.environ.get("WORKER_PROXY", "https://streamhub-proxy.1545zoya.workers.dev")
REFERERS = {
    "hakunaymatata.com": "https://moviebox.ph/",
    "mxcontent.net": "https://mixdrop.top/",
    "messycloud.ink": "https://netmirror.hair/",
    "juicybits.io": "https://www.messycloud.ink/",
    "h5-api.aoneroom.com": "https://videodownloader.site/",
}
REFERER_PROFILE_URL = os.environ.get("REFERER_PROFILE_URL", "https://net27.cc/api/hh-profile.json")
_referer_lock = threading.Lock()
_referer_state = {"at": 0.0, "remote": {}}
APP_USER = os.environ.get("APP_USER", "")
APP_PASS = os.environ.get("APP_PASS", "")
BIND_HOST = ""
CAST_MEDIA_BASE = os.environ.get("CAST_MEDIA_BASE", "").strip().rstrip("/")

_tls = threading.local()
_base_lock = threading.Lock()
_base = None
_catalog_lock = threading.Lock()
_catalog_state = {"mtime": None, "data": None, "loaded_at": 0}
_detail_cache = {}
_detail_lock = threading.Lock()
META = metadata.store()
LIB = library_module.library()
DOWNLOADS = downloads.Downloads(torrents)
DOWNLOADS.start()

_enrich_queue: "queue.Queue[dict]" = queue.Queue()
_enrich_seen: set[str] = set()
_enrich_lock = threading.Lock()
_enrich_worker_started = False
_enrich_stats = {"queued": 0, "done": 0, "failed": 0, "running": False, "lastError": ""}
_enrich_session = threading.local()
_facet_lock = threading.Lock()
_facet_cache = {"at": 0.0, "signature": None, "value": None}

RAIL_GENRE_HINTS = {
    "horror": ["Horror"],
    "comedy": ["Comedy"],
    "action&thriller": ["Action", "Thriller"],
    "action": ["Action"],
    "thriller": ["Thriller"],
    "romance": ["Romance"],
    "k-drama": ["Drama", "Romance"],
    "c-drama": ["Drama", "Romance"],
    "anime": ["Animation"],
    "adult animation": ["Animation"],
    "superhero": ["Action", "Science Fiction"],
    "epic fantasy": ["Fantasy", "Adventure"],
    "teen fantasy": ["Fantasy", "Adventure"],
    "teen romance": ["Romance", "Drama"],
    "gangster": ["Crime", "Drama"],
    "bl story": ["Romance", "Drama"],
    "nollywood": ["Drama"],
    "documentary": ["Documentary"],
    "family": ["Family"],
    "kids": ["Kids", "Family"],
}


def rail_hints(title):
    text = str(title or "").lower()
    genres = []
    adult = False
    for needle, values in RAIL_GENRE_HINTS.items():
        if needle in text:
            for genre in values:
                if genre not in genres:
                    genres.append(genre)
    if "adult animation" in text:
        adult = True
    return genres, adult


def session():
    value = getattr(_tls, "session", None)
    if value is None:
        value = requests.Session()
        value.headers.update(dc.BROWSER_HEADERS)
        _tls.session = value
    return value


def get_base():
    global _base
    if _base is not None:
        return _base
    with _base_lock:
        if _base is None:
            try:
                _base = dc.pick_base(os.environ.get("NETMIRROR_BASE"))
            except Exception:
                _base = "https://net79.cc"
    return _base


_token_lock = threading.Lock()
_token_store = {}
_media_token_lock = threading.Lock()
_media_token_cache = {}
_torrent_key_lock = threading.Lock()
_torrent_keys: dict[str, dict] = {}
TORRENT_KEY_LIMIT = 200
TOKEN_LIMIT = 120000


def purge_tokens(now=None):
    now = now or time.time()
    expired = [key for key, value in _token_store.items() if int(value.get("e") or 0) < now]
    for key in expired:
        _token_store.pop(key, None)


def issue_token(payload, ttl):
    token = secrets.token_urlsafe(24)
    value = dict(payload)
    value["e"] = int(time.time()) + int(ttl)
    with _token_lock:
        if len(_token_store) >= TOKEN_LIMIT:
            purge_tokens()
            while len(_token_store) >= TOKEN_LIMIT:
                _token_store.pop(next(iter(_token_store)), None)
        _token_store[token] = value
    return token


def verify_token(token):
    if not token:
        return None
    with _token_lock:
        payload = _token_store.get(token)
        if payload and int(payload.get("e") or 0) < int(time.time()):
            _token_store.pop(token, None)
            return None
        return payload


def remember_torrent(magnet, info_hash, title="", content_type="", file_name="", quality=0, size=0):
    key = secrets.token_urlsafe(18)
    with _torrent_key_lock:
        if len(_torrent_keys) >= TORRENT_KEY_LIMIT:
            for stale in list(_torrent_keys)[:40]:
                _torrent_keys.pop(stale, None)
        _torrent_keys[key] = {
            "magnet": magnet,
            "infoHash": info_hash,
            "title": title,
            "contentType": content_type,
            "fileName": file_name,
            "quality": int(quality or 0),
            "size": int(size or 0),
            "at": time.time(),
        }
    return key


def recall_torrent(key):
    with _torrent_key_lock:
        return dict(_torrent_keys.get(str(key or "")) or {})


def media_token(url, kind="img", ttl=7 * 86400):
    if not url:
        return None
    key = (kind, str(url))
    now = time.time()
    with _media_token_lock:
        cached = _media_token_cache.get(key)
        if cached and cached[0] > now + 300:
            with _token_lock:
                if cached[1] in _token_store:
                    return cached[1]
    token = issue_token({"t": kind, "u": str(url)}, ttl)
    with _media_token_lock:
        _media_token_cache[key] = (now + ttl, token)
    return token


def media_url(url, kind="img"):
    if not url or not str(url).startswith(("http://", "https://")):
        return None
    token = media_token(url, kind)
    return f"/api/image?token={quote(token)}" if token else None


def localize(value):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in ("poster", "backdrop", "still", "photo") and isinstance(item, str) and item.startswith("http"):
                result[key] = media_url(item)
            else:
                result[key] = localize(item)
        return result
    if isinstance(value, list):
        return [localize(item) for item in value]
    return value


def catalog_items_from(data):
    items = []
    index = {}
    for item in data.get("hero") or []:
        dc.add_items(items, index, [item], "hero")
    for window in (data.get("trending") or {}).values():
        dc.add_items(items, index, window, "trending")
    for rail in data.get("rails") or []:
        dc.add_items(items, index, rail.get("items") or [], "rail")
    dc.add_items(items, index, data.get("items") or [], "items")
    return items


def load_live_catalog():
    hero, trending, rails, _ = dc.fetch_home(get_base(), "IN")
    return catalog_items_from({"hero": hero, "trending": trending, "rails": rails})


def prepare_catalog(data, raw):
    items = []
    for item in catalog_items_from(data):
        if not item.get("tmdbId"):
            continue
        item["_type"] = str(item.get("type") or "").lower()
        item["_search"] = f"{item.get('title') or ''} {item.get('year') or ''}".lower()
        items.append(item)

    by_title = {}
    by_tmdb = {}
    for item in items:
        by_tmdb[f"{item['_type']}:{item['tmdbId']}"] = item
        normalized = discovery.normalize_title(item.get("title"))
        if not normalized:
            continue
        year = str(item.get("year") or "")[:4]
        by_title[(normalized, year)] = item
        by_title.setdefault(normalized, item)

    hints = {}
    for rail in data.get("rails") or []:
        genres, adult = rail_hints(rail.get("title") or rail.get("name"))
        if not genres and not adult:
            continue
        for entry in rail.get("items") or []:
            target = None
            if entry.get("tmdbId"):
                target = by_tmdb.get(f"{str(entry.get('type') or '').lower()}:{entry.get('tmdbId')}")
            if target is None:
                normalized = discovery.normalize_title(entry.get("title"))
                if not normalized:
                    continue
                target = by_title.get((normalized, str(entry.get("year") or "")[:4])) or by_title.get(normalized)
            if target is None:
                continue
            key = f"{target['_type']}:{target['tmdbId']}"
            current = hints.setdefault(key, {"genres": [], "adult": False})
            for genre in genres:
                if genre not in current["genres"]:
                    current["genres"].append(genre)
            current["adult"] = current["adult"] or adult

    for item in items:
        hint = hints.get(f"{item['_type']}:{item['tmdbId']}")
        if hint:
            item["_fallbackGenres"] = hint["genres"]
            item["_adultHint"] = hint["adult"]
    by_type = {}
    items_by_key = {}
    for item in items:
        by_type.setdefault(item["_type"], []).append(item)
        items_by_key[f"{item['_type']}:{item['tmdbId']}"] = item
    return {
        "items": items,
        "by_type": by_type,
        "items_by_key": items_by_key,
        "hero": data.get("hero") or [],
        "trending": data.get("trending") or {},
        "rails": data.get("rails") or [],
        "sources": data.get("sources") or [],
        "raw": raw,
    }


def get_catalog():
    now = time.time()
    with _catalog_lock:
        if CATALOG_PATH.exists():
            mtime = CATALOG_PATH.stat().st_mtime
            if _catalog_state["mtime"] != mtime:
                data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
                _catalog_state.update(
                    mtime=mtime,
                    data=prepare_catalog(data, data),
                    loaded_at=now,
                )
        elif _catalog_state["data"] is None or now - _catalog_state["loaded_at"] > 300:
            hero, trending, rails, _ = dc.fetch_home(get_base(), "IN")
            _catalog_state.update(
                mtime=None,
                data=prepare_catalog({"hero": hero, "trending": trending, "rails": rails}, {}),
                loaded_at=now,
            )
        return _catalog_state["data"]


def library_state_map(snapshot):
    state = {key: {"inWatchlist": False, "liked": False, "disliked": False, "watched": False, "progress": None} for key in (snapshot.get("watchlist") or {})}
    for key in (snapshot.get("watchlist") or {}):
        state.setdefault(key, {})["inWatchlist"] = True
    for key, reaction in (snapshot.get("reactions") or {}).items():
        value = int(reaction.get("value") or 0)
        entry = state.setdefault(key, {})
        entry["liked"] = value > 0
        entry["disliked"] = value < 0
    for key in (snapshot.get("watched") or {}):
        state.setdefault(key, {})["watched"] = True
    for entry in (snapshot.get("progress") or {}).values():
        key = entry.get("key")
        if not key:
            continue
        current = state.setdefault(key, {}).get("progress")
        if not current or entry.get("updatedAt", 0) > current.get("updatedAt", 0):
            state[key]["progress"] = {
                "progressKey": entry.get("progressKey"),
                "position": entry.get("position"),
                "duration": entry.get("duration"),
                "season": entry.get("season"),
                "episode": entry.get("episode"),
                "updatedAt": entry.get("updatedAt"),
            }
    return state


def _enrich_worker_loop():
    _enrich_stats["running"] = True
    while True:
        item = _enrich_queue.get()
        key = f"{str(item.get('type') or '').lower()}:{item.get('tmdbId')}"
        try:
            existing = META.get(key)
            if existing and existing.get("status") == "ok" and not item.get("_force"):
                _enrich_stats["done"] += 1
                continue
            row = metadata.fetch_and_store(
                session(),
                get_base(),
                item.get("type"),
                item.get("tmdbId"),
                metadata_store=META,
                timeout=25,
            )
            if row:
                _enrich_stats["done"] += 1
            else:
                _enrich_stats["failed"] += 1
                _enrich_stats["lastError"] = f"{key}: upstream detail fetch failed"
        except Exception as exc:  # noqa: BLE001
            _enrich_stats["failed"] += 1
            _enrich_stats["lastError"] = f"{key}: {str(exc)[:180]}"
        finally:
            with _enrich_lock:
                _enrich_seen.discard(key)
            _enrich_queue.task_done()
            time.sleep(0.03)


def enqueue_enrichment(item, force=False):
    if not item or not item.get("tmdbId") or str(item.get("type") or "").lower() not in ("movie", "tv"):
        return False
    global _enrich_worker_started
    key = f"{str(item.get('type') or '').lower()}:{item.get('tmdbId')}"
    with _enrich_lock:
        if key in _enrich_seen:
            return False
        existing = META.get(key, refresh=False)
        if existing and existing.get("status") == "ok" and not force:
            return False
        if not _enrich_worker_started:
            threading.Thread(target=_enrich_worker_loop, name="metadata-enricher", daemon=True).start()
            _enrich_worker_started = True
        _enrich_seen.add(key)
        _enrich_queue.put({**item, "_force": force})
        _enrich_stats["queued"] += 1
    return True


def start_bulk_enrichment(limit=0, include_errors=False):
    catalog = get_catalog()
    missing = set(META.missing_keys(catalog["items"], include_errors=include_errors))
    queued = 0
    for item in catalog["items"]:
        key = f"{str(item.get('type') or '').lower()}:{item.get('tmdbId')}"
        if key not in missing:
            continue
        if enqueue_enrichment(item, force=include_errors):
            queued += 1
        if limit and queued >= limit:
            break
    return queued


def metadata_status():
    catalog = get_catalog()
    coverage = META.coverage(len(catalog["items"]))
    with _enrich_lock:
        queue_size = _enrich_queue.qsize()
        seen = len(_enrich_seen)
    return {
        "ok": True,
        "coverage": coverage,
        "queue": queue_size,
        "pending": seen,
        "stats": dict(_enrich_stats),
        "externalBulkAvailable": True,
        "torrents": torrents.bridge_info(),
        "downloads": DOWNLOADS.stats(),
    }


def public_item(item, meta_map=None, snapshot=None, state_map=None):
    tmdb_id = item.get("tmdbId")
    if not tmdb_id:
        return None
    meta_map = meta_map or {}
    meta = discovery.item_meta(item, meta_map)
    media_type = str(item.get("type") or "").lower()
    key = f"{media_type}:{tmdb_id}"
    flags = {}
    if state_map is not None:
        flags = state_map.get(key) or {}
    elif snapshot is not None:
        flags = (library_state_map(snapshot).get(key) or {})
    trailer_key = meta.get("trailer") or item.get("trailerKey") or ""
    result = {
        "id": key,
        "tmdbId": tmdb_id,
        "type": media_type,
        "title": item.get("title") or meta.get("title") or "",
        "year": item.get("year") or meta.get("year") or "",
        "rating": item.get("rating") if item.get("rating") is not None else meta.get("rating"),
        "overview": item.get("overview") or "",
        "poster": media_url(item.get("poster")),
        "backdrop": media_url(item.get("backdrop")),
        "posterRaw": item.get("poster") or "",
        "backdropRaw": item.get("backdrop") or "",
        "streamable": item.get("streamable"),
        "genres": meta.get("genres") or [],
        "certification": meta.get("certification") or "",
        "age": meta.get("minAge"),
        "ageBand": meta.get("ageBand") or "unknown",
        "kidsSafe": bool(meta.get("kidsSafe")),
        "familySafe": bool(meta.get("familySafe")),
        "adult": bool(meta.get("adult") or item.get("_adultHint")),
        "runtime": meta.get("runtime"),
        "language": meta.get("language") or "",
        "trailer": f"https://www.youtube.com/watch?v={quote(trailer_key)}" if trailer_key else "",
        "upstreamOnly": bool(item.get("upstreamOnly")),
        "inWatchlist": bool(flags.get("inWatchlist")),
        "liked": bool(flags.get("liked")),
        "disliked": bool(flags.get("disliked")),
        "watched": bool(flags.get("watched")),
        "progress": flags.get("progress"),
    }
    if item.get("reason"):
        result["reason"] = item["reason"]
    if item.get("score") is not None:
        result["score"] = item["score"]
    return result


def public_items(items, meta_map=None, snapshot=None):
    state_map = library_state_map(snapshot) if snapshot is not None else None
    result = []
    for item in items:
        public = public_item(item, meta_map, state_map=state_map)
        if public:
            result.append(public)
    return result


def upstream_json(path, params=None, timeout=30, retries=3):
    try:
        return dc.fetch_json(f"{get_base()}{path}", params=params, timeout=timeout, retries=retries)
    except dc.FetchError as exc:
        app.logger.warning("upstream %s failed: %s", path, exc)
        return None


def copy_headers(upstream):
    headers = {}
    for key in (
        "Content-Type",
        "Content-Length",
        "Content-Range",
        "Accept-Ranges",
        "Cache-Control",
        "Last-Modified",
        "ETag",
    ):
        value = upstream.headers.get(key)
        if value:
            headers[key] = value
    return headers


def stream_body(upstream):
    try:
        for chunk in upstream.iter_content(chunk_size=64 * 1024):
            if chunk:
                yield chunk
    finally:
        upstream.close()


def refresh_referers():
    now = time.time()
    with _referer_lock:
        if now - _referer_state["at"] < 10800:
            return
        _referer_state["at"] = now
    try:
        response = session().get(REFERER_PROFILE_URL, timeout=10)
        if response.status_code != 200:
            return
        rules = response.json().get("rules") or []
        remote = {}
        for rule in rules:
            host = str(rule.get("host") or "").strip().lower()
            referer = str(rule.get("referer") or "").strip()
            if host and referer.startswith(("http://", "https://")):
                remote[host] = referer
        if remote:
            with _referer_lock:
                _referer_state["remote"] = remote
    except (requests.RequestException, ValueError):
        return


def referer_for(url):
    refresh_referers()
    host = (urlparse(url).hostname or "").lower()
    with _referer_lock:
        remote = dict(_referer_state["remote"])
    merged = {**REFERERS, **remote}
    for domain, referer in merged.items():
        if host == domain or host.endswith("." + domain):
            return referer
    return f"{get_base()}/"


def proxy_video_request(url, range_header):
    headers = {
        "User-Agent": dc.BROWSER_HEADERS["User-Agent"],
        "Accept": "*/*",
        "Referer": referer_for(url),
    }
    if range_header:
        headers["Range"] = range_header
    return session().get(url, headers=headers, stream=True, timeout=(10, 90), allow_redirects=True)


def video_candidates(payload):
    url = payload.get("u") or ""
    exp = payload.get("x")
    sig = payload.get("s") or ""
    candidates = [url]
    if exp and sig:
        query = f"?url={quote(url, safe='')}&exp={exp}&sig={quote(sig, safe='')}"
        candidates.append(WORKER_PROXY.rstrip("/") + "/" + query)
        candidates.append(f"{get_base()}/api/proxy/video{query}")
    seen = []
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.append(candidate)
    return seen


def lan_ip():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def media_base():
    if CAST_MEDIA_BASE:
        return CAST_MEDIA_BASE
    host = request.host
    hostname = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    if hostname not in ("127.0.0.1", "localhost", "::1"):
        return f"{request.scheme}://{host}"
    port = host.rsplit(":", 1)[1] if host.count(":") == 1 else ""
    return f"http://{lan_ip()}{':' + port if port else ''}"


def cast_thumb(value):
    text = str(value or "")
    if text.startswith("/api/image?"):
        return media_base() + text
    return None


def stream_content_type(url):
    return casting.content_type_for(url)


@app.before_request
def basic_auth():
    if not (APP_USER and APP_PASS):
        return None
    if request.path in ("/api/video", "/api/subtitle", "/api/image", "/api/torrents/stream"):
        if verify_token(request.args.get("token") or ""):
            return None
    auth = request.authorization
    if not auth or auth.username != APP_USER or not hmac.compare_digest(auth.password or "", APP_PASS):
        return Response(
            "Authentication required",
            401,
            {"WWW-Authenticate": 'Basic realm="nm-private"'},
        )
    return None


@app.before_request
def same_origin_mutation():
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return None
    origin = request.headers.get("Origin")
    if origin and urlparse(origin).netloc != request.host:
        return jsonify({"ok": False, "error": "Cross-origin request blocked."}), 403
    return None


@app.after_request
def security_headers(response):
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; media-src 'self' blob:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; "
        "font-src 'self'; frame-ancestors 'self'"
    )
    if (
        response.mimetype == "application/json"
        and not response.is_streamed
        and request.accept_encodings.quality("gzip") > 0
    ):
        data = response.get_data()
        if len(data) > 1024:
            compressed = gzip.compress(data, 6)
            response.set_data(compressed)
            response.headers["Content-Encoding"] = "gzip"
            response.headers["Vary"] = "Accept-Encoding"
            response.headers["Content-Length"] = str(len(compressed))
    return response


@app.get("/")
def index():
    return send_file(STATIC_DIR / "index.html")


@app.get("/favicon.ico")
def favicon():
    return Response(status=204)


@app.get("/api/health")
def health():
    catalog = get_catalog()
    snapshot = LIB.snapshot()
    return jsonify(
        {
            "ok": True,
            "catalog_items": len(catalog["items"]),
            "catalog_source": "file" if CATALOG_PATH.exists() else "live",
            "auth": bool(APP_USER),
            "metadata": META.coverage(len(catalog["items"])),
            "torrents": torrents.bridge_info(),
            "library": {
                "watchlist": len(snapshot.get("watchlist") or {}),
                "watched": len(snapshot.get("watched") or {}),
                "progress": len(snapshot.get("progress") or {}),
            },
        }
    )


@app.get("/api/catalog")
def api_catalog():
    catalog = get_catalog()
    filters = discovery.parse_filters(request.args)
    try:
        page = max(1, int(request.args.get("page", "1")))
        limit = min(120, max(1, int(request.args.get("limit", "60"))))
    except ValueError:
        page, limit = 1, 60

    meta_map = META.all()
    snapshot = LIB.snapshot()
    media_type = filters.get("type") or ""
    source = catalog["by_type"].get(media_type, []) if media_type else catalog["items"]
    candidates = discovery.filter_items(source, meta_map, filters)
    ordered = discovery.sort_items(candidates, meta_map, filters.get("sort") or "popular", filters.get("q") or "")
    total = len(ordered)
    start = (page - 1) * limit
    page_items = ordered[start : start + limit]
    for item in page_items:
        enqueue_enrichment(item)
    items = public_items(page_items, meta_map, snapshot)

    meta_signature = len(meta_map)
    if meta_map:
        meta_signature += int(max((row.get("updatedAt") or 0) for row in meta_map.values()))
    etag = hashlib.sha256(
        json.dumps(
            {
                "mtime": str(_catalog_state.get("mtime")),
                "loaded": int(_catalog_state.get("loaded_at") or 0),
                "meta": meta_signature,
                "filters": filters,
                "page": page,
                "limit": limit,
            },
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()[:32]
    if request.if_none_match.contains(etag):
        return Response(status=304)

    response = jsonify(
        {
            "ok": True,
            "total": total,
            "page": page,
            "limit": limit,
            "pages": (total + limit - 1) // limit if total else 0,
            "items": items,
            "filters": filters,
            "coverage": META.coverage(len(catalog["items"])),
        }
    )
    response.set_etag(etag)
    response.headers["Cache-Control"] = "private, max-age=15"
    return response


@app.get("/api/facets")
def api_facets():
    catalog = get_catalog()
    meta_map = META.all()
    signature = (len(meta_map), len(catalog["items"]))
    now = time.time()
    with _facet_lock:
        cached = _facet_cache
        if cached["value"] is not None and cached["signature"] == signature and now - cached["at"] < 20:
            return jsonify(cached["value"])
    value = discovery.facets(catalog["items"], meta_map)
    value.update(
        {
            "ok": True,
            "total": len(catalog["items"]),
            "coverage": META.coverage(len(catalog["items"])),
            "types": {key: len(items) for key, items in catalog["by_type"].items()},
        }
    )
    with _facet_lock:
        _facet_cache.update(at=now, signature=signature, value=value)
    return jsonify(value)


@app.get("/api/details")
def api_details():
    media_type = (request.args.get("type") or "").lower()
    tmdb_id = request.args.get("id") or ""
    if media_type not in ("movie", "tv") or not tmdb_id.isdigit():
        abort(400)
    key = f"{media_type}:{tmdb_id}"
    now = time.time()
    with _detail_lock:
        cached = _detail_cache.get(key)
        fresh = cached and now - cached[0] < 600
    catalog = get_catalog()
    catalog_item = catalog["items_by_key"].get(key)
    snapshot = LIB.snapshot()

    if fresh:
        localized = dict(cached[1])
    else:
        data = upstream_json(f"/api/catalog/title/{media_type}/{tmdb_id}", timeout=40)
        if not data or not data.get("ok"):
            meta_map = META.all()
            meta = meta_map.get(key)
            if not catalog_item and not meta:
                return jsonify({"ok": False, "error": "Details are temporarily unavailable."}), 502
            localized = {
                "ok": True,
                "degraded": True,
                "type": media_type,
                "title": (catalog_item or {}).get("title") or (meta or {}).get("title") or "",
                "year": (catalog_item or {}).get("year") or (meta or {}).get("year") or "",
                "overview": (catalog_item or {}).get("overview") or "",
                "poster": media_url((catalog_item or {}).get("poster")),
                "backdrop": media_url((catalog_item or {}).get("backdrop")),
                "rating": (catalog_item or {}).get("rating"),
                "genres": [{"name": name} for name in (meta or {}).get("genres") or []],
                "certification": {"rating": (meta or {}).get("certification") or "", "country": (meta or {}).get("certCountry") or ""},
                "runtime": (meta or {}).get("runtime"),
                "trailerKey": (meta or {}).get("trailer") or "",
                "recommendations": [],
            }
        else:
            localized = localize(data)
            row = metadata.row_from_details(data, media_type, tmdb_id)
            META.upsert(row)
            catalog_info = localized.get("catalog")
            if isinstance(catalog_info, dict):
                catalog_info.pop("variants", None)
                catalog_info.pop("nativeSeasons", None)
            with _detail_lock:
                _detail_cache[key] = (now, localized)
            enqueue_enrichment({"type": media_type, "tmdbId": int(tmdb_id)})

    meta_map = META.all()
    meta = meta_map.get(key) or {}
    target = catalog_item or {
        "type": media_type,
        "tmdbId": int(tmdb_id),
        "title": localized.get("title") or "",
        "year": localized.get("year") or "",
        "poster": localized.get("poster") or "",
        "backdrop": localized.get("backdrop") or "",
        "rating": localized.get("rating"),
        "overview": localized.get("overview") or "",
    }
    similar = recommender.similar_items(target, catalog["items"], meta_map, limit=14)
    localized["similar"] = public_items(similar, meta_map, snapshot)
    localized["age"] = meta.get("minAge")
    localized["ageBand"] = meta.get("ageBand") or "unknown"
    localized["kidsSafe"] = bool(meta.get("kidsSafe"))
    localized["familySafe"] = bool(meta.get("familySafe"))
    localized["adult"] = bool(meta.get("adult"))
    localized["runtime"] = localized.get("runtime") or meta.get("runtime")
    localized["trailerUrl"] = (
        f"https://www.youtube.com/watch?v={quote(localized.get('trailerKey'))}"
        if localized.get("trailerKey")
        else ""
    )
    flags = library_state_map(snapshot).get(key) or {}
    localized["library"] = {
        "inWatchlist": bool(flags.get("inWatchlist")),
        "liked": bool(flags.get("liked")),
        "disliked": bool(flags.get("disliked")),
        "watched": bool(flags.get("watched")),
        "progress": flags.get("progress"),
    }
    localized["progressEntries"] = sorted(
        [entry for entry in (snapshot.get("progress") or {}).values() if entry.get("key") == key],
        key=lambda entry: entry.get("updatedAt") or 0,
        reverse=True,
    )
    return jsonify(localized)


@app.get("/api/recommendations")
def api_recommendations():
    catalog = get_catalog()
    filters = discovery.parse_filters(request.args)
    try:
        limit = min(30, max(6, int(request.args.get("limit", "18"))))
    except ValueError:
        limit = 18
    meta_map = META.all()
    snapshot = LIB.snapshot()
    result = recommender.recommend_rows(catalog["items"], snapshot, meta_map, filters, limit=limit)
    rows = []
    for row in result.get("rows") or []:
        rows.append(
            {
                "id": row.get("id"),
                "title": row.get("title"),
                "subtitle": row.get("subtitle") or "",
                "items": public_items(row.get("items") or [], meta_map, snapshot),
            }
        )

    continue_items = []
    for entry in sorted((snapshot.get("progress") or {}).values(), key=lambda value: value.get("updatedAt") or 0, reverse=True):
        item = catalog["items_by_key"].get(entry.get("key"))
        merged = dict(item or entry)
        merged.update({key: value for key, value in entry.items() if value not in (None, "")})
        meta = discovery.item_meta(merged, meta_map)
        if not discovery.matches(merged, meta, filters):
            continue
        public = public_item(merged, meta_map, snapshot=snapshot)
        if not public:
            continue
        public["resume"] = {
            "position": entry.get("position"),
            "duration": entry.get("duration"),
            "season": entry.get("season"),
            "episode": entry.get("episode"),
            "updatedAt": entry.get("updatedAt"),
            "progressKey": entry.get("progressKey"),
        }
        public["reason"] = (
            f"Continue S{entry.get('season')} E{entry.get('episode')}"
            if merged.get("type") == "tv"
            else "Continue watching"
        )
        continue_items.append(public)
        if len(continue_items) >= 18:
            break
    if continue_items:
        rows.insert(
            0,
            {
                "id": "continue",
                "title": "Continue watching",
                "subtitle": "Pick up exactly where you stopped.",
                "items": continue_items,
            },
        )

    for row in rows:
        for item in row.get("items") or []:
            enqueue_enrichment(item)
    result["rows"] = rows
    result["filters"] = filters
    result["coverage"] = META.coverage(len(catalog["items"]))
    return jsonify(result)


@app.get("/api/random")
def api_random():
    catalog = get_catalog()
    filters = discovery.parse_filters(request.args)
    meta_map = META.all()
    snapshot = LIB.snapshot()
    include_watched = str(request.args.get("includeWatched") or "").lower() in ("1", "true", "yes")
    excluded = set((snapshot.get("watchlist") or {}).keys())
    for key, reaction in (snapshot.get("reactions") or {}).items():
        if int(reaction.get("value") or 0) < 0:
            excluded.add(key)
    if not include_watched:
        excluded.update((snapshot.get("watched") or {}).keys())
    candidates = discovery.filter_items(catalog["items"], meta_map, {**filters, "excludeKeys": excluded})
    if not candidates:
        candidates = discovery.filter_items(catalog["items"], meta_map, filters)
    if not candidates:
        return jsonify({"ok": False, "error": "No title matches those filters."}), 404
    picker = random.SystemRandom()
    choices = picker.sample(candidates, min(3, len(candidates)))
    items = public_items(choices, meta_map, snapshot)
    if not items:
        return jsonify({"ok": False, "error": "No playable title matched those filters."}), 404
    for item in choices:
        enqueue_enrichment(item)
    return jsonify({"ok": True, "item": items[0], "options": items, "count": len(candidates)})


def _library_item(record, catalog, meta_map, snapshot):
    local = catalog["items_by_key"].get(record.get("key"))
    merged = dict(local or {})
    merged.update({key: value for key, value in record.items() if value not in (None, "")})
    return public_item(merged, meta_map, snapshot=snapshot)


@app.get("/api/library")
def api_library():
    catalog = get_catalog()
    meta_map = META.all()
    snapshot = LIB.snapshot()
    state_map = library_state_map(snapshot)
    filters = discovery.parse_filters(request.args)

    def visible(record):
        item = catalog["items_by_key"].get(record.get("key")) or record
        return discovery.matches(item, discovery.item_meta(item, meta_map), filters)

    watchlist = []
    for record in sorted((snapshot.get("watchlist") or {}).values(), key=lambda value: value.get("addedAt") or 0, reverse=True):
        if not visible(record):
            continue
        item = _library_item(record, catalog, meta_map, snapshot)
        if item:
            watchlist.append(item)
    watched = []
    for key, info in sorted((snapshot.get("watched") or {}).items(), key=lambda pair: pair[1].get("at") or 0, reverse=True):
        record = {"key": key, "type": key.split(":", 1)[0], "tmdbId": key.split(":", 1)[1]}
        if not visible(record):
            continue
        item = _library_item(record, catalog, meta_map, snapshot)
        if item:
            item["watchedAt"] = info.get("at")
            item["userRating"] = info.get("rating")
            watched.append(item)
    liked = []
    disliked = []
    for key, reaction in (snapshot.get("reactions") or {}).items():
        record = {"key": key, "type": key.split(":", 1)[0], "tmdbId": key.split(":", 1)[1]}
        if not visible(record):
            continue
        item = _library_item(record, catalog, meta_map, snapshot)
        if not item:
            continue
        if int(reaction.get("value") or 0) > 0:
            liked.append(item)
        else:
            disliked.append(item)
    progress = []
    for entry in sorted((snapshot.get("progress") or {}).values(), key=lambda value: value.get("updatedAt") or 0, reverse=True):
        if not visible(entry):
            continue
        item = _library_item(entry, catalog, meta_map, snapshot)
        if item:
            item["resume"] = {
                "position": entry.get("position"),
                "duration": entry.get("duration"),
                "season": entry.get("season"),
                "episode": entry.get("episode"),
                "updatedAt": entry.get("updatedAt"),
                "progressKey": entry.get("progressKey"),
            }
            progress.append(item)
    history = []
    for entry in (snapshot.get("history") or [])[:50]:
        if not visible(entry):
            continue
        item = _library_item(entry, catalog, meta_map, snapshot)
        if item:
            item["openedAt"] = entry.get("at")
            history.append(item)
    lists = []
    for record in (snapshot.get("lists") or {}).values():
        list_items = record.get("items") or []
        if filters.get("kids") or filters.get("age") or filters.get("maxAge") is not None:
            list_items = discovery.filter_items(list_items, meta_map, filters)
        if not list_items:
            continue
        lists.append(
            {
                "id": record.get("id"),
                "name": record.get("name"),
                "source": record.get("source"),
                "kind": "custom",
                "count": len(list_items),
                "updatedAt": record.get("updatedAt") or record.get("createdAt"),
                "samples": public_items(list_items[:6], meta_map, snapshot),
            }
        )
    return jsonify(
        {
            "ok": True,
            "watchlist": watchlist,
            "watched": watched,
            "liked": liked,
            "disliked": disliked,
            "progress": progress,
            "history": history,
            "lists": lists,
            "settings": snapshot.get("settings") or {},
            "state": state_map,
            "stats": {
                "watchlist": len(snapshot.get("watchlist") or {}),
                "watched": len(snapshot.get("watched") or {}),
                "liked": len(liked),
                "progress": len(snapshot.get("progress") or {}),
                "lists": len(snapshot.get("lists") or {}),
            },
        }
    )


@app.post("/api/library")
def api_library_action():
    payload = request.get_json(silent=True) or {}
    action = str(payload.get("action") or "").strip().lower()
    item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
    key = str(payload.get("key") or "").strip()
    if not key and item.get("type") and item.get("tmdbId"):
        key = f"{str(item.get('type')).lower()}:{item.get('tmdbId')}"
    if key and not re.match(r"^(movie|tv):\d+$", key):
        return jsonify({"ok": False, "error": "Invalid title key."}), 400

    try:
        if action == "watchlist-add":
            record = LIB.watchlist_add(item)
            return jsonify({"ok": True, "state": {"key": record["key"], "inWatchlist": True}})
        if action == "watchlist-remove":
            removed = LIB.watchlist_remove(key)
            return jsonify({"ok": True, "removed": removed, "state": {"key": key, "inWatchlist": False}})
        if action in ("like", "dislike", "clear-reaction"):
            value = 1 if action == "like" else -1 if action == "dislike" else 0
            LIB.react(key, value)
            return jsonify({"ok": True, "state": {"key": key, "liked": value > 0, "disliked": value < 0}})
        if action in ("watched", "unwatched"):
            LIB.mark_watched(item, action == "watched", payload.get("rating"))
            return jsonify({"ok": True, "state": {"key": key, "watched": action == "watched"}})
        if action == "rate":
            LIB.mark_watched(item, True, payload.get("value") or payload.get("rating"))
            return jsonify({"ok": True, "state": {"key": key, "watched": True, "rating": payload.get("value") or payload.get("rating")}})
        if action == "progress":
            entry = LIB.save_progress(
                item,
                float(payload.get("position") or 0),
                float(payload.get("duration") or 0),
                int(payload.get("season") or 1),
                int(payload.get("episode") or 1),
            )
            return jsonify({"ok": True, "entry": entry})
        if action == "progress-clear":
            progress_key = str(payload.get("progressKey") or key)
            return jsonify({"ok": True, "removed": LIB.clear_progress(progress_key)})
        if action == "settings":
            values = payload.get("settings") if isinstance(payload.get("settings"), dict) else payload
            return jsonify({"ok": True, "settings": LIB.update_settings(values)})
        if action == "touch-history":
            LIB.touch_history(item, int(payload.get("season") or 1), int(payload.get("episode") or 1))
            return jsonify({"ok": True})
    except (ValueError, TypeError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": False, "error": "Unknown library action."}), 400


@app.get("/api/lists")
def api_lists():
    catalog = get_catalog()
    meta_map = META.all()
    snapshot = LIB.snapshot()
    filters = discovery.parse_filters(request.args)
    lists = list(internet_lists.live_lists(catalog))
    lists.extend(internet_lists.tmdb_lists())
    for record in (snapshot.get("lists") or {}).values():
        lists.append(record)
    summaries = []
    for record in lists:
        items = []
        for raw in record.get("items") or []:
            key = f"{str(raw.get('type') or '').lower()}:{raw.get('tmdbId')}"
            local = catalog["items_by_key"].get(key)
            if local:
                merged = dict(local)
                for field in ("title", "year", "poster", "backdrop", "rating"):
                    if not merged.get(field) and raw.get(field):
                        merged[field] = raw[field]
                items.append(merged)
            else:
                items.append(raw)
        if filters.get("kids") or filters.get("age") or filters.get("maxAge") is not None:
            items = discovery.filter_items(items, meta_map, filters)
        if not items:
            continue
        summaries.append(
            {
                "id": record.get("id"),
                "name": record.get("name"),
                "source": record.get("source") or "",
                "kind": record.get("kind") or "internet",
                "count": len(items),
                "subtitle": record.get("subtitle") or "",
                "updatedAt": record.get("updatedAt") or "",
                "samples": public_items(items[:6], meta_map, snapshot),
            }
        )
    with internet_lists._lock():
        live_at = internet_lists._LIVE_STATE.get("at") or 0
    return jsonify(
        {
            "ok": True,
            "lists": summaries,
            "liveAt": live_at,
            "tmdb": bool(os.environ.get("TMDB_API_KEY")),
            "coverage": META.coverage(len(catalog["items"])),
        }
    )


def _all_lists(catalog, snapshot):
    lists = list(internet_lists.live_lists(catalog))
    lists.extend(internet_lists.tmdb_lists())
    for record in (snapshot.get("lists") or {}).values():
        lists.append(record)
    return lists


@app.get("/api/lists/<path:list_id>")
def api_list_detail(list_id):
    catalog = get_catalog()
    meta_map = META.all()
    snapshot = LIB.snapshot()
    filters = discovery.parse_filters(request.args)
    record = next((entry for entry in _all_lists(catalog, snapshot) if entry.get("id") == list_id), None)
    if not record:
        return jsonify({"ok": False, "error": "List not found."}), 404
    items = []
    for raw in record.get("items") or []:
        key = f"{str(raw.get('type') or '').lower()}:{raw.get('tmdbId')}"
        local = catalog["items_by_key"].get(key)
        merged = dict(local or {})
        for field, value in raw.items():
            if not merged.get(field) and value not in (None, "", 0):
                merged[field] = value
        items.append(merged)
    if filters.get("kids") or filters.get("age") or filters.get("maxAge") is not None:
        items = discovery.filter_items(items, meta_map, filters)
    for item in items[:30]:
        enqueue_enrichment(item)
    return jsonify(
        {
            "ok": True,
            "list": {
                "id": record.get("id"),
                "name": record.get("name"),
                "source": record.get("source") or "",
                "kind": record.get("kind") or "internet",
                "subtitle": record.get("subtitle") or "",
                "count": len(items),
                "updatedAt": record.get("updatedAt") or "",
            },
            "items": public_items(items, meta_map, snapshot),
        }
    )


@app.post("/api/lists/refresh")
def api_lists_refresh():
    catalog = get_catalog()
    result = internet_lists.refresh_live_lists(catalog, get_base(), force=True)
    if os.environ.get("TMDB_API_KEY"):
        internet_lists.tmdb_lists(force=True)
    return jsonify(result)


@app.post("/api/lists/import")
def api_lists_import():
    catalog = get_catalog()
    payload = request.get_json(silent=True) or {}
    url = str(payload.get("url") or "").strip()
    text = payload.get("text")
    name = str(payload.get("name") or "").strip()
    try:
        if url:
            result = internet_lists.import_url_list(url, catalog["items"], LIB)
        elif isinstance(text, str) and text.strip():
            result = internet_lists.import_text_list(text, name, str(payload.get("source") or "Pasted list"), catalog["items"], LIB)
        else:
            return jsonify({"ok": False, "error": "Provide a list URL or paste list text."}), 400
    except (ValueError, OSError, requests.RequestException) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    record = result.get("list") or {}
    meta_map = META.all()
    snapshot = LIB.snapshot()
    result["list"] = {
        "id": record.get("id"),
        "name": record.get("name"),
        "source": record.get("source") or "",
        "kind": "custom",
        "count": record.get("count") or len(record.get("items") or []),
        "updatedAt": record.get("updatedAt") or "",
        "samples": public_items((record.get("items") or [])[:6], meta_map, snapshot),
    }
    return jsonify(result)


@app.delete("/api/lists/<path:list_id>")
def api_list_delete(list_id):
    if not list_id.startswith("custom-"):
        return jsonify({"ok": False, "error": "Only imported lists can be removed."}), 400
    return jsonify({"ok": True, "removed": LIB.remove_list(list_id)})


@app.get("/api/metadata")
def api_metadata():
    return jsonify(metadata_status())


@app.post("/api/metadata")
def api_metadata_action():
    payload = request.get_json(silent=True) or {}
    mode = str(payload.get("mode") or "missing").lower()
    try:
        limit = max(0, int(payload.get("limit") or 0))
    except (TypeError, ValueError):
        limit = 0
    include_errors = mode in ("all", "refresh") or bool(payload.get("includeErrors"))
    queued = start_bulk_enrichment(limit=limit, include_errors=include_errors)
    status = metadata_status()
    status["queuedNow"] = queued
    return jsonify(status)


@app.get("/api/season")
def api_season():
    tmdb_id = request.args.get("tmdbId") or ""
    season = request.args.get("season") or "1"
    if not tmdb_id.isdigit() or not season.isdigit():
        abort(400)
    data = upstream_json(f"/api/catalog/season/{tmdb_id}/{int(season)}", timeout=40)
    if not data or not data.get("ok"):
        return jsonify({"ok": False, "error": "Episodes are temporarily unavailable."}), 502
    return jsonify(localize(data))


@app.get("/api/torrents/search")
def api_torrent_search():
    media_type = (request.args.get("type") or "movie").lower()
    if media_type not in ("movie", "tv"):
        abort(400)
    query = (request.args.get("q") or "").strip()
    tmdb_id = (request.args.get("tmdbId") or "").strip()
    title = (request.args.get("title") or "").strip()
    year = (request.args.get("year") or "").strip()
    imdb_id = (request.args.get("imdbId") or "").strip()
    try:
        season = max(1, int(request.args.get("season", "1")))
        episode = max(1, int(request.args.get("episode", "1")))
    except ValueError:
        season = episode = 1
    if tmdb_id and not title:
        catalog = get_catalog()
        item = catalog["items_by_key"].get(f"{media_type}:{tmdb_id}") or {}
        title = item.get("title") or ""
        year = year or str(item.get("year") or "")
    if not title and not query and not imdb_id:
        return jsonify({"ok": False, "error": "A title or IMDb id is required."}), 400
    refresh = str(request.args.get("refresh") or "").lower() in ("1", "true", "yes")
    try:
        data = torrents.search(
            media_type,
            tmdb_id=tmdb_id or None,
            title=title,
            year=year,
            season=season,
            episode=episode,
            query=query,
            imdb_id=imdb_id,
            refresh=refresh,
        )
    except torrents.TorrentError as exc:
        return jsonify({"ok": False, "error": str(exc), "bridge": torrents.bridge_info()}), 502
    except Exception as exc:  # noqa: BLE001 - provider failures are runtime data
        app.logger.warning("torrent search failed: %s", exc)
        return jsonify({"ok": False, "error": "Torrent search failed. Try again.", "bridge": torrents.bridge_info()}), 502
    data["bridge"] = torrents.bridge_info()
    return jsonify(data)


@app.post("/api/torrents/play")
def api_torrent_play():
    payload = request.get_json(silent=True) or {}
    stored = recall_torrent(payload.get("key")) if payload.get("key") else {}
    magnet = str(stored.get("magnet") or payload.get("magnet") or "").strip()
    if not magnet.startswith("magnet:?"):
        return jsonify({"ok": False, "error": "A magnet URI or source key is required."}), 400
    info_hash = str(stored.get("infoHash") or torrents.info_hash_from_magnet(magnet) or "")
    if not info_hash:
        return jsonify({"ok": False, "error": "The magnet URI does not contain a usable info hash."}), 400
    try:
        file_index = int(payload.get("fileIndex")) if payload.get("fileIndex") is not None else None
    except (TypeError, ValueError):
        file_index = None
    file_hint = str(payload.get("fileHint") or "")[:300]
    try:
        result = torrents.add_torrent(
            magnet,
            file_index=file_index,
            file_hint=file_hint,
            timeout=75,
        )
    except torrents.TorrentError as exc:
        return jsonify({"ok": False, "error": str(exc), "bridge": torrents.bridge_info()}), 502
    except Exception as exc:  # noqa: BLE001 - bridge/network failures are runtime data
        app.logger.warning("torrent add failed: %s", exc)
        return jsonify({"ok": False, "error": "The torrent could not be added."}), 502

    files = [item for item in (result.get("files") or []) if isinstance(item, dict)]
    videos = [item for item in files if item.get("isVideo")]
    selected = result.get("selected")
    if not isinstance(selected, int) or selected < 0 or selected >= len(files):
        selected = videos[0]["index"] if videos else (files[0]["index"] if files else -1)
    if selected < 0:
        return jsonify({"ok": False, "error": "This torrent does not contain any playable files."}), 422
    chosen = next((item for item in files if item.get("index") == selected), {})
    info_hash = str(result.get("infoHash") or info_hash).lower()
    file_name = str(chosen.get("name") or "")
    content_type = str(chosen.get("type") or "video/mp4")
    quality_label, quality_value = torrents.parse_quality(file_name)
    if payload.get("qualityValue"):
        try:
            quality_value = int(payload.get("qualityValue"))
            quality_label = f"{quality_value}p" if quality_value else quality_label
        except (TypeError, ValueError):
            pass
    try:
        size_bytes = int(payload.get("sizeBytes") or chosen.get("length") or 0)
    except (TypeError, ValueError):
        size_bytes = 0
    title = str(payload.get("title") or result.get("name") or stored.get("title") or "")
    key = str(payload.get("key") or "") if stored else ""
    if not key:
        key = remember_torrent(
            magnet,
            info_hash,
            title=title,
            content_type=content_type,
            file_name=file_name,
            quality=quality_value,
            size=size_bytes,
        )
    else:
        with _torrent_key_lock:
            if key in _torrent_keys:
                _torrent_keys[key].update(
                    {
                        "contentType": content_type,
                        "fileName": file_name,
                        "quality": int(quality_value or 0),
                        "size": int(size_bytes or 0),
                        "at": time.time(),
                    }
                )
    hint_matches = bool(
        file_hint
        and torrents.normalize_title(file_hint)
        and torrents.normalize_title(file_hint) in torrents.normalize_title(file_name)
    )
    if file_index is None and not hint_matches and len(videos) > 1:
        return jsonify(
            {
                "ok": True,
                "needsFile": True,
                "key": key,
                "infoHash": info_hash,
                "name": result.get("name") or title,
                "selected": selected,
                "files": videos,
                "numPeers": result.get("numPeers") or 0,
                "progress": result.get("progress") or 0,
            }
        )
    token = issue_token(
        {
            "t": "tor",
            "k": key,
            "h": info_hash,
            "i": int(selected),
            "ct": content_type,
            "n": file_name,
            "q": int(quality_value),
        },
        6 * 3600,
    )
    return jsonify(
        {
            "ok": True,
            "key": key,
            "infoHash": info_hash,
            "name": result.get("name") or title,
            "fileName": file_name,
            "fileIndex": selected,
            "files": videos,
            "numPeers": result.get("numPeers") or 0,
            "progress": result.get("progress") or 0,
            "token": token,
            "stream": {
                "kind": "torrent",
                "resolution": int(quality_value or 0),
                "size": size_bytes,
                "content_type": content_type,
                "label": quality_label or "Auto",
            },
        }
    )


@app.get("/api/torrents/status")
def api_torrent_status():
    token = request.args.get("token") or ""
    payload = verify_token(token)
    if not payload or payload.get("t") != "tor":
        abort(403)
    try:
        status = torrents.torrent_status(payload.get("h") or "")
    except torrents.TorrentError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    key = recall_torrent(payload.get("k"))
    status["stream"] = {
        "fileIndex": int(payload.get("i") or 0),
        "fileName": payload.get("n") or key.get("fileName") or "",
        "quality": int(payload.get("q") or 0),
    }
    return jsonify({"ok": True, "status": status})


@app.post("/api/torrents/remove")
def api_torrent_remove():
    payload = request.get_json(silent=True) or {}
    token = str(payload.get("token") or "")
    verified = verify_token(token) if token else None
    info_hash = str((verified or {}).get("h") or payload.get("infoHash") or "")
    remove_key = str((verified or {}).get("k") or payload.get("key") or "")
    if not info_hash:
        return jsonify({"ok": False, "error": "A valid torrent token is required."}), 400
    try:
        result = torrents.remove_torrent(info_hash)
    except torrents.TorrentError:
        result = {"ok": True, "removed": False}
    if remove_key:
        with _torrent_key_lock:
            _torrent_keys.pop(remove_key, None)
    if token:
        with _token_lock:
            _token_store.pop(token, None)
    return jsonify({"ok": True, **result})


# -- background downloads ---------------------------------------------------


DOWNLOAD_PUBLIC_FIELDS = (
    "id",
    "title",
    "baseTitle",
    "displayTitle",
    "fileName",
    "contentType",
    "quality",
    "size",
    "downloaded",
    "progress",
    "downloadSpeed",
    "numPeers",
    "timeRemaining",
    "mediaType",
    "tmdbId",
    "year",
    "poster",
    "backdrop",
    "season",
    "episode",
    "item",
    "status",
    "error",
    "createdAt",
    "updatedAt",
    "completedAt",
)


def public_download(record):
    if not record:
        return None
    data = {key: record.get(key) for key in DOWNLOAD_PUBLIC_FIELDS if key in record}
    item = data.get("item") if isinstance(data.get("item"), dict) else {}
    for key in ("poster", "backdrop"):
        value = str(data.get(key) or item.get(key) or "")
        if value.startswith(("http://", "https://")):
            value = media_url(value) or ""
        data[key] = value
    if item:
        item = dict(item)
        for key in ("poster", "backdrop"):
            value = str(item.get(key) or "")
            if value.startswith(("http://", "https://")):
                item[key] = media_url(value) or ""
        data["item"] = item
    data["playable"] = bool(record.get("magnet")) and record.get("status") != "error"
    data["local"] = record.get("status") == "done" and DOWNLOADS.local_path(record) is not None
    return data


def download_resume(record):
    media_type = str(record.get("mediaType") or "").lower()
    tmdb_id = str(record.get("tmdbId") or "")
    if media_type not in ("movie", "tv") or not tmdb_id.isdigit():
        return None
    if media_type == "tv":
        key = f"tv:{tmdb_id}:s{max(1, int(record.get('season') or 1))}:e{max(1, int(record.get('episode') or 1))}"
    else:
        key = f"movie:{tmdb_id}"
    entry = (LIB.snapshot().get("progress") or {}).get(key)
    if not entry:
        return None
    return {
        "position": entry.get("position"),
        "duration": entry.get("duration"),
        "season": entry.get("season"),
        "episode": entry.get("episode"),
    }


@app.get("/api/downloads")
def api_downloads():
    records = DOWNLOADS.list()
    return jsonify(
        {
            "ok": True,
            "downloads": [public_download(record) for record in records],
            "stats": DOWNLOADS.stats(),
        }
    )


@app.post("/api/downloads/add")
def api_downloads_add():
    payload = request.get_json(silent=True) or {}
    token = str(payload.get("token") or "")
    verified = verify_token(token)
    if not verified or verified.get("t") != "tor":
        return jsonify({"ok": False, "error": "Start the torrent first, then add it to Downloads."}), 400
    stored = recall_torrent(verified.get("k"))
    magnet = str(stored.get("magnet") or "")
    if not magnet.startswith("magnet:?"):
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "This torrent link expired. Open the source again and choose Downloads.",
                }
            ),
            409,
        )
    extra = {
        "title": payload.get("title") or stored.get("title") or "",
        "baseTitle": payload.get("baseTitle") or payload.get("title") or stored.get("title") or "",
        "displayTitle": payload.get("displayTitle") or payload.get("title") or "",
        "mediaType": payload.get("mediaType") or payload.get("type") or "",
        "tmdbId": payload.get("tmdbId") or "",
        "year": payload.get("year") or "",
        "poster": payload.get("poster") or "",
        "backdrop": payload.get("backdrop") or "",
        "season": payload.get("season") or 1,
        "episode": payload.get("episode") or 1,
        "item": payload.get("item"),
    }
    try:
        record = DOWNLOADS.add(verified, magnet, extra)
    except downloads.DownloadError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "download": public_download(record)})


@app.post("/api/downloads/play")
def api_downloads_play():
    payload = request.get_json(silent=True) or {}
    record = DOWNLOADS.get(str(payload.get("id") or ""))
    if not record:
        return jsonify({"ok": False, "error": "Download not found."}), 404
    stream_info = DOWNLOADS.stream_payload(record)
    quality = int(record.get("quality") or 0)
    label = f"{quality}p" if quality else "Download"
    resume = download_resume(record)
    if record.get("status") == "done":
        path = DOWNLOADS.local_path(record)
        if path is None:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "The downloaded file is missing. Retry the download or remove it.",
                    }
                ),
                409,
            )
        token = issue_token({"t": "dl", "id": record.get("id")}, 12 * 3600)
        stream = {
            "kind": "download",
            "url": f"/api/downloads/stream?token={quote(token, safe='')}",
            "label": label,
            "resolution": quality,
            "content_type": str(record.get("contentType") or mimetypes.guess_type(str(path))[0] or "video/mp4"),
            "size": int(record.get("size") or 0),
        }
    else:
        if not stream_info["magnet"]:
            return jsonify({"ok": False, "error": "This download lost its magnet link."}), 409
        key = remember_torrent(
            stream_info["magnet"],
            stream_info["infoHash"],
            title=str(record.get("title") or ""),
            content_type=stream_info["contentType"],
            file_name=stream_info["fileName"],
            quality=quality,
            size=stream_info["size"],
        )
        token = issue_token(
            {
                "t": "tor",
                "k": key,
                "h": stream_info["infoHash"],
                "i": stream_info["fileIndex"],
                "ct": stream_info["contentType"],
                "n": stream_info["fileName"],
                "q": quality,
            },
            6 * 3600,
        )
        stream = {
            "kind": "torrent",
            "token": token,
            "label": label,
            "resolution": quality,
            "content_type": stream_info["contentType"],
            "size": stream_info["size"],
        }
    media_type = str(record.get("mediaType") or "").lower()
    item = record.get("item") if isinstance(record.get("item"), dict) else {}
    return jsonify(
        {
            "ok": True,
            "download": public_download(record),
            "stream": stream,
            "resume": resume,
            "context": {
                "type": media_type if media_type in ("movie", "tv") else "movie",
                "tmdbId": record.get("tmdbId") or "",
                "title": record.get("baseTitle") or record.get("title") or "",
                "displayTitle": record.get("displayTitle") or record.get("title") or "",
                "year": record.get("year") or "",
                "season": int(record.get("season") or 1),
                "episode": int(record.get("episode") or 1),
                "item": item,
            },
        }
    )


@app.get("/api/downloads/stream")
def api_downloads_stream():
    token = request.args.get("token") or ""
    payload = verify_token(token)
    if not payload or payload.get("t") != "dl":
        abort(403)
    record = DOWNLOADS.get(str(payload.get("id") or ""))
    path = DOWNLOADS.local_path(record)
    if path is None:
        abort(404)
    mimetype = mimetypes.guess_type(str(path))[0] or str((record or {}).get("contentType") or "video/mp4")
    response = send_file(path, mimetype=mimetype, conditional=True, max_age=0)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Accept-Ranges"] = "bytes"
    return response


@app.post("/api/downloads/remove")
def api_downloads_remove():
    payload = request.get_json(silent=True) or {}
    record_id = str(payload.get("id") or "")
    if not record_id:
        return jsonify({"ok": False, "error": "A download id is required."}), 400
    removed = DOWNLOADS.remove(record_id, delete_files=bool(payload.get("deleteFiles", True)))
    return jsonify({"ok": True, "removed": removed})


@app.post("/api/downloads/retry")
def api_downloads_retry():
    payload = request.get_json(silent=True) or {}
    record = DOWNLOADS.retry(str(payload.get("id") or ""))
    if not record:
        return jsonify({"ok": False, "error": "Download not found."}), 404
    return jsonify({"ok": True, "download": public_download(record)})


@app.get("/api/play")
def api_play():
    media_type = (request.args.get("type") or "").lower()
    tmdb_id = request.args.get("tmdbId") or ""
    try:
        season = max(1, int(request.args.get("season", "1")))
        episode = max(1, int(request.args.get("episode", "1")))
    except ValueError:
        abort(400)
    if media_type not in ("movie", "tv") or not tmdb_id.isdigit():
        abort(400)

    data = upstream_json(
        f"/api/embed-tmdb/{tmdb_id}",
        params={"type": media_type, "se": season, "ep": episode},
        timeout=50,
    )
    if not data or not data.get("ok"):
        return jsonify({"ok": False, "error": "This title is not available right now.", "torrentFallback": True}), 502

    exp = int(data.get("exp") or (time.time() + 3600))
    sig = data.get("sig") or ""
    streams = []
    seen = set()
    for stream in data.get("streams") or []:
        url = stream.get("url")
        if not url or url in seen:
            continue
        seen.add(url)
        token = issue_token(
            {
                "t": "vid",
                "u": url,
                "x": exp,
                "s": sig,
            },
            max(60, min(exp - int(time.time()), 6 * 3600)),
        )
        streams.append(
            {
                "resolution": int(stream.get("resolution") or 0),
                "size": int(stream.get("size") or 0),
                "content_type": stream_content_type(url),
                "token": token,
            }
        )

    mp4 = data.get("mp4")
    if mp4 and mp4 not in seen:
        token = issue_token(
            {
                "t": "vid",
                "u": mp4,
                "x": exp,
                "s": sig,
            },
            max(60, min(exp - int(time.time()), 6 * 3600)),
        )
        streams.append(
            {
                "resolution": int(data.get("resolution") or 0),
                "size": 0,
                "content_type": stream_content_type(mp4),
                "token": token,
            }
        )

    streams.sort(key=lambda item: item["resolution"])
    if not streams:
        return jsonify({"ok": False, "error": "No playable stream was returned.", "torrentFallback": True}), 502

    captions = []
    for caption in data.get("captions") or []:
        url = caption.get("url")
        if not url:
            continue
        captions.append(
            {
                "lang": caption.get("lang") or "",
                "name": caption.get("name") or caption.get("lang") or "",
                "token": issue_token(
                    {
                        "t": "sub",
                        "u": url,
                    },
                    6 * 3600,
                ),
            }
        )

    snapshot = LIB.snapshot()
    progress_key = (
        f"{media_type}:{tmdb_id}:s{season}:e{episode}"
        if media_type == "tv"
        else f"{media_type}:{tmdb_id}"
    )
    resume_entry = (snapshot.get("progress") or {}).get(progress_key)

    return jsonify(
        {
            "ok": True,
            "title": data.get("title") or "",
            "type": media_type,
            "season": season,
            "episode": episode,
            "expires_at": exp,
            "streams": streams,
            "captions": captions,
            "resume": {
                "position": (resume_entry or {}).get("position"),
                "duration": (resume_entry or {}).get("duration"),
            }
            if resume_entry
            else None,
        }
    )


@app.get("/api/cast/devices")
def api_cast_devices():
    if not casting.available():
        return jsonify(
            {
                "ok": True,
                "available": False,
                "devices": [],
                "error": "pychromecast is not installed. Run: pip install -r requirements.txt",
            }
        )
    refresh = str(request.args.get("refresh") or "").lower() in ("1", "true", "yes")
    return jsonify({"ok": True, "available": True, "devices": casting.devices(refresh=refresh)})


@app.post("/api/cast/play")
def api_cast_play():
    if not casting.available():
        return jsonify({"ok": False, "error": "pychromecast is not installed. Run: pip install -r requirements.txt"}), 503
    if BIND_HOST in ("127.0.0.1", "localhost", "::1"):
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "The server is bound to loopback. Restart it with --host 0.0.0.0 so the TV can reach the stream.",
                }
            ),
            409,
        )
    payload = request.get_json(silent=True) or {}
    device_id = str(payload.get("device_id") or "").strip()
    token = str(payload.get("token") or "")
    media = verify_token(token)
    if not device_id or not media or media.get("t") not in ("vid", "tor", "dl"):
        return jsonify({"ok": False, "error": "A valid stream token and Cast device are required."}), 400
    try:
        position = max(0.0, float(payload.get("position") or 0.0))
    except (TypeError, ValueError):
        position = 0.0
    if media.get("t") == "tor":
        media_url = f"{media_base()}/api/torrents/stream?token={quote(token, safe='')}"
        content_type = media.get("ct") or "video/mp4"
    elif media.get("t") == "dl":
        media_url = f"{media_base()}/api/downloads/stream?token={quote(token, safe='')}"
        record = DOWNLOADS.get(str(media.get("id") or "")) or {}
        content_type = record.get("contentType") or "video/mp4"
    else:
        media_url = f"{media_base()}/api/video?token={quote(token, safe='')}"
        content_type = stream_content_type(media.get("u") or "")
    try:
        result = casting.play(
            device_id,
            media_url,
            content_type=content_type,
            title=str(payload.get("title") or ""),
            position=position,
            thumb=cast_thumb(payload.get("thumb")),
        )
    except CastError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502
    return jsonify({"ok": True, **result})


@app.get("/api/cast/status")
def api_cast_status():
    return jsonify(casting.status())


@app.post("/api/cast/control")
def api_cast_control():
    payload = request.get_json(silent=True) or {}
    try:
        result = casting.control(str(payload.get("action") or ""), payload.get("value"))
    except CastError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502
    return jsonify({"ok": True, **result})


@app.post("/api/cast/stop")
def api_cast_stop():
    return jsonify(casting.stop())


@app.get("/api/video")
def api_video():
    token = request.args.get("token") or ""
    payload = verify_token(token)
    if not payload or payload.get("t") != "vid":
        abort(403)
    range_header = request.headers.get("Range")
    last_status = None
    for candidate in video_candidates(payload):
        try:
            upstream = proxy_video_request(candidate, range_header)
        except requests.RequestException:
            continue
        if upstream.status_code in (200, 206):
            headers = copy_headers(upstream)
            headers["Referrer-Policy"] = "no-referrer"
            return Response(
                stream_body(upstream),
                status=upstream.status_code,
                headers=headers,
                direct_passthrough=True,
            )
        last_status = upstream.status_code
        upstream.close()
        if upstream.status_code not in (401, 403, 410, 429, 500, 502, 503, 504):
            break
    return jsonify({"ok": False, "error": "Video source is unavailable.", "status": last_status}), 502


@app.get("/api/torrents/stream")
def api_torrent_stream():
    token = request.args.get("token") or ""
    payload = verify_token(token)
    if not payload or payload.get("t") != "tor":
        abort(403)
    info_hash = str(payload.get("h") or "")
    try:
        index = max(0, int(payload.get("i") or 0))
    except (TypeError, ValueError):
        abort(400)
    stored = recall_torrent(payload.get("k"))
    magnet = str(stored.get("magnet") or "")
    try:
        upstream = torrents.open_stream(magnet, info_hash, index, request.headers.get("Range"))
    except torrents.TorrentError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502
    headers = copy_headers(upstream)
    headers.setdefault("Content-Type", payload.get("ct") or "video/mp4")
    headers["Cache-Control"] = "no-store"
    headers["Referrer-Policy"] = "no-referrer"
    return Response(
        stream_body(upstream),
        status=upstream.status_code,
        headers=headers,
        direct_passthrough=True,
    )


@app.get("/api/image")
def api_image():
    token = request.args.get("token") or ""
    payload = verify_token(token)
    if not payload or payload.get("t") != "img":
        abort(403)
    url = payload.get("u") or ""
    if not url.startswith(("http://", "https://")):
        abort(400)
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    cached = IMAGE_DIR / digest
    mime = mimetypes.guess_type(urlparse(url).path)[0] or "image/jpeg"
    if cached.exists():
        return send_file(cached, mimetype=mime, max_age=604800)
    try:
        upstream = session().get(url, stream=True, timeout=(10, 30))
    except requests.RequestException:
        abort(502)
    if upstream.status_code != 200:
        upstream.close()
        abort(404)
    content_type = upstream.headers.get("Content-Type", "image/jpeg")
    temp = cached.with_name(cached.name + f".{secrets.token_hex(4)}.tmp")
    try:
        with temp.open("wb") as handle:
            for chunk in upstream.iter_content(chunk_size=64 * 1024):
                if chunk:
                    handle.write(chunk)
        temp.replace(cached)
    finally:
        upstream.close()
        if temp.exists():
            temp.unlink(missing_ok=True)
    return send_file(cached, mimetype=content_type, max_age=604800)


@app.get("/api/subtitle")
def api_subtitle():
    token = request.args.get("token") or ""
    payload = verify_token(token)
    if not payload or payload.get("t") != "sub":
        abort(403)
    url = payload.get("u") or ""
    if url.startswith("/api/proxy/video?"):
        nested = parse_qs(urlparse(url).query).get("url", [None])[0]
        if nested:
            url = unquote(nested)
    if url.startswith("/"):
        url = get_base() + url
    try:
        upstream = session().get(
            url,
            headers={
                "User-Agent": dc.BROWSER_HEADERS["User-Agent"],
                "Accept": "*/*",
                "Referer": referer_for(url),
            },
            timeout=(10, 30),
        )
    except requests.RequestException:
        abort(502)
    if upstream.status_code != 200:
        abort(404)
    content_type = upstream.headers.get("Content-Type", "text/vtt; charset=utf-8")
    return Response(upstream.content, content_type=content_type)


def main():
    global BIND_HOST
    parser = argparse.ArgumentParser(description="Self-hosted catalog browser and player.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    BIND_HOST = args.host
    threading.Thread(target=refresh_referers, daemon=True).start()
    app.run(host=args.host, port=args.port, debug=False, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
