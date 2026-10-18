#!/usr/bin/env python3
"""Bulk-fill the local title metadata cache (genres, age ratings, runtime, recs).

Examples:
  python enrich_metadata.py --limit 500
  python enrich_metadata.py --workers 8
  python enrich_metadata.py --refresh --types tv
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

import metadata
from download_catalog import BROWSER_HEADERS

ROOT = Path(__file__).resolve().parent


def load_items(path: Path, types: set[str]) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    items = []
    seen = set()
    for item in data.get("items") or []:
        media_type = str(item.get("type") or "").lower()
        tmdb_id = item.get("tmdbId")
        if media_type not in types or not tmdb_id:
            continue
        key = metadata.item_key(media_type, tmdb_id)
        if key in seen:
            continue
        seen.add(key)
        items.append(item)
    return items


def main() -> int:
    parser = argparse.ArgumentParser(description="Enrich catalog metadata from the upstream title API.")
    parser.add_argument("--catalog", default=str(ROOT / "catalog.json"))
    parser.add_argument("--base", default=metadata.DEFAULT_BASE)
    parser.add_argument("--types", default="movie,tv")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--limit", type=int, default=0, help="Maximum missing titles to fetch (0 = all)")
    parser.add_argument("--refresh", action="store_true", help="Re-fetch titles that already have metadata")
    parser.add_argument("--include-errors", action="store_true", help="Retry rows previously stored as errors")
    parser.add_argument("--delay", type=float, default=0.0, help="Delay before each request per worker")
    args = parser.parse_args()

    types = {part.strip().lower() for part in args.types.split(",") if part.strip()}
    catalog_path = Path(args.catalog)
    if not catalog_path.exists():
        print(f"catalog not found: {catalog_path}", file=sys.stderr)
        return 2

    items = load_items(catalog_path, types)
    store = metadata.store()
    rows = store.all()
    candidates = []
    for item in items:
        key = metadata.item_key(item.get("type"), item.get("tmdbId"))
        row = rows.get(key)
        if row is not None and not args.refresh:
            if row.get("status") == "ok" or not args.include_errors:
                continue
        candidates.append(item)
    if args.limit:
        candidates = candidates[: args.limit]

    total = len(candidates)
    print(f"metadata: {total} titles to fetch ({len(items)} in catalog, {len(rows)} cached)", flush=True)
    if not total:
        return 0

    lock = threading.Lock()
    done = 0
    ok = 0
    failed = 0
    started = time.time()

    def work(item: dict, session: requests.Session) -> tuple[dict, bool]:
        if args.delay:
            time.sleep(args.delay)
        row = metadata.fetch_and_store(
            session,
            args.base,
            item.get("type"),
            item.get("tmdbId"),
            metadata_store=store,
            timeout=25,
        )
        return item, row is not None

    def make_session() -> requests.Session:
        session = requests.Session()
        session.headers.update(BROWSER_HEADERS)
        return session

    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 16))) as pool:
        futures = {pool.submit(work, item, make_session()): item for item in candidates}
        for future in as_completed(futures):
            try:
                _, success = future.result()
            except Exception:
                success = False
            with lock:
                done += 1
                ok += 1 if success else 0
                failed += 0 if success else 1
                if done % 25 == 0 or done == total:
                    elapsed = max(0.001, time.time() - started)
                    rate = done / elapsed
                    eta = (total - done) / rate if rate else 0
                    print(
                        f"metadata: {done}/{total} ok={ok} failed={failed} "
                        f"rate={rate:.1f}/s eta={eta:.0f}s",
                        flush=True,
                    )
    print(f"metadata: finished ok={ok} failed={failed} in {time.time() - started:.0f}s", flush=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
