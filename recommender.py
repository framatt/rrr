#!/usr/bin/env python3
"""Transparent, local recommendation scoring.

No external recommendation service is involved.  Signals come from the
watchlist, likes/dislikes, watched markers, ratings, playback progress and
opening history.  Genre affinity and content similarity do most of the work,
with rating/popularity/recency as tie-breakers and an upstream "recommended
because of" list as a bonus.  Every returned row carries a short reason so the
UI can explain why a title appeared.
"""

from __future__ import annotations

import hashlib
import math
import time
from collections import Counter, defaultdict
from typing import Any, Iterable

import discovery

CURRENT_YEAR = time.gmtime().tm_year


def _hash01(value: str) -> float:
    digest = hashlib.sha256(value.encode("utf-8", "replace")).digest()
    return int.from_bytes(digest[:4], "big") / 0xFFFFFFFF


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _genres(meta: dict[str, Any]) -> set[str]:
    return {str(genre).lower() for genre in meta.get("genres") or [] if genre}


def _rating(item: dict[str, Any]) -> float:
    try:
        return float(item.get("rating") or 0)
    except (TypeError, ValueError):
        return 0.0


def _popularity(item: dict[str, Any]) -> float:
    try:
        return float(item.get("popularity") or 0)
    except (TypeError, ValueError):
        return 0.0


def _year(item: dict[str, Any]) -> int:
    try:
        return int(str(item.get("year") or "")[:4])
    except ValueError:
        return 0


def _seed_verb(sources: set[str]) -> str:
    if "watched" in sources:
        return "watched"
    if "like" in sources:
        return "liked"
    return "saved"


def _seed_weight(entry: dict[str, Any], kind: str, snapshot: dict[str, Any], key: str) -> float:
    if kind == "like":
        return 4.0
    if kind == "watchlist":
        return 2.2
    if kind == "watched":
        weight = 1.6
        rating = (snapshot.get("watched", {}).get(key) or {}).get("rating")
        if rating:
            weight += float(rating) * 0.55
        return weight
    if kind == "progress":
        return 1.4
    if kind == "history":
        return 0.5
    return 0.0


def build_profile(
    snapshot: dict[str, Any],
    items_by_key: dict[str, dict[str, Any]],
    meta_map: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    combined: dict[str, float] = defaultdict(float)
    sources: dict[str, set[str]] = defaultdict(set)

    for key in (snapshot.get("watchlist") or {}):
        combined[key] += 2.2
        sources[key].add("watchlist")
    for key, reaction in (snapshot.get("reactions") or {}).items():
        value = int(reaction.get("value") or 0)
        if value > 0:
            combined[key] += 4.0
            sources[key].add("like")
        elif value < 0:
            combined[key] -= 4.0
            sources[key].add("dislike")
    for key in (snapshot.get("watched") or {}):
        rating = ((snapshot.get("watched") or {}).get(key) or {}).get("rating")
        combined[key] += 1.6 + (float(rating) * 0.55 if rating else 0)
        sources[key].add("watched")
    for entry in (snapshot.get("progress") or {}).values():
        key = entry.get("key")
        if key:
            combined[key] += 1.4
            sources[key].add("progress")
    for entry in (snapshot.get("history") or [])[:60]:
        key = entry.get("key")
        if key:
            combined[key] += 0.5
            sources[key].add("history")

    genre_weights: Counter[str] = Counter()
    negative_genres: Counter[str] = Counter()
    type_weights: Counter[str] = Counter()
    language_weights: Counter[str] = Counter()
    era_weights: Counter[str] = Counter()
    seeds: list[dict[str, Any]] = []
    rec_weights: dict[str, float] = defaultdict(float)
    rec_source: dict[str, tuple[str, str]] = {}
    disliked_genres: set[str] = set()

    for key, weight in sorted(combined.items(), key=lambda pair: pair[1], reverse=True):
        item = items_by_key.get(key)
        if not item:
            continue
        meta = discovery.item_meta(item, meta_map)
        genres = _genres(meta)
        if weight < 0:
            for genre in genres:
                negative_genres[genre] += abs(weight)
                disliked_genres.add(genre)
            continue
        weight = min(weight, 8.0)
        for genre in genres:
            genre_weights[genre] += weight
        type_weights[str(item.get("type") or "")] += weight
        if meta.get("language"):
            language_weights[str(meta["language"])] += weight * 0.5
        year = _year(item)
        if year:
            era_weights[str((year // 10) * 10)] += weight * 0.25
        if genres:
            seeds.append(
                {
                    "key": key,
                    "item": item,
                    "meta": meta,
                    "genres": genres,
                    "weight": weight,
                    "sources": sources[key],
                }
            )
        for rec in meta.get("recommendations") or []:
            rec_key = discovery.item_key(rec)
            rec_weights[rec_key] += weight * 1.5
            rec_source.setdefault(rec_key, (item.get("title") or "a title you liked", _seed_verb(sources[key])))

    for genre, penalty in negative_genres.items():
        genre_weights[genre] = max(0, genre_weights.get(genre, 0) - penalty)

    seeds.sort(key=lambda seed: seed["weight"], reverse=True)
    total_genre_weight = sum(genre_weights.values()) or 1
    return {
        "signals": len(combined),
        "genre_weights": genre_weights,
        "normalized_genres": {genre: value / total_genre_weight for genre, value in genre_weights.items()},
        "negative_genres": negative_genres,
        "type_weights": type_weights,
        "language_weights": language_weights,
        "era_weights": era_weights,
        "seeds": seeds[:12],
        "rec_weights": dict(rec_weights),
        "rec_source": rec_source,
        "top_genres": [genre for genre, _ in genre_weights.most_common(4) if genre not in disliked_genres],
        "has_signals": bool(seeds),
    }


def score_item(
    item: dict[str, Any],
    meta: dict[str, Any],
    profile: dict[str, Any],
    day_seed: str,
) -> tuple[float, str]:
    genres = _genres(meta)
    key = discovery.item_key(item)
    genre_score = 0.0
    for genre in genres:
        genre_score += profile["normalized_genres"].get(genre, 0) * 3.4
    genre_score = min(3.4, genre_score)

    similarity = 0.0
    best_seed = None
    for seed in profile["seeds"]:
        value = _jaccard(genres, seed["genres"]) * min(1.0, seed["weight"] / 5.0)
        if value > similarity:
            similarity = value
            best_seed = seed
    similarity *= 3.0

    quality = min(1.0, _rating(item) / 10.0) * 1.6
    popularity = min(1.0, math.log1p(max(0.0, _popularity(item))) / 12.0) * 0.45
    age_bonus = 0.35 if _year(item) >= CURRENT_YEAR - 2 else 0.0
    type_bonus = 0.35 if profile["type_weights"].get(str(item.get("type") or "")) else 0.0
    language_bonus = 0.0
    if meta.get("language") and profile["language_weights"].get(str(meta["language"])):
        language_bonus = 0.25
    rec_bonus = min(2.4, profile["rec_weights"].get(key, 0.0) * 0.5)
    jitter = _hash01(f"{day_seed}:{key}") * 0.28
    negative = 0.0
    for genre in genres:
        negative += profile["negative_genres"].get(genre, 0) * 0.35

    score = genre_score + similarity + quality + popularity + age_bonus + type_bonus + language_bonus + rec_bonus + jitter - negative

    reason = "Popular now"
    if rec_bonus and key in profile["rec_source"]:
        source_title, source_verb = profile["rec_source"][key]
        reason = f"Because you {source_verb} {source_title}"
    elif similarity > 0.6 and best_seed:
        reason = f"Because you {_seed_verb(best_seed['sources'])} {best_seed['item'].get('title')}"
    elif genres & set(profile.get("top_genres") or []):
        shared = sorted(genres & set(profile["top_genres"]))
        reason = f"More {shared[0].title()}" if shared else "Matched to your taste"
    elif quality > 1.2:
        reason = "Top rated pick"
    return score, reason


def _decorate(items: Iterable[dict[str, Any]], reason: str | None = None) -> list[dict[str, Any]]:
    output = []
    for item in items:
        copied = dict(item)
        if reason:
            copied["reason"] = reason
        output.append(copied)
    return output


def _exclusions(snapshot: dict[str, Any], include_watched: bool = False) -> dict[str, Any]:
    excluded = set()
    excluded.update((snapshot.get("watchlist") or {}).keys())
    for key, reaction in (snapshot.get("reactions") or {}).items():
        if int(reaction.get("value") or 0) < 0:
            excluded.add(key)
    if not include_watched:
        excluded.update((snapshot.get("watched") or {}).keys())
    return {"excludeKeys": excluded}


def _profile_snapshot(
    snapshot: dict[str, Any],
    items_by_key: dict[str, dict[str, Any]],
    meta_map: dict[str, dict[str, Any]],
    filters: dict[str, Any],
) -> dict[str, Any]:
    """Keep adult viewing history out of kids and age-restricted profiles."""
    if not (filters.get("kids") or filters.get("age") or filters.get("maxAge") is not None):
        return snapshot
    clean_filters = {key: value for key, value in filters.items() if key not in ("excludeKeys", "q", "genre", "sort")}

    def visible(key: str) -> bool:
        item = items_by_key.get(key)
        if not item:
            return False
        return discovery.matches(item, discovery.item_meta(item, meta_map), clean_filters)

    filtered = dict(snapshot)
    filtered["watchlist"] = {key: value for key, value in (snapshot.get("watchlist") or {}).items() if visible(key)}
    filtered["reactions"] = {key: value for key, value in (snapshot.get("reactions") or {}).items() if visible(key)}
    filtered["watched"] = {key: value for key, value in (snapshot.get("watched") or {}).items() if visible(key)}
    filtered["progress"] = {
        key: value
        for key, value in (snapshot.get("progress") or {}).items()
        if visible(value.get("key") or "")
    }
    filtered["history"] = [entry for entry in (snapshot.get("history") or []) if visible(entry.get("key") or "")]
    return filtered


def recommend_rows(
    items: list[dict[str, Any]],
    snapshot: dict[str, Any],
    meta_map: dict[str, dict[str, Any]],
    base_filters: dict[str, Any],
    limit: int = 18,
) -> dict[str, Any]:
    items_by_key = {discovery.item_key(item): item for item in items}
    profile_snapshot = _profile_snapshot(snapshot, items_by_key, meta_map, base_filters)
    profile = build_profile(profile_snapshot, items_by_key, meta_map)
    day_seed = time.strftime("%Y-%m-%d")
    exclusions = _exclusions(snapshot)
    filters = {**base_filters, **exclusions}
    candidates = discovery.filter_items(items, meta_map, filters)
    if not candidates:
        filters = dict(base_filters)
        candidates = discovery.filter_items(items, meta_map, filters)

    scored = []
    for item in candidates:
        meta = discovery.item_meta(item, meta_map)
        score, reason = score_item(item, meta, profile, day_seed)
        scored.append((score, reason, item))
    scored.sort(key=lambda entry: entry[0], reverse=True)

    rows: list[dict[str, Any]] = []
    used: set[str] = set()

    if profile["has_signals"]:
        top = []
        for score, reason, item in scored:
            key = discovery.item_key(item)
            if key in used:
                continue
            used.add(key)
            copied = dict(item)
            copied["reason"] = reason
            copied["score"] = round(score, 3)
            top.append(copied)
            if len(top) >= limit:
                break
        rows.append(
            {
                "id": "top-picks",
                "title": "Top picks for you",
                "subtitle": "Ranked from your watchlist, likes, ratings and viewing history.",
                "items": top,
            }
        )

        for seed in profile["seeds"][:3]:
            seed_genres = seed["genres"]
            seed_recs = {
                discovery.item_key(rec)
                for rec in (seed["meta"].get("recommendations") or [])
                if rec.get("tmdbId")
            }
            seed_candidates = []
            for item in candidates:
                key = discovery.item_key(item)
                if key in used or key == seed["key"]:
                    continue
                meta = discovery.item_meta(item, meta_map)
                similarity = _jaccard(seed_genres, _genres(meta))
                if key in seed_recs:
                    similarity = max(similarity, 0.85)
                if similarity < 0.34:
                    continue
                seed_candidates.append((similarity, _rating(item), item))
            seed_candidates.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
            row_items = []
            for similarity, _, item in seed_candidates[:14]:
                key = discovery.item_key(item)
                if key in used:
                    continue
                used.add(key)
                copied = dict(item)
                source_label = _seed_verb(seed["sources"])
                copied["reason"] = f"Because you {source_label} {seed['item'].get('title')}"
                row_items.append(copied)
            if len(row_items) >= 4:
                rows.append(
                    {
                        "id": f"because-{seed['key']}",
                        "title": f"Because you {_seed_verb(seed['sources'])} {seed['item'].get('title')}",
                        "subtitle": "Similar themes, genres and upstream recommendations.",
                        "items": row_items,
                    }
                )

        if profile["top_genres"]:
            genre = profile["top_genres"][0]
            genre_items = [
                item
                for item in candidates
                if genre in _genres(discovery.item_meta(item, meta_map))
                and discovery.item_key(item) not in used
            ]
            genre_items = discovery.sort_items(genre_items, meta_map, "rating")[:limit]
            if len(genre_items) >= 6:
                for item in genre_items:
                    used.add(discovery.item_key(item))
                rows.append(
                    {
                        "id": f"genre-{genre}",
                        "title": f"More {genre.title()}",
                        "subtitle": "A deeper cut into your strongest genre.",
                        "items": _decorate(genre_items, f"Your favourite genre: {genre.title()}"),
                    }
                )
    else:
        popular = discovery.sort_items(candidates, meta_map, "popular")[:limit]
        rows.append(
            {
                "id": "popular",
                "title": "Popular right now",
                "subtitle": "Start with something everyone is watching. Likes and ratings will personalise this page.",
                "items": _decorate(popular, "Popular now"),
            }
        )

    hidden = [
        item
        for item in candidates
        if _rating(item) >= 7.0 and _popularity(item) < 55 and discovery.item_key(item) not in used
    ]
    hidden = discovery.sort_items(hidden, meta_map, "hidden")[:limit]
    if len(hidden) >= 5:
        rows.append(
            {
                "id": "hidden-gems",
                "title": "Hidden gems",
                "subtitle": "High ratings, smaller audiences.",
                "items": _decorate(hidden, "Hidden gem"),
            }
        )

    recent = [
        item
        for item in candidates
        if _year(item) >= CURRENT_YEAR - 1 and _rating(item) >= 6.3 and discovery.item_key(item) not in used
    ]
    recent = discovery.sort_items(recent, meta_map, "newest")[:limit]
    if len(recent) >= 5:
        rows.append(
            {
                "id": "new-releases",
                "title": "New & noteworthy",
                "subtitle": "Fresh titles rated well by viewers.",
                "items": _decorate(recent, "New release"),
            }
        )

    return {
        "ok": True,
        "rows": rows,
        "profile": {
            "signals": profile["signals"],
            "topGenres": [genre.title() for genre in profile["top_genres"]],
            "personalised": profile["has_signals"],
        },
    }


def similar_items(
    target: dict[str, Any],
    items: list[dict[str, Any]],
    meta_map: dict[str, dict[str, Any]],
    limit: int = 14,
) -> list[dict[str, Any]]:
    """Local catalog matches plus upstream recommendations for a details page."""
    meta = discovery.item_meta(target, meta_map)
    target_genres = _genres(meta)
    target_key = discovery.item_key(target)
    items_by_key = {discovery.item_key(item): item for item in items}
    results: list[dict[str, Any]] = []
    seen = {target_key}

    for rec in meta.get("recommendations") or []:
        if not rec.get("tmdbId"):
            continue
        key = discovery.item_key(rec)
        if key in seen:
            continue
        local = items_by_key.get(key)
        if local:
            copied = dict(local)
        else:
            copied = {
                "tmdbId": rec.get("tmdbId"),
                "type": rec.get("type") or "movie",
                "title": rec.get("title") or "",
                "year": rec.get("year") or "",
                "poster": rec.get("poster") or "",
                "backdrop": "",
                "rating": None,
                "overview": "",
                "popularity": 0,
                "upstreamOnly": True,
            }
        copied["reason"] = f"Like {target.get('title') or 'this title'}"
        results.append(copied)
        seen.add(key)
        if len(results) >= limit:
            return results

    scored = []
    for item in items:
        key = discovery.item_key(item)
        if key in seen:
            continue
        other = discovery.item_meta(item, meta_map)
        similarity = _jaccard(target_genres, _genres(other))
        if similarity <= 0:
            continue
        scored.append((similarity * 3 + _rating(item) / 10, item))
    scored.sort(key=lambda entry: entry[0], reverse=True)
    for _, item in scored:
        key = discovery.item_key(item)
        if key in seen:
            continue
        copied = dict(item)
        copied["reason"] = f"Shares {', '.join(sorted(target_genres & _genres(discovery.item_meta(item, meta_map)))).title()}"
        results.append(copied)
        seen.add(key)
        if len(results) >= limit:
            break
    return results
