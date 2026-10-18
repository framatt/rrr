#!/usr/bin/env python3
"""Single-profile library persisted as a small JSON document.

Holds the watchlist, likes/dislikes, watched markers, playback progress,
recently opened titles and lists imported by the user.  The profile is
intentionally server-side: it survives browser storage clears, works across
devices on the same private server, and feeds the recommendation engine.
"""

from __future__ import annotations

import copy
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DATA_PATH = Path(os.environ.get("LIBRARY_PATH", str(ROOT / "state" / "userdata.json")))
MAX_HISTORY = 200
MAX_LISTS = 40


def key_for(media_type: Any, tmdb_id: Any) -> str:
    return f"{str(media_type or '').lower()}:{int(tmdb_id)}"


def default_data() -> dict[str, Any]:
    return {
        "version": 1,
        "watchlist": {},
        "reactions": {},
        "watched": {},
        "progress": {},
        "history": [],
        "lists": {},
        "settings": {"kids": False, "maxAge": None},
        "updatedAt": time.time(),
    }


def clean_item(item: dict[str, Any]) -> dict[str, Any]:
    media_type = str(item.get("type") or "").lower()
    tmdb_id = item.get("tmdbId")
    if media_type not in ("movie", "tv") or not str(tmdb_id or "").isdigit():
        raise ValueError("invalid item")
    return {
        "key": key_for(media_type, tmdb_id),
        "type": media_type,
        "tmdbId": int(tmdb_id),
        "title": str(item.get("title") or "")[:300],
        "year": str(item.get("year") or "")[:8],
        "poster": str(item.get("poster") or "")[:1000],
        "backdrop": str(item.get("backdrop") or "")[:1000],
    }


class Library:
    def __init__(self, path: Path | str = DATA_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._data = self._read()

    def _read(self) -> dict[str, Any]:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    merged = default_data()
                    for field in merged:
                        if field in data:
                            merged[field] = data[field]
                    return merged
            except (OSError, ValueError):
                pass
        return default_data()

    def _write(self) -> None:
        self._data["updatedAt"] = time.time()
        temp = self.path.with_suffix(self.path.suffix + f".{secrets.token_hex(4)}.tmp")
        temp.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8")
        temp.replace(self.path)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

    # -- watchlist ---------------------------------------------------------
    def watchlist_add(self, item: dict[str, Any]) -> dict[str, Any]:
        record = clean_item(item)
        record["addedAt"] = time.time()
        with self._lock:
            self._data["watchlist"][record["key"]] = record
            self._write()
        return record

    def watchlist_remove(self, key: str) -> bool:
        with self._lock:
            removed = self._data["watchlist"].pop(key, None) is not None
            if removed:
                self._write()
        return removed

    # -- reactions and watched state --------------------------------------
    def react(self, key: str, value: int) -> None:
        with self._lock:
            if value == 0:
                self._data["reactions"].pop(key, None)
            else:
                self._data["reactions"][key] = {"value": 1 if value > 0 else -1, "at": time.time()}
            self._write()

    def mark_watched(self, item: dict[str, Any], watched: bool, rating: int | None = None) -> None:
        record = clean_item(item)
        with self._lock:
            if watched:
                entry = {"at": time.time()}
                if rating is not None:
                    entry["rating"] = max(1, min(5, int(rating)))
                self._data["watched"][record["key"]] = entry
                self._data["progress"].pop(record["key"], None)
            else:
                self._data["watched"].pop(record["key"], None)
            self._write()

    # -- playback progress -------------------------------------------------
    def save_progress(
        self,
        item: dict[str, Any],
        position: float,
        duration: float,
        season: int = 1,
        episode: int = 1,
    ) -> dict[str, Any] | None:
        record = clean_item(item)
        progress_key = (
            f"{record['key']}:s{max(1, int(season))}:e{max(1, int(episode))}"
            if record["type"] == "tv"
            else record["key"]
        )
        position = max(0.0, float(position or 0))
        duration = max(0.0, float(duration or 0))
        with self._lock:
            finished = duration > 0 and position >= duration - 25
            if position < 10 or finished:
                self._data["progress"].pop(progress_key, None)
                if finished:
                    self._data["watched"][record["key"]] = {"at": time.time()}
                    self._history_add(record, season, episode)
                self._write()
                return None
            entry = {
                **record,
                "progressKey": progress_key,
                "season": max(1, int(season)),
                "episode": max(1, int(episode)),
                "position": round(position, 1),
                "duration": round(duration, 1),
                "updatedAt": time.time(),
            }
            self._data["progress"][progress_key] = entry
            self._history_add(record, season, episode)
            self._write()
            return entry

    def clear_progress(self, progress_key: str) -> bool:
        with self._lock:
            removed = self._data["progress"].pop(progress_key, None) is not None
            if removed:
                self._write()
        return removed

    def _history_add(self, record: dict[str, Any], season: int = 1, episode: int = 1) -> None:
        history = self._data.get("history") or []
        history = [entry for entry in history if not (entry.get("key") == record["key"] and entry.get("season") == season and entry.get("episode") == episode)]
        history.insert(
            0,
            {
                "key": record["key"],
                "type": record["type"],
                "tmdbId": record["tmdbId"],
                "title": record["title"],
                "year": record["year"],
                "poster": record["poster"],
                "season": max(1, int(season)),
                "episode": max(1, int(episode)),
                "at": time.time(),
            },
        )
        self._data["history"] = history[:MAX_HISTORY]

    def touch_history(self, item: dict[str, Any], season: int = 1, episode: int = 1) -> None:
        record = clean_item(item)
        with self._lock:
            self._history_add(record, season, episode)
            self._write()

    # -- settings and custom lists ----------------------------------------
    def update_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            settings = self._data.setdefault("settings", {})
            if "kids" in values:
                settings["kids"] = bool(values["kids"])
            if "maxAge" in values:
                raw = values["maxAge"]
                settings["maxAge"] = None if raw in (None, "", "any") else max(0, min(21, int(raw)))
            self._write()
            return copy.deepcopy(settings)

    def add_list(self, name: str, source: str, items: list[dict[str, Any]], matched: int, missing: int) -> dict[str, Any]:
        list_id = f"custom-{secrets.token_hex(5)}"
        record = {
            "id": list_id,
            "name": str(name or "Imported list")[:160],
            "source": str(source or "")[:600],
            "createdAt": time.time(),
            "items": items[:500],
            "matched": int(matched),
            "missing": int(missing),
        }
        with self._lock:
            lists = self._data.setdefault("lists", {})
            if len(lists) >= MAX_LISTS:
                oldest = min(lists, key=lambda key: lists[key].get("createdAt") or 0)
                lists.pop(oldest, None)
            lists[list_id] = record
            self._write()
        return record

    def remove_list(self, list_id: str) -> bool:
        with self._lock:
            removed = self._data["lists"].pop(list_id, None) is not None
            if removed:
                self._write()
        return removed


_LIBRARY: Library | None = None
_LOCK = threading.Lock()


def library() -> Library:
    global _LIBRARY
    if _LIBRARY is None:
        with _LOCK:
            if _LIBRARY is None:
                _LIBRARY = Library(DATA_PATH)
    return _LIBRARY
