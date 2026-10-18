#!/usr/bin/env python3
"""Shared metadata and age-rating store for the Private Mirror catalog.

The upstream catalog feed only carries a title, year, poster and rating.  Genre,
certification, runtime and per-title recommendations live on the title detail
endpoint, so they are fetched once and cached in SQLite.  Keeping this in its
own module lets both the web app and the bulk enrichment CLI share the same
store without importing each other.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("META_DB", str(ROOT / ".app_cache" / "metadata.sqlite3")))
DEFAULT_BASE = os.environ.get("NETMIRROR_BASE", "https://net79.cc")

KID_GENRES = {"kids", "family"}
MATURE_GENRES = {"horror", "thriller", "crime", "war", "adult"}

CERT_AGE = {
    "U": 0,
    "G": 0,
    "TV-Y": 0,
    "TV-G": 0,
    "TV-Y7": 7,
    "TV-Y7-FV": 7,
    "PG": 7,
    "TV-PG": 7,
    "PG-13": 13,
    "TV-14": 14,
    "R": 17,
    "TV-MA": 17,
    "NC-17": 18,
    "A": 18,
    "X": 18,
    "M": 15,
    "MA15+": 15,
    "R18+": 18,
    "T": 13,
}

AGE_FILTERS = {
    "kids": 7,
    "family": 12,
    "teen": 15,
    "adult": None,
}


def item_key(media_type: str, tmdb_id: Any) -> str:
    return f"{str(media_type).lower()}:{int(tmdb_id)}"


def normalize_cert(value: Any) -> str:
    text = str(value or "").strip().upper().replace("\u2013", "-")
    text = re.sub(r"\s+", " ", text)
    return text


def cert_min_age(value: Any) -> int | None:
    raw = normalize_cert(value)
    if not raw or raw in {"NR", "UNRATED", "NOT RATED", "UNKNOWN", "N/A", "NA", "-"}:
        return None

    compact = raw.replace(" ", "")
    if compact in CERT_AGE:
        return CERT_AGE[compact]

    if compact.startswith(("U/A", "UA")):
        numbers = re.findall(r"\d+", compact)
        if numbers:
            return min(18, int(numbers[0]))
        return 12
    if compact.startswith("FSK"):
        numbers = re.findall(r"\d+", compact)
        return min(18, int(numbers[0])) if numbers else None

    if raw in {"A", "R", "MA", "MA 15+", "MA15+"}:
        return 17 if raw == "R" else 18 if raw == "A" else 15

    numbers = re.findall(r"\d+", compact)
    if numbers:
        value_int = int(numbers[0])
        if 0 <= value_int <= 21:
            return value_int
    return None


def age_band(min_age: int | None) -> str:
    if min_age is None:
        return "unknown"
    if min_age <= 6:
        return "kids"
    if min_age <= 12:
        return "family"
    if min_age <= 17:
        return "teen"
    return "adult"


def is_kids_safe(genres: Iterable[str], min_age: int | None, adult: bool) -> bool:
    names = {str(genre).strip().lower() for genre in genres or [] if genre}
    if adult or min_age is None or min_age > 7:
        return False
    if names & MATURE_GENRES:
        return False
    return bool(names & KID_GENRES)


def is_family_safe(genres: Iterable[str], min_age: int | None, adult: bool) -> bool:
    names = {str(genre).strip().lower() for genre in genres or [] if genre}
    if adult or min_age is None or min_age > 12:
        return False
    if names & {"horror", "war", "adult"}:
        return False
    return True


def derive(genres: Iterable[str], certification: Any, adult_flag: Any = None) -> dict[str, Any]:
    names = [str(genre).strip() for genre in genres or [] if str(genre).strip()]
    lower = {name.lower() for name in names}
    min_age = cert_min_age(certification)
    adult = bool(adult_flag) or min_age is not None and min_age >= 18
    if "adult" in lower:
        adult = True
    return {
        "genres": names,
        "min_age": min_age,
        "age_band": age_band(min_age),
        "kids_safe": is_kids_safe(names, min_age, adult),
        "family_safe": is_family_safe(names, min_age, adult),
        "adult": adult,
    }


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _parse_json(value: Any, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


class MetadataStore:
    """Thread-safe SQLite-backed metadata cache with a small in-memory index."""

    def __init__(self, path: Path | str = DB_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._reload_lock = threading.Lock()
        self._rows: dict[str, dict[str, Any]] = {}
        self._last_load = 0.0
        self._last_mtime = 0.0
        self._init_db()
        self._load_all()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    tmdb_id INTEGER NOT NULL,
                    title TEXT,
                    year TEXT,
                    genres TEXT,
                    genre_ids TEXT,
                    certification TEXT,
                    cert_country TEXT,
                    min_age INTEGER,
                    age_band TEXT,
                    kids_safe INTEGER DEFAULT 0,
                    family_safe INTEGER DEFAULT 0,
                    adult INTEGER DEFAULT 0,
                    runtime INTEGER,
                    language TEXT,
                    countries TEXT,
                    trailer TEXT,
                    recommendations TEXT,
                    rating REAL,
                    status TEXT DEFAULT 'ok',
                    error TEXT,
                    updated_at REAL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS meta_type_idx ON meta(type)")
            conn.execute("CREATE INDEX IF NOT EXISTS meta_min_age_idx ON meta(min_age)")
            conn.commit()

    def _row_from_db(self, row: sqlite3.Row) -> dict[str, Any]:
        genres = _parse_json(row["genres"], [])
        min_age = row["min_age"]
        if min_age is None and row["certification"]:
            min_age = cert_min_age(row["certification"])
        adult = bool(row["adult"])
        return {
            "key": row["key"],
            "type": row["type"],
            "tmdbId": row["tmdb_id"],
            "title": row["title"],
            "year": row["year"],
            "genres": genres,
            "genreIds": _parse_json(row["genre_ids"], []),
            "certification": row["certification"],
            "certCountry": row["cert_country"],
            "minAge": min_age,
            "ageBand": age_band(min_age),
            "kidsSafe": is_kids_safe(genres, min_age, adult),
            "familySafe": is_family_safe(genres, min_age, adult),
            "adult": adult,
            "runtime": row["runtime"],
            "language": row["language"],
            "countries": _parse_json(row["countries"], []),
            "trailer": row["trailer"],
            "recommendations": _parse_json(row["recommendations"], []),
            "rating": row["rating"],
            "status": row["status"],
            "error": row["error"],
            "updatedAt": row["updated_at"],
        }

    def _load_all(self) -> None:
        started = time.time()
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM meta").fetchall()
        parsed = {row["key"]: self._row_from_db(row) for row in rows}
        with self._lock:
            if self._last_load > started:
                return
            self._rows = parsed
            self._last_load = time.time()
            try:
                self._last_mtime = self.path.stat().st_mtime
            except OSError:
                self._last_mtime = 0.0

    def refresh(self, max_age: float = 4.0) -> None:
        """Reload rows written by another process (the enrichment CLI).

        SQLite WAL writes do not reliably change the main database mtime, so a
        short unconditional reload is used instead of mtime comparison.  With a
        ten-thousand-row cache this costs only a few milliseconds.
        """
        with self._lock:
            if time.time() - self._last_load < max_age:
                return
        with self._reload_lock:
            with self._lock:
                if time.time() - self._last_load < max_age:
                    return
            self._load_all()

    def get(self, key: str, refresh: bool = True) -> dict[str, Any] | None:
        if refresh:
            self.refresh()
        with self._lock:
            return self._rows.get(key)

    def get_many(self, keys: Iterable[str]) -> dict[str, dict[str, Any]]:
        self.refresh()
        wanted = list(keys)
        with self._lock:
            return {key: self._rows[key] for key in wanted if key in self._rows}

    def all(self) -> dict[str, dict[str, Any]]:
        self.refresh()
        with self._lock:
            return dict(self._rows)

    def upsert(self, row: dict[str, Any]) -> None:
        values = (
            row["key"],
            row["type"],
            int(row["tmdbId"]),
            row.get("title") or "",
            row.get("year") or "",
            _json(row.get("genres") or []),
            _json(row.get("genreIds") or []),
            row.get("certification") or "",
            row.get("certCountry") or "",
            row.get("minAge"),
            row.get("ageBand") or "unknown",
            1 if row.get("kidsSafe") else 0,
            1 if row.get("familySafe") else 0,
            1 if row.get("adult") else 0,
            row.get("runtime"),
            row.get("language") or "",
            _json(row.get("countries") or []),
            row.get("trailer") or "",
            _json(row.get("recommendations") or []),
            row.get("rating"),
            row.get("status") or "ok",
            row.get("error") or "",
            float(row.get("updatedAt") or time.time()),
        )
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO meta (
                        key, type, tmdb_id, title, year, genres, genre_ids,
                        certification, cert_country, min_age, age_band,
                        kids_safe, family_safe, adult, runtime, language,
                        countries, trailer, recommendations, rating, status,
                        error, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        type=excluded.type,
                        tmdb_id=excluded.tmdb_id,
                        title=excluded.title,
                        year=excluded.year,
                        genres=excluded.genres,
                        genre_ids=excluded.genre_ids,
                        certification=excluded.certification,
                        cert_country=excluded.cert_country,
                        min_age=excluded.min_age,
                        age_band=excluded.age_band,
                        kids_safe=excluded.kids_safe,
                        family_safe=excluded.family_safe,
                        adult=excluded.adult,
                        runtime=excluded.runtime,
                        language=excluded.language,
                        countries=excluded.countries,
                        trailer=excluded.trailer,
                        recommendations=excluded.recommendations,
                        rating=excluded.rating,
                        status=excluded.status,
                        error=excluded.error,
                        updated_at=excluded.updated_at
                    """,
                    values,
                )
                conn.commit()
            self._rows[row["key"]] = dict(row)
            self._last_load = time.time()
            try:
                self._last_mtime = self.path.stat().st_mtime
            except OSError:
                pass

    def coverage(self, total: int) -> dict[str, Any]:
        rows = self.all()
        known = sum(1 for row in rows.values() if row.get("status") == "ok")
        rated = sum(1 for row in rows.values() if row.get("status") == "ok" and row.get("minAge") is not None)
        return {
            "known": known,
            "rated": rated,
            "total": total,
            "percent": round((rated / total) * 100, 1) if total else 0.0,
        }

    def missing_keys(self, items: Iterable[dict[str, Any]], include_errors: bool = False) -> list[str]:
        rows = self.all()
        missing = []
        for item in items:
            if not item.get("tmdbId"):
                continue
            key = item_key(item.get("type") or "", item["tmdbId"])
            row = rows.get(key)
            if row is None or (include_errors and row.get("status") == "error"):
                missing.append(key)
        return missing


_STORE: MetadataStore | None = None
_STORE_LOCK = threading.Lock()


def store() -> MetadataStore:
    global _STORE
    if _STORE is None:
        with _STORE_LOCK:
            if _STORE is None:
                _STORE = MetadataStore(DB_PATH)
    return _STORE


def row_from_details(data: dict[str, Any], media_type: str, tmdb_id: Any) -> dict[str, Any]:
    genres = [str(genre.get("name") or "").strip() for genre in data.get("genres") or [] if genre.get("name")]
    genre_ids = [genre.get("id") for genre in data.get("genres") or [] if genre.get("id") is not None]
    certification = data.get("certification") or {}
    cert_rating = certification.get("rating") if isinstance(certification, dict) else certification
    derived = derive(genres, cert_rating, data.get("adult"))
    recommendations = []
    for rec in data.get("recommendations") or []:
        if not isinstance(rec, dict) or not rec.get("tmdbId"):
            continue
        recommendations.append(
            {
                "tmdbId": rec.get("tmdbId"),
                "type": rec.get("type") or "movie",
                "title": rec.get("title") or "",
                "year": rec.get("year") or "",
                "poster": rec.get("poster") or "",
            }
        )
        if len(recommendations) >= 10:
            break
    return {
        "key": item_key(media_type, tmdb_id),
        "type": str(media_type).lower(),
        "tmdbId": int(tmdb_id),
        "title": data.get("title") or "",
        "year": data.get("year") or "",
        "genres": genres,
        "genreIds": genre_ids,
        "certification": normalize_cert(cert_rating) or "",
        "certCountry": certification.get("country") if isinstance(certification, dict) else "",
        "minAge": derived["min_age"],
        "ageBand": derived["age_band"],
        "kidsSafe": derived["kids_safe"],
        "familySafe": derived["family_safe"],
        "adult": derived["adult"],
        "runtime": int(data.get("runtime") or 0) or None,
        "language": data.get("originalLanguage") or "",
        "countries": [part for part in str(data.get("originCountry") or "").split(",") if part],
        "trailer": data.get("trailerKey") or "",
        "recommendations": recommendations,
        "rating": data.get("rating"),
        "status": "ok",
        "error": "",
        "updatedAt": time.time(),
    }


def fetch_and_store(
    session: Any,
    base: str,
    media_type: str,
    tmdb_id: Any,
    metadata_store: MetadataStore | None = None,
    timeout: int = 20,
) -> dict[str, Any] | None:
    """Fetch one title's detail metadata and cache it. Never raises network errors."""
    metadata_store = metadata_store or store()
    url = f"{base.rstrip('/')}/api/catalog/title/{media_type}/{tmdb_id}"
    try:
        response = session.get(url, timeout=timeout)
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}")
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("error") or "detail response was not ok")
    except Exception as exc:  # noqa: BLE001 - upstream errors are data, not crashes
        row = {
            "key": item_key(media_type, tmdb_id),
            "type": str(media_type).lower(),
            "tmdbId": int(tmdb_id),
            "title": "",
            "year": "",
            "genres": [],
            "genreIds": [],
            "certification": "",
            "certCountry": "",
            "minAge": None,
            "ageBand": "unknown",
            "kidsSafe": False,
            "familySafe": False,
            "adult": False,
            "runtime": None,
            "language": "",
            "countries": [],
            "trailer": "",
            "recommendations": [],
            "rating": None,
            "status": "error",
            "error": str(exc)[:240],
            "updatedAt": time.time(),
        }
        metadata_store.upsert(row)
        return None
    row = row_from_details(data, media_type, tmdb_id)
    metadata_store.upsert(row)
    return row
