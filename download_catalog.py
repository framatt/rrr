#!/usr/bin/env python3
"""Download the NetMirror catalog as JSON.

Examples:
  python download_catalog.py
  python download_catalog.py --home-only
  python download_catalog.py --types movie --max-pages 5
  python download_catalog.py --mobile-home
  python download_catalog.py --search "jailer"
  python download_catalog.py --title movie 1368337
  python download_catalog.py --check
"""

import argparse
import base64
import json
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

CONFIG_HOSTS = [
    "mobidetect.art",
    "mobidetect.cc",
    "mobidetects.art",
    "mobidetects.cc",
    "mobidetect.click",
    "mobidetect.pro",
    "mobidetect.live",
    "mobidetect.site",
    "mobidetect.store",
    "mobidetect.xyz",
]

KNOWN_BASES = [
    "https://net79.cc",
    "https://netmirrors.vip",
    "https://net77.cc",
    "https://netmirror.gg",
    "https://net52.cc",
]

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

APP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 13; Pixel 5 Build/TQ3A.230901.001; wv) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
        "Chrome/144.0.7559.132 Safari/537.36 /OS.Gatu v3.1.0.0.43"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
    "X-Requested-With": "XMLHttpRequest",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Dest": "document",
}

HERO_PLATFORMS = [None, "Netflix", "PrimeVideo", "JioHotstar", "SonyLIV", "Crunchyroll", "MX"]

_thread_local = threading.local()
_print_lock = threading.Lock()
_config_cache = None
_cooldown_lock = threading.Lock()
_cooldown_until = 0.0


class FetchError(RuntimeError):
    pass


class ChallengeError(FetchError):
    pass


def log(message):
    with _print_lock:
        print(message, file=sys.stderr, flush=True)


def session():
    value = getattr(_thread_local, "session", None)
    if value is None:
        value = requests.Session()
        value.headers.update(BROWSER_HEADERS)
        _thread_local.session = value
    return value


def decode_token(value):
    if not value:
        return None
    value = value.strip()
    value += "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value).decode("utf-8", "replace")
    except Exception:
        try:
            return base64.b64decode(value).decode("utf-8", "replace")
        except Exception:
            return None


def is_challenge(response):
    text = response.text[:4000]
    return (
        response.status_code in (403, 503)
        and (
            "Just a moment" in text
            or "cf-chl" in text
            or "challenge-platform" in text
            or "Performing security verification" in text
        )
    )


def wait_for_cooldown():
    while True:
        with _cooldown_lock:
            remaining = _cooldown_until - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 2.0))


def set_cooldown(seconds):
    global _cooldown_until
    with _cooldown_lock:
        _cooldown_until = max(_cooldown_until, time.monotonic() + seconds)


def fetch_json(url, params=None, timeout=60, retries=6):
    last_error = None
    for attempt in range(retries):
        wait_for_cooldown()
        try:
            response = session().get(url, params=params, timeout=(15, timeout))
        except requests.RequestException as exc:
            last_error = exc
            if attempt + 1 < retries:
                set_cooldown(2.0)
                time.sleep(min(8.0, 0.75 * (2 ** attempt)) + random.random() * 0.3)
                continue
            raise FetchError(f"{url}: {exc}") from exc

        if is_challenge(response):
            raise ChallengeError(
                f"{url}: Cloudflare challenge ({response.status_code}); "
                "use a browser-cleared session or the live API domain"
            )

        if response.status_code == 200:
            content_type = response.headers.get("content-type", "")
            body = response.text.lstrip()
            if "json" in content_type or body.startswith(("{", "[")):
                try:
                    return response.json()
                except ValueError as exc:
                    raise FetchError(f"{url}: invalid JSON: {exc}") from exc
            raise FetchError(f"{url}: expected JSON, got {content_type or 'unknown content type'}")

        if response.status_code in (429, 500, 502, 503, 504) and attempt + 1 < retries:
            retry_after = response.headers.get("retry-after")
            cooldown = float(retry_after) if retry_after and retry_after.isdigit() else min(30.0, 5.0 * (2 ** attempt))
            set_cooldown(cooldown)
            time.sleep(min(30.0, cooldown) + random.random() * 0.5)
            last_error = FetchError(f"{url}: HTTP {response.status_code}")
            continue

        raise FetchError(f"{url}: HTTP {response.status_code}")

    raise FetchError(f"{url}: {last_error}")


def redirect_origins(url, limit=4):
    origins = []
    current = url
    for _ in range(limit):
        try:
            response = requests.get(current, headers=BROWSER_HEADERS, allow_redirects=False, timeout=10)
        except requests.RequestException:
            break
        location = response.headers.get("location")
        if not location:
            break
        current = urljoin(current, location)
        origin = f"{urlparse(current).scheme}://{urlparse(current).netloc}"
        if origin not in origins:
            origins.append(origin)
    return origins


def resolve_config_urls():
    global _config_cache
    if _config_cache is not None:
        return list(_config_cache)
    urls = []
    for host in CONFIG_HOSTS:
        for path in ("/check.php", "/checknewtv.php"):
            try:
                response = requests.get(
                    f"https://{host}{path}",
                    headers=BROWSER_HEADERS,
                    timeout=8,
                )
            except requests.RequestException:
                continue
            if response.status_code != 200:
                continue
            try:
                data = response.json()
            except ValueError:
                continue
            for key in ("token_hash", "stape"):
                decoded = decode_token(data.get(key))
                if decoded and decoded.startswith(("http://", "https://")) and decoded not in urls:
                    urls.append(decoded)
            if urls:
                _config_cache = urls
                return list(urls)
    _config_cache = urls
    return list(urls)


def fetch_bootstrap():
    for host in CONFIG_HOSTS:
        try:
            response = requests.get(f"https://{host}/check.php", headers=BROWSER_HEADERS, timeout=8)
        except requests.RequestException:
            continue
        if response.status_code != 200:
            continue
        try:
            raw = response.json()
        except ValueError:
            continue
        return {
            "host": host,
            "token_url": decode_token(raw.get("token_hash")),
            "stream_url": decode_token(raw.get("stape")),
            "stream_hosts": [decode_token(value) for value in raw.get("u", []) if isinstance(value, str)],
            "raw": raw,
        }
    return None


def candidate_bases(explicit):
    candidates = []
    if explicit:
        candidates.append(explicit.rstrip("/"))
    env_base = os.environ.get("NETMIRROR_BASE")
    if env_base:
        candidates.append(env_base.rstrip("/"))

    for url in resolve_config_urls():
        parsed = urlparse(url)
        if parsed.scheme and parsed.netloc:
            candidates.append(f"{parsed.scheme}://{parsed.netloc}")

    for origin in redirect_origins("https://net52.cc/"):
        candidates.append(origin)

    candidates.extend(KNOWN_BASES)

    unique = []
    for value in candidates:
        value = value.rstrip("/")
        if value and value not in unique:
            unique.append(value)
    return unique


def pick_base(explicit=None):
    for base in candidate_bases(explicit):
        try:
            data = fetch_json(f"{base}/api/catalog/hero", timeout=12, retries=1)
        except FetchError:
            continue
        if isinstance(data, dict) and data.get("ok") and isinstance(data.get("items"), list):
            return base
    raise FetchError("no working catalog API found")


def fetch_mobile_home(base, output_path):
    targets = []
    for value in [f"{base}/mobile/home?app=1", *resolve_config_urls(), "https://net52.cc/mobile/home?app=1", "https://net77.cc/mobile/home?app=1"]:
        if "/mobile/" not in value:
            continue
        if value not in targets:
            targets.append(value)

    for target in targets:
        log(f"legacy mobile endpoint: {target}")
        try:
            response = requests.get(target, headers=APP_HEADERS, timeout=(15, 30), allow_redirects=True)
        except requests.RequestException as exc:
            log(f"legacy mobile endpoint failed: {exc}")
            continue

        log(f"legacy mobile endpoint: HTTP {response.status_code}, {len(response.content)} bytes")
        content_type = response.headers.get("content-type", "")
        body = response.text.lstrip()
        if "json" in content_type or body.startswith(("{", "[")):
            path = output_path.with_name(output_path.stem + ".mobile_home.json")
            path.write_text(response.text, encoding="utf-8")
            log(f"saved {path}")
            try:
                return response.json()
            except ValueError:
                return None

        if response.status_code == 404:
            continue

        path = output_path.with_name(output_path.stem + ".mobile_home.html")
        path.write_text(response.text, encoding="utf-8")
        log(f"saved {path}")

        match = re.search(r'data-addhash="([^"]+)"', response.text)
        if match:
            log(
                "legacy endpoint is ad-gated; verification hash "
                f"{match.group(1)} requires the interactive ad flow. "
                "Falling back to /api/catalog/*."
            )
        elif "Site Direct Access Not Allowed" in response.text:
            log("legacy endpoint rejected direct access; falling back to /api/catalog/*.")
        return None

    log("legacy mobile endpoint unavailable; using /api/catalog/* only.")
    return None


def normalized_title(value):
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def item_keys(item):
    media_type = str(item.get("type") or "").lower()
    keys = []
    tmdb_id = item.get("tmdbId")
    subject_id = item.get("subjectId")
    if tmdb_id:
        keys.append(f"{media_type}:tmdb:{tmdb_id}")
    if subject_id:
        keys.append(f"{media_type}:subject:{subject_id}")
    if not tmdb_id:
        title = normalized_title(item.get("title"))
        if title:
            year = str(item.get("year") or "")
            keys.append(f"{media_type}:title:{title}:{year}")
    return keys


def merge_item(existing, incoming):
    for key, value in incoming.items():
        if value in (None, "", 0, [], {}):
            continue
        if existing.get(key) in (None, "", 0, [], {}):
            existing[key] = value


def title_key(item):
    title = normalized_title(item.get("title"))
    if not title:
        return None
    media_type = str(item.get("type") or "").lower()
    year = str(item.get("year") or "")
    return f"{media_type}:title:{title}:{year}"


def add_items(store, index, items, source):
    added = 0
    for item in items or []:
        if not isinstance(item, dict):
            continue
        keys = item_keys(item)
        if not keys:
            continue
        existing = next((index[key] for key in keys if key in index), None)
        if existing is None and item.get("tmdbId"):
            fallback = title_key(item)
            candidate = index.get(fallback) if fallback else None
            if candidate is not None and not candidate.get("tmdbId"):
                existing = candidate
                keys.append(fallback)
        if existing is not None:
            merge_item(existing, item)
            for key in keys:
                index.setdefault(key, existing)
            continue
        copied = dict(item)
        store.append(copied)
        for key in keys:
            index[key] = copied
        added += 1
    return added


def fetch_home(base, region):
    hero = []
    trending = {}
    rails = []
    sources = []

    for platform in HERO_PLATFORMS:
        params = {"platform": platform} if platform else None
        try:
            data = fetch_json(f"{base}/api/catalog/hero", params=params)
        except FetchError as exc:
            log(f"hero {platform or 'default'} failed: {exc}")
            continue
        items = data.get("items") or []
        hero.extend(items)
        label = platform or "default"
        sources.append(f"hero:{label}:{len(items)}")
        log(f"hero {label}: {len(items)} items")

    for window in ("day", "week"):
        try:
            data = fetch_json(f"{base}/api/catalog/trending", params={"window": window, "page": 1})
        except FetchError as exc:
            log(f"trending {window} failed: {exc}")
            continue
        trending[window] = data.get("items") or []
        sources.append(f"trending:{window}:{len(trending[window])}")
        log(f"trending {window}: {len(trending[window])} items")

    try:
        data = fetch_json(f"{base}/api/catalog/aoneroom-home")
        rails = data.get("rails") or []
        sources.append(f"aoneroom-home:{len(rails)} rails")
        log(f"aoneroom-home: {len(rails)} rails")
    except FetchError as exc:
        log(f"aoneroom-home failed: {exc}")

    return hero, trending, rails, sources


def discover_page(base, media_type, region, page, cache_dir):
    if cache_dir:
        cache_path = Path(cache_dir) / media_type / f"page_{page}.json"
        if cache_path.exists():
            try:
                return json.loads(cache_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                pass

    params = {"type": media_type, "sort": "popularity", "page": page}
    if region:
        params["region"] = region
    data = fetch_json(f"{base}/api/catalog/discover", params=params)

    if cache_dir:
        cache_path = Path(cache_dir) / media_type / f"page_{page}.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def crawl_type(base, media_type, region, max_pages, workers, delay, cache_dir, strict):
    first = discover_page(base, media_type, region, 1, cache_dir)
    total_pages = int(first.get("totalPages") or 1)
    total_results = int(first.get("totalResults") or 0)
    limit = total_pages if not max_pages else min(total_pages, max_pages)
    log(f"{media_type}: {total_results} results, {total_pages} pages, fetching {limit}")

    pages = {1: first}
    errors = []

    def work(page):
        if delay:
            time.sleep(delay)
        return page, discover_page(base, media_type, region, page, cache_dir)

    if limit > 1:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(work, page): page for page in range(2, limit + 1)}
            completed = 1
            for future in as_completed(futures):
                page = futures[future]
                try:
                    page_number, data = future.result()
                    pages[page_number] = data
                except FetchError as exc:
                    errors.append(f"{media_type} page {page}: {exc}")
                    if strict:
                        for pending in futures:
                            pending.cancel()
                        raise
                completed += 1
                if completed % 25 == 0 or completed == limit:
                    log(f"{media_type}: {completed}/{limit} pages")

    items = []
    for page in range(1, limit + 1):
        data = pages.get(page)
        if data:
            items.extend(data.get("items") or [])

    if errors:
        log(f"{media_type}: {len(errors)} page failures")
        for error in errors[:10]:
            log(f"  {error}")

    return items, total_pages, total_results, errors


def build_catalog(base, region, types, max_pages, workers, delay, cache_dir, home_only, strict):
    hero, trending, rails, sources = fetch_home(base, region)

    catalog = []
    index = {}
    add_items(catalog, index, hero, "hero")
    for window in ("day", "week"):
        add_items(catalog, index, trending.get(window), f"trending:{window}")
    for rail in rails:
        add_items(catalog, index, rail.get("items"), f"rail:{rail.get('title')}")

    stats = {}
    errors = []
    if not home_only:
        for media_type in types:
            items, total_pages, total_results, page_errors = crawl_type(
                base,
                media_type,
                region,
                max_pages,
                workers,
                delay,
                cache_dir,
                strict,
            )
            added = add_items(catalog, index, items, f"discover:{media_type}")
            stats[media_type] = {
                "api_total_pages": total_pages,
                "api_total_results": total_results,
                "fetched_pages": total_pages if not max_pages else min(total_pages, max_pages),
                "unique_added": added,
            }
            sources.append(f"discover:{media_type}:{added} unique")
            errors.extend(page_errors)

    by_type = {}
    for item in catalog:
        key = str(item.get("type") or "unknown")
        by_type[key] = by_type.get(key, 0) + 1

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base": base,
        "region": region,
        "types": types,
        "home_only": home_only,
        "counts": {
            "items": len(catalog),
            "by_type": by_type,
            "hero": len(hero),
            "rails": len(rails),
            "trending": {key: len(value) for key, value in trending.items()},
        },
        "discover": stats,
        "hero": hero,
        "trending": trending,
        "rails": rails,
        "items": catalog,
        "sources": sources,
        "errors": errors,
        "playback": {
            "resolver_url": f"{base}/api/embed-tmdb/{{tmdbId}}?type={{type}}&se={{season}}&ep={{episode}}",
            "title_url": f"{base}/api/catalog/title/{{type}}/{{tmdbId}}",
            "season_url": f"{base}/api/catalog/season/{{tmdbId}}/{{season}}",
            "notes": "Signed stream URLs expire; resolve at click time. TV playback uses season and episode.",
            "expiry_field": "exp",
            "signed_url_fields": ["mp4", "streams[].url"],
            "hls_fallback_field": "fallbackHls",
        },
    }


def enrich_details(base, items, workers, limit):
    candidates = [
        item
        for item in items
        if item.get("tmdbId") and str(item.get("type") or "").lower() in ("movie", "tv")
    ]
    if limit:
        candidates = candidates[:limit]
    if not candidates:
        log("details: no titles with tmdbId to resolve")
        return 0, 0

    log(f"details: resolving {len(candidates)} titles")
    resolved = 0
    failed = 0

    def work(item):
        try:
            data = fetch_json(
                f"{base}/api/catalog/title/{item['type']}/{item['tmdbId']}",
                timeout=45,
                retries=4,
            )
            return item, data, None
        except FetchError as exc:
            return item, None, str(exc)

    with ThreadPoolExecutor(max_workers=max(1, min(workers, 4))) as pool:
        futures = [pool.submit(work, item) for item in candidates]
        for index, future in enumerate(as_completed(futures), 1):
            item, data, error = future.result()
            if error or not data or not data.get("ok"):
                item["details_error"] = error or (data.get("error") if data else "no details data")
                failed += 1
            else:
                item["details"] = data
                resolved += 1
            if index % 25 == 0 or index == len(candidates):
                log(f"details: {index}/{len(candidates)} (resolved {resolved}, failed {failed})")

    return resolved, failed


def enrich_streams(base, items, workers, limit, season, episode):
    candidates = [
        item
        for item in items
        if item.get("tmdbId") and str(item.get("type") or "").lower() in ("movie", "tv")
    ]
    if limit:
        candidates = candidates[:limit]
    if not candidates:
        log("streams: no titles with tmdbId to resolve")
        return 0, 0

    log(f"streams: resolving {len(candidates)} titles (season={season}, episode={episode})")
    resolved = 0
    failed = 0

    def work(item):
        try:
            data = fetch_json(
                f"{base}/api/embed-tmdb/{item['tmdbId']}",
                params={"type": item["type"], "se": season, "ep": episode},
                timeout=45,
                retries=4,
            )
            return item, data, None
        except FetchError as exc:
            return item, None, str(exc)

    with ThreadPoolExecutor(max_workers=max(1, min(workers, 4))) as pool:
        futures = [pool.submit(work, item) for item in candidates]
        for index, future in enumerate(as_completed(futures), 1):
            item, data, error = future.result()
            if error or not data or not data.get("ok"):
                item["stream_error"] = error or (data.get("error") if data else "no stream data")
                failed += 1
            else:
                item["stream"] = data
                resolved += 1
            if index % 25 == 0 or index == len(candidates):
                log(f"streams: {index}/{len(candidates)} (resolved {resolved}, failed {failed})")

    return resolved, failed


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)
    return path


def command_check(base):
    log(f"base: {base}")
    checks = [
        ("hero", "/api/catalog/hero", None),
        ("trending day", "/api/catalog/trending", {"window": "day"}),
        ("trending week", "/api/catalog/trending", {"window": "week"}),
        ("rails", "/api/catalog/aoneroom-home", None),
        ("discover movie", "/api/catalog/discover", {"type": "movie", "region": "IN", "page": 1}),
        ("discover tv", "/api/catalog/discover", {"type": "tv", "region": "IN", "page": 1}),
    ]
    for label, path, params in checks:
        try:
            data = fetch_json(f"{base}{path}", params=params, timeout=30, retries=1)
        except FetchError as exc:
            print(f"{label:16} FAIL  {exc}")
            continue
        if "items" in data:
            print(
                f"{label:16} OK    items={len(data.get('items') or [])} "
                f"pages={data.get('totalPages', '-')} results={data.get('totalResults', '-')}"
            )
        elif "rails" in data:
            print(f"{label:16} OK    rails={len(data.get('rails') or [])}")
        else:
            print(f"{label:16} OK    {list(data)[:6]}")


def command_search(base, query):
    data = fetch_json(f"{base}/api/catalog/search-hybrid", params={"q": query}, timeout=60)
    items = data.get("items") or []
    print(json.dumps({"query": query, "base": base, "count": len(items), "items": items}, ensure_ascii=False, indent=2))
    return data


def command_title(base, media_type, tmdb_id):
    data = fetch_json(f"{base}/api/catalog/title/{media_type}/{tmdb_id}", timeout=45)
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return data


def parse_args():
    parser = argparse.ArgumentParser(description="Download the NetMirror catalog as JSON.")
    parser.add_argument("--base", help="Catalog site origin (auto-detected by default)")
    parser.add_argument("--out", default="catalog.json", help="Output JSON path (default: catalog.json)")
    parser.add_argument("--region", default="IN", help="Discover region (default: IN; empty for global)")
    parser.add_argument("--types", default="movie,tv", help="Comma-separated media types (default: movie,tv)")
    parser.add_argument("--max-pages", type=int, default=0, help="Maximum discover pages per type (0 = all)")
    parser.add_argument("--workers", type=int, default=4, help="Concurrent page requests (default: 4)")
    parser.add_argument("--delay", type=float, default=0.15, help="Delay per request in seconds (default: 0.15)")
    parser.add_argument("--home-only", action="store_true", help="Only fetch hero, trending page 1 and home rails")
    parser.add_argument("--mobile-home", action="store_true", help="Also fetch the legacy /mobile/home?app=1 endpoint")
    parser.add_argument("--cache-dir", help="Cache raw discover pages here and reuse them on rerun")
    parser.add_argument("--strict", action="store_true", help="Abort on the first page failure")
    parser.add_argument("--streams", action="store_true", help="Resolve signed video URLs for each catalog title")
    parser.add_argument("--streams-limit", type=int, default=0, help="Maximum titles to resolve (0 = all)")
    parser.add_argument("--streams-workers", type=int, default=2, help="Concurrent stream requests (default: 2)")
    parser.add_argument("--season", type=int, default=1, help="TV season for stream resolution (default: 1)")
    parser.add_argument("--episode", type=int, default=1, help="TV episode for stream resolution (default: 1)")
    parser.add_argument("--details", action="store_true", help="Attach title details and TV seasons/episodes")
    parser.add_argument("--details-limit", type=int, default=0, help="Maximum titles to resolve (0 = all)")
    parser.add_argument("--details-workers", type=int, default=2, help="Concurrent detail requests (default: 2)")
    parser.add_argument("--check", action="store_true", help="Resolve the base and test catalog endpoints")
    parser.add_argument("--bootstrap", action="store_true", help="Print the mobidetect bootstrap config as JSON")
    parser.add_argument("--search", metavar="QUERY", help="Search the catalog and print JSON")
    parser.add_argument("--title", nargs=2, metavar=("TYPE", "ID"), help="Fetch one title and print JSON")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.bootstrap:
        data = fetch_bootstrap()
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    base = pick_base(args.base)
    log(f"using base: {base}")

    if args.check:
        command_check(base)
        return 0
    if args.search:
        command_search(base, args.search)
        return 0
    if args.title:
        command_title(base, args.title[0], args.title[1])
        return 0

    output_path = Path(args.out)
    if args.mobile_home:
        fetch_mobile_home(base, output_path)

    types = [value.strip().lower() for value in args.types.split(",") if value.strip()]
    region = args.region or None
    log(
        f"catalog: types={','.join(types)} region={region or 'global'} "
        f"home_only={args.home_only} max_pages={args.max_pages or 'all'}"
    )
    catalog = build_catalog(
        base,
        region,
        types,
        args.max_pages,
        args.workers,
        args.delay,
        args.cache_dir,
        args.home_only,
        args.strict,
    )
    if args.details:
        resolved, failed = enrich_details(
            base,
            catalog["items"],
            args.details_workers,
            args.details_limit,
        )
        catalog["counts"]["details_resolved"] = resolved
        catalog["counts"]["details_failed"] = failed
    if args.streams:
        resolved, failed = enrich_streams(
            base,
            catalog["items"],
            args.streams_workers,
            args.streams_limit,
            args.season,
            args.episode,
        )
        catalog["counts"]["streams_resolved"] = resolved
        catalog["counts"]["streams_failed"] = failed
    path = write_json(output_path, catalog)
    log(
        f"wrote {path}: {catalog['counts']['items']} unique items "
        f"({json.dumps(catalog['counts']['by_type'], ensure_ascii=False)})"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("interrupted")
        raise SystemExit(130)
    except FetchError as exc:
        log(f"error: {exc}")
        raise SystemExit(1)
