#!/usr/bin/env python3
"""Catalog filtering, facet and sort helpers shared by the API and recommender."""

from __future__ import annotations

import math
import re
import time
import unicodedata
from collections import Counter
from typing import Any, Iterable

CURRENT_YEAR = time.gmtime().tm_year


def normalize_title(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"\b(the|a|an)\b", " ", text)
    return re.sub(r"[^a-z0-9]+", "", text)


def item_key(item: dict[str, Any]) -> str:
    return f"{str(item.get('type') or '').lower()}:{item.get('tmdbId')}"


def item_meta(item: dict[str, Any], meta_map: dict[str, dict[str, Any]]) -> dict[str, Any]:
    row = meta_map.get(item_key(item))
    fallback = item.get("_fallbackGenres") or []
    if row and row.get("status") == "ok":
        genres = row.get("genres") or []
        if not genres and fallback:
            return {**row, "genres": list(fallback)}
        return row
    return {
        "genres": list(fallback),
        "minAge": None,
        "ageBand": "unknown",
        "kidsSafe": False,
        "familySafe": False,
        "adult": False,
        "runtime": None,
        "language": "",
        "certification": "",
        "recommendations": [],
        "known": False,
    }


def is_adult(meta: dict[str, Any], item: dict[str, Any]) -> bool:
    if meta.get("adult") or item.get("_adultHint"):
        return True
    genres = {str(genre).lower() for genre in meta.get("genres") or []}
    if "adult" in genres or "erotic" in genres:
        return True
    cert = str(meta.get("certification") or "").upper()
    return cert in {"A", "R", "NC-17", "TV-MA", "X", "18+", "R18+"}


def matches(item: dict[str, Any], meta: dict[str, Any], filters: dict[str, Any]) -> bool:
    media_type = str(item.get("type") or "").lower()
    wanted_type = str(filters.get("type") or "").lower()
    if wanted_type and media_type != wanted_type:
        return False

    query = str(filters.get("q") or "").strip().lower()
    if query:
        search = f"{item.get('title') or ''} {item.get('year') or ''}".lower()
        if query not in search:
            return False

    genres = {str(genre).lower() for genre in meta.get("genres") or []}
    wanted_genre = str(filters.get("genre") or "").strip().lower()
    if wanted_genre and wanted_genre not in genres:
        return False

    try:
        rating = float(item.get("rating") or 0)
    except (TypeError, ValueError):
        rating = 0.0
    min_rating = filters.get("minRating")
    if min_rating is not None and rating < float(min_rating):
        return False

    try:
        year = int(str(item.get("year") or "")[:4])
    except ValueError:
        year = 0

    year_from = filters.get("yearFrom")
    if year_from is not None and year < int(year_from):
        return False
    year_to = filters.get("yearTo")
    if year_to is not None and year > int(year_to):
        return False
    decade = filters.get("decade")
    if decade is not None and not (int(decade) <= year <= int(decade) + 9):
        return False

    max_runtime = filters.get("maxRuntime")
    if max_runtime is not None:
        runtime = meta.get("runtime")
        if not runtime or int(runtime) > int(max_runtime):
            return False

    kids = bool(filters.get("kids"))
    age = str(filters.get("age") or "").strip().lower()
    max_age = filters.get("maxAge")
    min_age = meta.get("minAge")
    adult = is_adult(meta, item)

    if kids:
        if not meta.get("kidsSafe") or adult:
            return False
        return True

    if max_age is not None:
        if min_age is None or int(min_age) > int(max_age) or adult:
            return False

    if age in ("kids", "family", "teen", "adult"):
        if age == "kids" and not meta.get("kidsSafe"):
            return False
        if age == "family" and (not meta.get("familySafe") or (min_age is not None and min_age > 12)):
            return False
        if age == "teen" and (min_age is None or min_age > 15 or adult):
            return False
        if age == "adult" and not adult:
            return False

    excluded = filters.get("excludeKeys")
    if excluded and item_key(item) in excluded:
        return False

    return True


def filter_items(
    items: Iterable[dict[str, Any]],
    meta_map: dict[str, dict[str, Any]],
    filters: dict[str, Any],
) -> list[dict[str, Any]]:
    results = []
    for item in items:
        meta = item_meta(item, meta_map)
        if matches(item, meta, filters):
            results.append(item)
    return results


def sort_items(
    items: list[dict[str, Any]],
    meta_map: dict[str, dict[str, Any]],
    sort: str = "popular",
    query: str = "",
) -> list[dict[str, Any]]:
    sort = (sort or "popular").lower()

    def popularity(item):
        try:
            return float(item.get("popularity") or 0)
        except (TypeError, ValueError):
            return 0.0

    def rating(item):
        try:
            return float(item.get("rating") or 0)
        except (TypeError, ValueError):
            return 0.0

    def year(item):
        try:
            return int(str(item.get("year") or "")[:4])
        except ValueError:
            return 0

    if sort == "rating":
        return sorted(items, key=lambda item: (rating(item), popularity(item)), reverse=True)
    if sort == "newest":
        return sorted(items, key=lambda item: (year(item), rating(item)), reverse=True)
    if sort == "oldest":
        return sorted(items, key=lambda item: (year(item) or 9999, rating(item)))
    if sort == "az":
        return sorted(items, key=lambda item: str(item.get("title") or "").lower())
    if sort == "hidden":
        candidates = [item for item in items if rating(item) >= 6.4]
        candidates.sort(key=lambda item: (rating(item) * 2 - math.log1p(popularity(item))), reverse=True)
        return candidates
    if query:
        needle = query.strip().lower()

        def relevance(item):
            title = str(item.get("title") or "").lower()
            if title == needle:
                return 3
            if title.startswith(needle):
                return 2
            return 1

        return sorted(items, key=lambda item: (-relevance(item), -popularity(item)))

    return sorted(items, key=lambda item: (popularity(item), rating(item)), reverse=True)


def facets(items: Iterable[dict[str, Any]], meta_map: dict[str, dict[str, Any]]) -> dict[str, Any]:
    genres = Counter()
    decades = Counter()
    age = Counter({"kids": 0, "family": 0, "teen": 0, "adult": 0, "unknown": 0})
    languages = Counter()
    rated = 0
    known = 0
    for item in items:
        meta = item_meta(item, meta_map)
        if meta.get("known") is False:
            age["unknown"] += 1
            continue
        known += 1
        for genre in meta.get("genres") or []:
            genres[str(genre).strip().title()] += 1
        try:
            year = int(str(item.get("year") or "")[:4])
            decades[(year // 10) * 10] += 1
        except ValueError:
            pass
        if meta.get("minAge") is not None:
            rated += 1
            band = meta.get("ageBand") or "unknown"
            age[band if band in age else "unknown"] += 1
        else:
            age["unknown"] += 1
        if meta.get("language"):
            languages[meta["language"]] += 1
    return {
        "genres": [{"name": name, "count": count} for name, count in genres.most_common(40)],
        "decades": [{"decade": decade, "count": count} for decade, count in sorted(decades.items(), reverse=True)],
        "age": dict(age),
        "languages": [{"code": code, "count": count} for code, count in languages.most_common(12)],
        "coverage": {"known": known, "rated": rated, "total": known + age["unknown"]},
    }


def parse_filters(args: Any) -> dict[str, Any]:
    def int_arg(name: str, minimum: int | None = None, maximum: int | None = None):
        raw = args.get(name)
        if raw in (None, "", "any"):
            return None
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None
        if minimum is not None:
            value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return value

    def float_arg(name: str):
        raw = args.get(name)
        if raw in (None, "", "any"):
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    return {
        "q": (args.get("q") or "").strip(),
        "type": (args.get("type") or "").strip().lower(),
        "genre": (args.get("genre") or "").strip(),
        "age": (args.get("age") or "").strip().lower(),
        "kids": str(args.get("kids") or "").lower() in ("1", "true", "yes", "on"),
        "maxAge": int_arg("maxAge", 0, 21),
        "minRating": float_arg("minRating"),
        "yearFrom": int_arg("yearFrom", 1880, 2100),
        "yearTo": int_arg("yearTo", 1880, 2100),
        "decade": int_arg("decade", 1880, 2100),
        "maxRuntime": int_arg("maxRuntime", 1, 600),
        "sort": (args.get("sort") or "popular").strip().lower(),
    }
