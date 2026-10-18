#!/usr/bin/env python3
"""Background torrent downloads.

The player streams a torrent through the WebTorrent bridge while the user is
watching.  When the swarm is too slow, the player offers to keep the selected
file downloading in the background instead.  This module owns those records:
it persists them in ``state/downloads.json``, keeps the bridge entry alive,
tracks progress and hands completed files to the local file server.

Only the selected video file is downloaded.  The bridge deselects every other
file when a torrent is opened, so the background record continues just that
file and can be played as soon as enough pieces are available.
"""

from __future__ import annotations

import copy
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
STATE_PATH = Path(os.environ.get("DOWNLOADS_PATH", str(ROOT / "state" / "downloads.json")))
POLL_SECONDS = max(2.0, float(os.environ.get("TORRENT_DOWNLOAD_POLL", "4")))
RETRY_SECONDS = max(10.0, float(os.environ.get("TORRENT_DOWNLOAD_RETRY", "30")))
INFO_HASH_RE = re.compile(r"^[a-f0-9]{40}$")


class DownloadError(RuntimeError):
    """Raised when a download cannot be created or used."""


def download_id(info_hash: str, file_index: int) -> str:
    return f"{str(info_hash).lower()}-{int(file_index)}"


def _text(value: Any, limit: int = 400) -> str:
    return str(value or "")[:limit]


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in (float("inf"), float("-inf")):
        return default
    return number


def _clean_item(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    media_type = str(item.get("type") or "").lower()
    tmdb_id = item.get("tmdbId")
    if media_type not in ("movie", "tv") or not str(tmdb_id or "").isdigit():
        return {}
    return {
        "type": media_type,
        "tmdbId": int(tmdb_id),
        "title": _text(item.get("title"), 300),
        "year": _text(item.get("year"), 8),
        "poster": _text(item.get("poster"), 1200),
        "backdrop": _text(item.get("backdrop"), 1200),
    }


class Downloads:
    def __init__(self, torrents_module: Any, path: Path | str = STATE_PATH) -> None:
        self._torrents = torrents_module
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._items: dict[str, dict[str, Any]] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._dirty = False
        self._last_save = 0.0
        self._attempts: dict[str, float] = {}
        self._read()

    # -- persistence -------------------------------------------------------

    def _read(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        items = payload.get("downloads") if isinstance(payload, dict) else None
        if not isinstance(items, dict):
            return
        now = time.time()
        for key, raw in items.items():
            if not isinstance(raw, dict):
                continue
            info_hash = str(raw.get("infoHash") or "").lower()
            file_index = int(_number(raw.get("fileIndex"), 0))
            if not INFO_HASH_RE.match(info_hash):
                continue
            record = {
                "id": _text(raw.get("id") or key, 80),
                "infoHash": info_hash,
                "magnet": _text(raw.get("magnet"), 12000),
                "fileIndex": max(0, file_index),
                "fileName": _text(raw.get("fileName"), 500),
                "filePath": _text(raw.get("filePath"), 2000),
                "contentType": _text(raw.get("contentType") or "video/mp4", 120),
                "quality": int(_number(raw.get("quality"), 0)),
                "size": int(_number(raw.get("size"), 0)),
                "downloaded": int(_number(raw.get("downloaded"), 0)),
                "progress": max(0.0, min(1.0, _number(raw.get("progress"), 0))),
                "downloadSpeed": 0,
                "numPeers": 0,
                "timeRemaining": 0,
                "title": _text(raw.get("title"), 400),
                "baseTitle": _text(raw.get("baseTitle"), 400),
                "displayTitle": _text(raw.get("displayTitle"), 400),
                "mediaType": _text(raw.get("mediaType"), 12),
                "tmdbId": _text(raw.get("tmdbId"), 20),
                "year": _text(raw.get("year"), 8),
                "poster": _text(raw.get("poster"), 1200),
                "backdrop": _text(raw.get("backdrop"), 1200),
                "season": max(1, int(_number(raw.get("season"), 1))),
                "episode": max(1, int(_number(raw.get("episode"), 1))),
                "item": _clean_item(raw.get("item")),
                "status": str(raw.get("status") or "queued"),
                "error": _text(raw.get("error"), 600),
                "createdAt": _number(raw.get("createdAt"), now),
                "updatedAt": _number(raw.get("updatedAt"), now),
                "completedAt": _number(raw.get("completedAt"), 0),
            }
            if not record["magnet"]:
                record["status"] = "error"
                record["error"] = "The magnet link was not saved."
            elif record["status"] not in ("done", "error"):
                record["status"] = "queued"
                record["error"] = ""
            self._items[record["id"]] = record

    def _write(self) -> None:
        payload = {
            "version": 1,
            "updatedAt": time.time(),
            "downloads": self._items,
        }
        temp = self.path.with_suffix(self.path.suffix + f".{secrets.token_hex(4)}.tmp")
        try:
            temp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            temp.replace(self.path)
        except OSError:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    def _touch(self, force: bool = False) -> None:
        self._dirty = True
        now = time.time()
        if force or now - self._last_save >= 4.0:
            self._write()
            self._dirty = False
            self._last_save = now

    # -- worker ------------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, name="torrent-downloads", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=3)

    def _run(self) -> None:
        # Give the web server a moment to finish starting.
        if self._stop.wait(1.5):
            return
        while not self._stop.wait(POLL_SECONDS):
            try:
                self.refresh_all()
            except Exception:  # noqa: BLE001 - the worker must survive runtime failures
                time.sleep(2)
        with self._lock:
            if self._dirty:
                self._write()
                self._dirty = False

    def refresh_all(self) -> None:
        with self._lock:
            ids = [
                key
                for key, record in self._items.items()
                if record.get("status") in ("queued", "downloading")
            ]
        for key in ids:
            if self._stop.is_set():
                return
            self.refresh(key)

    # -- public operations -------------------------------------------------

    def add(self, payload: dict[str, Any], magnet: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        info_hash = str(payload.get("h") or "").lower()
        if not INFO_HASH_RE.match(info_hash):
            raise DownloadError("The torrent information hash is invalid.")
        magnet = str(magnet or "").strip()
        if not magnet.startswith("magnet:?"):
            raise DownloadError("The torrent link is no longer available. Start the torrent again.")
        try:
            file_index = max(0, int(payload.get("i") or 0))
        except (TypeError, ValueError):
            file_index = 0
        extra = extra or {}
        item = _clean_item(extra.get("item"))
        now = time.time()
        record_id = download_id(info_hash, file_index)
        with self._lock:
            record = self._items.get(record_id)
            if record is None:
                record = {
                    "id": record_id,
                    "infoHash": info_hash,
                    "magnet": magnet,
                    "fileIndex": file_index,
                    "fileName": _text(payload.get("n"), 500),
                    "filePath": "",
                    "contentType": _text(payload.get("ct") or "video/mp4", 120),
                    "quality": int(_number(payload.get("q"), 0)),
                    "size": 0,
                    "downloaded": 0,
                    "progress": 0.0,
                    "downloadSpeed": 0,
                    "numPeers": 0,
                    "timeRemaining": 0,
                    "status": "queued",
                    "error": "",
                    "createdAt": now,
                    "updatedAt": now,
                    "completedAt": 0,
                }
                self._items[record_id] = record
            for other_id in [
                key
                for key, other in self._items.items()
                if other.get("infoHash") == info_hash and key != record_id
            ]:
                self._items.pop(other_id, None)
            record.update(
                {
                    "magnet": magnet,
                    "title": _text(extra.get("title") or record.get("title"), 400),
                    "baseTitle": _text(extra.get("baseTitle") or extra.get("title") or record.get("baseTitle"), 400),
                    "displayTitle": _text(
                        extra.get("displayTitle") or extra.get("title") or record.get("displayTitle"), 400
                    ),
                    "mediaType": _text(extra.get("mediaType") or record.get("mediaType"), 12),
                    "tmdbId": _text(extra.get("tmdbId") or record.get("tmdbId"), 20),
                    "year": _text(extra.get("year") or record.get("year"), 8),
                    "poster": _text(extra.get("poster") or record.get("poster"), 1200),
                    "backdrop": _text(extra.get("backdrop") or record.get("backdrop"), 1200),
                    "season": max(1, int(_number(extra.get("season"), record.get("season") or 1))),
                    "episode": max(1, int(_number(extra.get("episode"), record.get("episode") or 1))),
                    "updatedAt": now,
                }
            )
            if item:
                record["item"] = item
                record["mediaType"] = record["mediaType"] or item.get("type", "")
                record["tmdbId"] = record["tmdbId"] or str(item.get("tmdbId") or "")
                record["year"] = record["year"] or item.get("year", "")
                record["poster"] = record["poster"] or item.get("poster", "")
                record["backdrop"] = record["backdrop"] or item.get("backdrop", "")
            if record.get("status") == "error":
                record["status"] = "queued"
                record["error"] = ""
            elif record.get("status") == "done" and self._resolve_local(record) is None:
                record["status"] = "queued"
                record["progress"] = 0.0
                record["downloaded"] = 0
                record["filePath"] = ""
                record["completedAt"] = 0
                record["error"] = ""
            self._attempts.pop(record_id, None)
            self._touch(force=True)
            return copy.deepcopy(record)

    def get(self, record_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._items.get(str(record_id or ""))
            return copy.deepcopy(record) if record else None

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            records = [copy.deepcopy(record) for record in self._items.values()]
        records.sort(key=lambda record: (record.get("createdAt") or 0), reverse=True)
        return records

    def stats(self) -> dict[str, int]:
        with self._lock:
            records = list(self._items.values())
        return {
            "total": len(records),
            "downloading": sum(1 for record in records if record.get("status") in ("queued", "downloading")),
            "done": sum(1 for record in records if record.get("status") == "done"),
            "error": sum(1 for record in records if record.get("status") == "error"),
        }

    def retry(self, record_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._items.get(str(record_id or ""))
            if not record:
                return None
            record["status"] = "queued"
            record["error"] = ""
            record["progress"] = 0.0
            record["downloaded"] = 0
            record["downloadSpeed"] = 0
            record["numPeers"] = 0
            record["timeRemaining"] = 0
            record["filePath"] = ""
            record["completedAt"] = 0
            record["updatedAt"] = time.time()
            self._attempts.pop(record["id"], None)
            self._touch(force=True)
            return copy.deepcopy(record)

    def remove(self, record_id: str, delete_files: bool = True) -> bool:
        record_id = str(record_id or "")
        with self._lock:
            record = self._items.pop(record_id, None)
            if not record:
                return False
            shared = any(
                other.get("infoHash") == record.get("infoHash")
                for other in self._items.values()
            )
            self._attempts.pop(record_id, None)
            self._touch(force=True)
        if not shared:
            path = self._resolve_local(record)
            try:
                self._torrents.remove_torrent(record.get("infoHash") or "", keep_data=not delete_files)
            except Exception:  # noqa: BLE001 - the bridge may already be stopped
                pass
            if delete_files and path is not None:
                self._delete_file(path)
        return True

    def local_path(self, record: dict[str, Any] | None) -> Path | None:
        return self._resolve_local(record) if record else None

    def stream_payload(self, record: dict[str, Any]) -> dict[str, Any]:
        """Return the metadata the app needs to issue a playback token."""
        return {
            "infoHash": record.get("infoHash") or "",
            "magnet": record.get("magnet") or "",
            "fileIndex": int(record.get("fileIndex") or 0),
            "fileName": record.get("fileName") or "",
            "contentType": record.get("contentType") or "video/mp4",
            "quality": int(record.get("quality") or 0),
            "size": int(record.get("size") or 0),
        }

    # -- refreshing --------------------------------------------------------

    def refresh(self, record_id: str) -> None:
        with self._lock:
            record = self._items.get(record_id)
            if not record or record.get("status") not in ("queued", "downloading"):
                return
            snapshot = copy.deepcopy(record)
        status: dict[str, Any] | None = None
        error = ""
        try:
            status = self._torrents.torrent_status(snapshot["infoHash"])
        except Exception as exc:  # noqa: BLE001 - bridge failures are runtime data
            error = str(exc)
        now = time.time()
        if status is None:
            last_attempt = self._attempts.get(record_id, 0.0)
            if snapshot.get("magnet") and now - last_attempt >= RETRY_SECONDS:
                self._attempts[record_id] = now
                try:
                    status = self._torrents.add_torrent(
                        snapshot["magnet"],
                        file_index=snapshot.get("fileIndex"),
                        timeout=30,
                    )
                except Exception as exc:  # noqa: BLE001
                    error = str(exc)
            if status is None:
                with self._lock:
                    record = self._items.get(record_id)
                    if record:
                        record["error"] = error[:600] or "Waiting for the torrent bridge."
                        record["downloadSpeed"] = 0
                        record["numPeers"] = 0
                        record["updatedAt"] = now
                        self._touch()
                return
        try:
            selected = int(status.get("selected", -1))
        except (TypeError, ValueError):
            selected = -1
        index = int(snapshot.get("fileIndex") or 0)
        if selected != index:
            try:
                status = self._torrents.select_file(snapshot["infoHash"], index)
            except Exception as exc:  # noqa: BLE001 - selection is best-effort
                with self._lock:
                    record = self._items.get(record_id)
                    if record:
                        record["error"] = str(exc)[:600] or "Could not select the video file."
                        record["downloadSpeed"] = 0
                        record["numPeers"] = 0
                        record["updatedAt"] = now
                        self._touch()
                return
        self._apply_status(record_id, status, now)

    def _apply_status(self, record_id: str, status: dict[str, Any], now: float) -> None:
        file_info = status.get("file") if isinstance(status.get("file"), dict) else {}
        file_progress = _number(status.get("fileProgress"), -1)
        if file_progress < 0:
            file_progress = _number(status.get("progress"), 0)
        file_progress = max(0.0, min(1.0, file_progress))
        size = int(_number(file_info.get("length"), 0))
        done = bool(status.get("done")) or file_progress >= 0.999
        with self._lock:
            record = self._items.get(record_id)
            if not record:
                return
            record["fileName"] = _text(file_info.get("name") or record.get("fileName"), 500)
            file_path = _text(file_info.get("path"), 2000)
            if file_path:
                record["filePath"] = file_path
            record["contentType"] = _text(file_info.get("type") or record.get("contentType"), 120)
            if size:
                record["size"] = size
            record["downloadSpeed"] = int(_number(status.get("downloadSpeed"), 0))
            record["numPeers"] = int(_number(status.get("numPeers"), 0))
            record["timeRemaining"] = int(_number(status.get("timeRemaining"), 0))
            record["error"] = ""
            record["updatedAt"] = now
            if done and self._resolve_local(record) is not None:
                record["progress"] = 1.0
                record["downloaded"] = int(record.get("size") or 0)
                if record.get("status") != "done":
                    record["completedAt"] = now
                record["status"] = "done"
                record["downloadSpeed"] = 0
                record["timeRemaining"] = 0
            else:
                record["progress"] = file_progress
                estimated = int(_number(status.get("fileDownloaded"), 0))
                if estimated <= 0 and size:
                    estimated = int(round(size * file_progress))
                record["downloaded"] = max(0, estimated)
                record["status"] = "downloading"
                if done:
                    record["error"] = "Waiting for the downloaded file to appear on disk…"
            self._touch(force=record["status"] == "done")

    def _resolve_local(self, record: dict[str, Any]) -> Path | None:
        relative = str(record.get("filePath") or "").strip()
        if not relative:
            return None
        data_dir = Path(getattr(self._torrents, "BRIDGE_DATA_DIR", ROOT / ".app_cache" / "torrents"))
        try:
            base = data_dir.resolve()
            candidate = (base / relative.replace("\\", "/")).resolve()
            candidate.relative_to(base)
        except (OSError, ValueError):
            return None
        return candidate if candidate.is_file() else None

    @staticmethod
    def _delete_file(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            return
        parent = path.parent
        for _ in range(2):
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
