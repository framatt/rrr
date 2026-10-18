#!/usr/bin/env python3
"""Internet list support: live upstream charts, optional TMDB charts and
user-imported lists (JSON / CSV / plain text / HTML table).

The importer fetches a public URL server-side, so it applies an SSRF guard:
only http(s), no credentials, public unicast addresses, bounded redirects,
bounded size and a hard timeout.  Titles are matched to the local catalog by
TMDB id, then title+year, then a conservative fuzzy title match.
"""

from __future__ import annotations

import csv
import difflib
import html
import io
import json
import os
import re
import socket
import time
import unicodedata
from collections import OrderedDict
from datetime import datetime, timezone
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

ROOT = Path(__file__).resolve().parent
LIVE_CACHE = ROOT / ".app_cache" / "live_lists.json"
MAX_BYTES = 5 * 1024 * 1024
MAX_ITEMS = 500
FETCH_TIMEOUT = (6, 18)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
TMDB_IMAGE = "https://image.tmdb.org/t/p/w342"
TMDB_BACKDROP = "https://image.tmdb.org/t/p/original"

_LIVE_STATE: dict[str, Any] = {"at": 0.0, "lists": [], "sources": []}
_TMDB_STATE: dict[str, Any] = {"at": 0.0, "lists": []}
_STATE_LOCK = None


def _lock():
    global _STATE_LOCK
    if _STATE_LOCK is None:
        import threading

        _STATE_LOCK = threading.RLock()
    return _STATE_LOCK


def _slug(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:70] or "list"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_item(item: dict[str, Any]) -> dict[str, Any] | None:
    media_type = str(item.get("type") or item.get("media_type") or "").lower()
    tmdb_id = item.get("tmdbId") or item.get("tmdb_id")
    if media_type not in ("movie", "tv"):
        media_type = "tv" if item.get("name") and item.get("first_air_date") else "movie"
    if not str(tmdb_id or "").isdigit():
        return None
    return {
        "type": media_type,
        "tmdbId": int(tmdb_id),
        "title": str(item.get("title") or item.get("name") or ""),
        "year": str(
            item.get("year")
            or str(item.get("release_date") or item.get("first_air_date") or "")[:4]
            or ""
        ),
        "poster": str(item.get("poster") or ""),
        "backdrop": str(item.get("backdrop") or ""),
        "rating": item.get("rating"),
        "popularity": item.get("popularity") or 0,
        "overview": str(item.get("overview") or ""),
    }


def _list_record(list_id: str, name: str, source: str, kind: str, items: list[dict[str, Any]], subtitle: str = "") -> dict[str, Any]:
    seen = set()
    unique = []
    for item in items:
        cleaned = _clean_item(item)
        if not cleaned:
            continue
        key = f"{cleaned['type']}:{cleaned['tmdbId']}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(cleaned)
        if len(unique) >= MAX_ITEMS:
            break
    return {
        "id": list_id,
        "name": str(name or "Untitled list")[:180],
        "source": source,
        "kind": kind,
        "subtitle": subtitle[:300],
        "count": len(unique),
        "items": unique,
        "updatedAt": _now_iso(),
    }


def _dedupe_lists(lists: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    output = []
    for record in lists:
        if not record or not record.get("count"):
            continue
        base_id = record["id"]
        list_id = base_id
        suffix = 2
        while list_id in seen:
            list_id = f"{base_id}-{suffix}"
            suffix += 1
        record["id"] = list_id
        seen.add(list_id)
        output.append(record)
    return output


# -- built-in and live lists -------------------------------------------------


def catalog_lists(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    lists = []
    local_items = catalog.get("items") or []
    index = CatalogIndex(local_items)
    hero = []
    for entry in catalog.get("hero") or []:
        if isinstance(entry, dict) and isinstance(entry.get("items"), list):
            hero.extend(entry.get("items") or [])
        elif isinstance(entry, dict):
            hero.append(entry)
    if hero:
        lists.append(_list_record("hero:featured", "Featured today", "NetMirror", "internet", hero, "Hero picks from the live home feed."))
    for index_name, (window, items) in enumerate((catalog.get("trending") or {}).items()):
        if items:
            label = "Trending today" if window == "day" else f"Trending this {window}"
            lists.append(_list_record(f"trend:{window}", label, "NetMirror", "internet", items, "Live trending chart."))
    for rail in catalog.get("rails") or []:
        title = rail.get("title") or rail.get("name") or ""
        raw_items = rail.get("items") or []
        if not title or not raw_items:
            continue
        resolved = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            if str(raw.get("tmdbId") or "").isdigit() and int(raw.get("tmdbId") or 0) > 0:
                resolved.append(raw)
                continue
            local = index.match(raw, fuzzy=False)
            if local:
                resolved.append(local)
        if resolved:
            lists.append(_list_record(f"rail:{_slug(title)}", title, "NetMirror", "internet", resolved, "Live home-feed collection matched to available titles."))

    items = catalog.get("items") or []
    if items:
        top = sorted(items, key=lambda item: float(item.get("rating") or 0), reverse=True)[:60]
        lists.append(_list_record("catalog:top-rated", "Top rated on this server", "Catalog", "catalog", top, "Highest viewer ratings in the local catalog."))
        current_year = time.gmtime().tm_year
        new = [item for item in items if str(item.get("year") or "")[:4].isdigit() and int(str(item.get("year"))[:4]) >= current_year - 1]
        new.sort(key=lambda item: (str(item.get("year") or ""), float(item.get("rating") or 0)), reverse=True)
        lists.append(_list_record("catalog:new", "New releases", "Catalog", "catalog", new[:60], "Recently released titles available here."))
        hidden = [
            item
            for item in items
            if float(item.get("rating") or 0) >= 7.0 and float(item.get("popularity") or 0) < 55
        ]
        hidden.sort(key=lambda item: float(item.get("rating") or 0), reverse=True)
        lists.append(_list_record("catalog:hidden", "Hidden gems", "Catalog", "catalog", hidden[:60], "High ratings and smaller audiences."))
    return _dedupe_lists(lists)


def load_live_cache() -> None:
    if not LIVE_CACHE.exists():
        return
    try:
        data = json.loads(LIVE_CACHE.read_text(encoding="utf-8"))
        lists = data.get("lists") or []
        if isinstance(lists, list):
            with _lock():
                _LIVE_STATE.update(at=float(data.get("at") or 0), lists=lists, sources=data.get("sources") or [])
    except (OSError, ValueError):
        return


def save_live_cache(lists: list[dict[str, Any]], sources: list[str]) -> None:
    LIVE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    temp = LIVE_CACHE.with_suffix(".json.tmp")
    temp.write_text(json.dumps({"at": time.time(), "lists": lists, "sources": sources}, ensure_ascii=False), encoding="utf-8")
    temp.replace(LIVE_CACHE)


def refresh_live_lists(catalog: dict[str, Any], base: str, force: bool = False) -> dict[str, Any]:
    """Fetch the current home feed.  Throttled to once every ten minutes."""
    now = time.time()
    with _lock():
        if not force and _LIVE_STATE["lists"] and now - _LIVE_STATE["at"] < 600:
            return {"ok": True, "cached": True, "at": _LIVE_STATE["at"], "sources": _LIVE_STATE["sources"]}
    try:
        import download_catalog as dc

        hero, trending, rails, sources = dc.fetch_home(base, "IN")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:300]}

    merged = {"hero": hero, "trending": trending, "rails": rails, "items": catalog.get("items") or []}
    lists = catalog_lists(merged)
    with _lock():
        _LIVE_STATE.update(at=time.time(), lists=lists, sources=sources)
    save_live_cache(lists, sources)
    return {"ok": True, "cached": False, "at": _LIVE_STATE["at"], "sources": sources, "count": len(lists)}


def live_lists(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    load_live_cache()
    with _lock():
        if _LIVE_STATE["lists"]:
            return list(_LIVE_STATE["lists"])
    return catalog_lists(catalog)


# -- optional TMDB charts ----------------------------------------------------


def tmdb_lists(force: bool = False) -> list[dict[str, Any]]:
    key = os.environ.get("TMDB_API_KEY", "").strip()
    if not key:
        return []
    now = time.time()
    with _lock():
        if not force and _TMDB_STATE["lists"] and now - _TMDB_STATE["at"] < 1800:
            return list(_TMDB_STATE["lists"])
    endpoints = [
        ("trending", "TMDB · Trending this week", "/3/trending/all/week", {}),
        ("top-movies", "TMDB · Top rated movies", "/3/movie/top_rated", {"page": "1"}),
        ("top-tv", "TMDB · Top rated series", "/3/tv/top_rated", {"page": "1"}),
        ("popular-movies", "TMDB · Popular movies", "/3/movie/popular", {"page": "1"}),
        ("popular-tv", "TMDB · Popular series", "/3/tv/popular", {"page": "1"}),
    ]
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    lists = []
    for slug, name, path, params in endpoints:
        try:
            response = session.get(
                f"https://api.themoviedb.org{path}",
                params={**params, "api_key": key, "language": "en-US"},
                timeout=(6, 15),
            )
            if response.status_code != 200:
                continue
            items = []
            for entry in (response.json().get("results") or [])[:60]:
                media_type = entry.get("media_type") or ("tv" if path.startswith("/3/tv") else "movie")
                if media_type not in ("movie", "tv"):
                    continue
                poster = entry.get("poster_path")
                backdrop = entry.get("backdrop_path")
                items.append(
                    {
                        "type": media_type,
                        "tmdbId": entry.get("id"),
                        "title": entry.get("title") or entry.get("name") or "",
                        "year": str(entry.get("release_date") or entry.get("first_air_date") or "")[:4],
                        "poster": f"{TMDB_IMAGE}{poster}" if poster else "",
                        "backdrop": f"{TMDB_BACKDROP}{backdrop}" if backdrop else "",
                        "rating": entry.get("vote_average"),
                        "popularity": entry.get("popularity") or 0,
                        "overview": entry.get("overview") or "",
                    }
                )
            lists.append(_list_record(f"tmdb:{slug}", name, "TMDB", "internet", items, "Fetched from TMDB with your API key."))
        except requests.RequestException:
            continue
    with _lock():
        _TMDB_STATE.update(at=time.time(), lists=lists)
    return lists


# -- imported lists ----------------------------------------------------------


def _public_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Only http and https URLs can be imported.")
    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("URL must not contain credentials.")
    if parsed.hostname.lower() in {"localhost", "localhost.localdomain"} or parsed.hostname.lower().endswith(".local"):
        raise ValueError("Private hosts are not allowed.")
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)}
    except OSError as exc:
        raise ValueError(f"Could not resolve host: {exc}") from exc
    for address in addresses:
        ip = ip_address(address)
        if not ip.is_global:
            raise ValueError("Private or reserved network addresses are not allowed.")
    return parsed.geturl()


def fetch_public_text(url: str, max_redirects: int = 4) -> tuple[str, str, str]:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/csv, text/plain, text/html;q=0.9, */*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    current = _public_url(url)
    for _ in range(max_redirects + 1):
        response = session.get(current, timeout=FETCH_TIMEOUT, allow_redirects=False, stream=True)
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise ValueError("Redirect without a destination.")
            current = _public_url(urljoin(current, location))
            continue
        if response.status_code != 200:
            response.close()
            raise ValueError(f"List URL returned HTTP {response.status_code}.")
        chunks = []
        size = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            size += len(chunk)
            if size > MAX_BYTES:
                response.close()
                raise ValueError("List is larger than 5 MB.")
            chunks.append(chunk)
        content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip()
        response.close()
        raw = b"".join(chunks)
        charset = "utf-8"
        match = re.search(r"charset=([\w-]+)", response.headers.get("Content-Type") or "", re.I)
        if match:
            charset = match.group(1)
        return raw.decode(charset, "replace"), current, content_type
    raise ValueError("Too many redirects.")


def _entry(title: Any, year: Any = "", tmdb_id: Any = None, media_type: Any = "", poster: str = "", rating: Any = None) -> dict[str, Any] | None:
    text = str(title or "").strip()
    if not text or len(text) > 300:
        return None
    year_text = str(year or "").strip()[:4]
    if year_text and not year_text.isdigit():
        year_text = ""
    media = str(media_type or "").lower()
    if media not in ("movie", "tv"):
        media = ""
    result = {"title": text, "year": year_text, "tmdbId": None, "type": media, "poster": poster, "rating": rating}
    if str(tmdb_id or "").isdigit():
        result["tmdbId"] = int(tmdb_id)
    return result


def parse_json_payload(text: str) -> tuple[list[dict[str, Any]], str]:
    data = json.loads(text)
    name = ""
    if isinstance(data, dict):
        name = str(data.get("name") or data.get("title") or data.get("list_name") or "")
        for key in ("results", "items", "entries", "titles", "list"):
            value = data.get(key)
            if isinstance(value, list):
                data = value
                break
        else:
            data = [data]
    entries = []
    if isinstance(data, list):
        for raw in data:
            if isinstance(raw, str):
                entry = _entry(raw)
            elif isinstance(raw, dict):
                entry = _entry(
                    raw.get("title") or raw.get("name"),
                    raw.get("year") or str(raw.get("release_date") or raw.get("first_air_date") or "")[:4],
                    raw.get("tmdbId") or raw.get("tmdb_id"),
                    raw.get("type") or raw.get("media_type"),
                    raw.get("poster") or "",
                    raw.get("rating") or raw.get("vote_average"),
                )
            else:
                entry = None
            if entry:
                entries.append(entry)
    return entries, name


def parse_csv_payload(text: str) -> tuple[list[dict[str, Any]], str]:
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    rows = [row for row in reader if any(str(cell).strip() for cell in row)]
    if not rows:
        return [], ""
    header = [str(cell).strip().lower() for cell in rows[0]]
    has_header = any(cell in header for cell in ("title", "name", "movie", "film", "series", "year", "tmdbid", "tmdb_id", "const"))
    name = ""
    entries = []
    if has_header:
        index = {cell: pos for pos, cell in enumerate(header)}

        def col(*names):
            for candidate in names:
                if candidate in index:
                    return index[candidate]
            return None

        title_col = col("title", "name", "movie", "film", "series")
        year_col = col("year", "release_year", "releaseyear")
        tmdb_col = col("tmdbid", "tmdb_id", "tmdb")
        type_col = col("type", "media_type", "mediatype")
        poster_col = col("poster", "poster_url", "image")
        rating_col = col("rating", "vote_average", "imdb_rating")
        for row in rows[1:]:
            if title_col is None or title_col >= len(row):
                continue
            entry = _entry(
                row[title_col],
                row[year_col] if year_col is not None and year_col < len(row) else "",
                row[tmdb_col] if tmdb_col is not None and tmdb_col < len(row) else None,
                row[type_col] if type_col is not None and type_col < len(row) else "",
                row[poster_col] if poster_col is not None and poster_col < len(row) else "",
                row[rating_col] if rating_col is not None and rating_col < len(row) else None,
            )
            if entry:
                entries.append(entry)
    else:
        for row in rows:
            title = row[0] if row else ""
            year = row[1] if len(row) > 1 and str(row[1]).strip()[:4].isdigit() else ""
            entry = _entry(title, year)
            if entry:
                entries.append(entry)
    return entries, name


def parse_text_payload(text: str) -> tuple[list[dict[str, Any]], str]:
    entries = []
    for line in text.splitlines():
        clean = line.strip()
        if not clean or clean.startswith(("#", "//")):
            continue
        clean = re.sub(r"^(?:[-*•]|\d+[.)])\s*", "", clean)
        match = re.match(r"^(.*?)\s*[\(\[]\s*((?:19|20)\d{2})\s*[\)\]]\s*$", clean)
        if match:
            entry = _entry(match.group(1), match.group(2))
        else:
            match = re.match(r"^(.*?)\s*[,;\t|]\s*((?:19|20)\d{2})\s*$", clean)
            if match:
                entry = _entry(match.group(1), match.group(2))
            else:
                entry = _entry(clean)
        if entry:
            entries.append(entry)
    return entries, ""


def _strip_html(value: str) -> str:
    value = re.sub(r"<script.*?</script>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<style.*?</style>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def parse_html_payload(text: str) -> tuple[list[dict[str, Any]], str]:
    entries = []
    name = ""
    title_match = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
    if title_match:
        name = _strip_html(title_match.group(1))[:180]

    for block in re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', text, re.I | re.S):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        candidates = data if isinstance(data, list) else [data]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            elements = candidate.get("itemListElement")
            if not isinstance(elements, list):
                continue
            for element in elements:
                item = element.get("item") if isinstance(element, dict) else None
                if isinstance(item, dict):
                    entry = _entry(item.get("name"), "", None, item.get("@type"))
                    if entry:
                        entries.append(entry)
    if entries:
        return entries, name

    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", text, re.I | re.S):
        cells = [_strip_html(cell) for cell in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.I | re.S)]
        if len(cells) < 2:
            continue
        year = ""
        year_index = None
        for index, cell in enumerate(cells):
            match = re.search(r"\b((?:19|20)\d{2})\b", cell)
            if match:
                year = match.group(1)
                year_index = index
                break
        if not year:
            continue
        title = ""
        for index, cell in enumerate(cells):
            if index == year_index or not cell or cell.isdigit():
                continue
            if len(cell) >= 2:
                title = cell
                break
        entry = _entry(title, year)
        if entry:
            entries.append(entry)
    return entries, name


def parse_payload(text: str, content_type: str, url: str = "") -> tuple[list[dict[str, Any]], str]:
    stripped = text.lstrip()
    if "json" in content_type or stripped.startswith(("{", "[")):
        try:
            return parse_json_payload(text)
        except ValueError:
            pass
    if "html" in content_type or stripped.lower().startswith(("<!doctype", "<html")):
        entries, name = parse_html_payload(text)
        if entries:
            return entries, name
    if "csv" in content_type or "\t" in text[:2000] or ("," in text[:2000] and re.search(r"(?m)^.*,.*(19|20)\d{2}", text[:4000])):
        entries, name = parse_csv_payload(text)
        if entries:
            return entries, name
    return parse_text_payload(text)


# -- catalog matching --------------------------------------------------------


def _normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"\b(the|a|an)\b", " ", text)
    return re.sub(r"[^a-z0-9]+", "", text)


class CatalogIndex:
    def __init__(self, items: list[dict[str, Any]]) -> None:
        self.by_tmdb: dict[str, dict[str, Any]] = {}
        self.by_title_year: dict[tuple[str, str], dict[str, Any]] = {}
        self.by_title: dict[str, list[dict[str, Any]]] = {}
        self.by_initial: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for item in items:
            if not item.get("tmdbId"):
                continue
            key = f"{str(item.get('type') or '').lower()}:{item['tmdbId']}"
            self.by_tmdb[key] = item
            normalized = _normalize(item.get("title"))
            if not normalized:
                continue
            year = str(item.get("year") or "")[:4]
            if year:
                self.by_title_year.setdefault((normalized, year), item)
            self.by_title.setdefault(normalized, []).append(item)
            self.by_initial.setdefault(normalized[0], []).append((normalized, item))

    def match(self, entry: dict[str, Any], fuzzy: bool = True) -> dict[str, Any] | None:
        tmdb_id = entry.get("tmdbId")
        if tmdb_id:
            types = [entry.get("type")] if entry.get("type") in ("movie", "tv") else ["movie", "tv"]
            for media_type in types:
                found = self.by_tmdb.get(f"{media_type}:{tmdb_id}")
                if found:
                    return found
        normalized = _normalize(entry.get("title"))
        if not normalized:
            return None
        year = str(entry.get("year") or "")[:4]
        if year:
            found = self.by_title_year.get((normalized, year))
            if found:
                return found
        titles = self.by_title.get(normalized) or []
        if len(titles) == 1:
            return titles[0]
        if titles and year:
            closest = min(titles, key=lambda item: abs(int(str(item.get("year") or 0)[:4] or 0) - int(year)))
            if closest and abs(int(str(closest.get("year") or 0)[:4] or 0) - int(year)) <= 1:
                return closest
        if fuzzy and len(normalized) >= 5:
            best_item = None
            best_ratio = 0.0
            for candidate, candidate_item in self.by_initial.get(normalized[0], []):
                if abs(len(candidate) - len(normalized)) > 12:
                    continue
                ratio = difflib.SequenceMatcher(None, normalized, candidate).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_item = candidate_item
            if best_item is not None and best_ratio >= 0.9:
                return best_item
        return None


def match_entries(entries: list[dict[str, Any]], catalog_items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    index = CatalogIndex(catalog_items)
    matched = []
    missing = []
    seen = set()
    fuzzy_budget = 25
    for entry in entries[:MAX_ITEMS * 4]:
        local = index.match(entry, fuzzy=fuzzy_budget > 0)
        if local:
            key = f"{local.get('type')}:{local.get('tmdbId')}"
            if key not in seen:
                seen.add(key)
                matched.append(dict(local))
            continue
        if not local:
            normalized = _normalize(entry.get("title"))
            if len(normalized) >= 5 and fuzzy_budget > 0:
                fuzzy_budget -= 1
        if entry.get("tmdbId") and entry.get("type") in ("movie", "tv"):
            key = f"{entry['type']}:{entry['tmdbId']}"
            if key not in seen:
                seen.add(key)
                matched.append(
                    {
                        "type": entry["type"],
                        "tmdbId": entry["tmdbId"],
                        "title": entry["title"],
                        "year": entry.get("year") or "",
                        "poster": entry.get("poster") or "",
                        "backdrop": "",
                        "rating": entry.get("rating"),
                        "popularity": 0,
                        "overview": "",
                        "upstreamOnly": True,
                    }
                )
            continue
        missing.append(entry.get("title") or "")
        if len(missing) >= 200:
            break
    return matched, missing


def import_text_list(
    text: str,
    name: str,
    source: str,
    catalog_items: list[dict[str, Any]],
    library: Any,
) -> dict[str, Any]:
    entries, parsed_name = parse_payload(text, "", source)
    if not entries:
        raise ValueError("No titles could be parsed from this list.")
    matched, missing = match_entries(entries, catalog_items)
    record = library.add_list(
        name or parsed_name or "Imported list",
        source,
        matched,
        len(matched),
        len(missing),
    )
    return {"ok": True, "list": record, "parsed": len(entries), "missing": len(missing), "missingSamples": missing[:12]}


def import_url_list(url: str, catalog_items: list[dict[str, Any]], library: Any) -> dict[str, Any]:
    text, final_url, content_type = fetch_public_text(url)
    entries, parsed_name = parse_payload(text, content_type, final_url)
    if not entries:
        raise ValueError("No titles could be parsed from this URL.")
    matched, missing = match_entries(entries, catalog_items)
    name = parsed_name or urlparse(final_url).path.rsplit("/", 1)[-1] or urlparse(final_url).hostname or "Imported list"
    record = library.add_list(name, final_url, matched, len(matched), len(missing))
    return {"ok": True, "list": record, "parsed": len(entries), "missing": len(missing), "missingSamples": missing[:12], "url": final_url}
