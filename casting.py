#!/usr/bin/env python3
"""Chromecast discovery and playback control.

The browser Cast SDK requires a secure context (HTTPS or localhost). Driving
the devices from the Flask process instead keeps the web UI usable over plain
HTTP on the LAN.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any

LOGGER = logging.getLogger(__name__)

DEVICE_TTL = 30.0
CAST_PORT = 8009

_discovery_lock = threading.Lock()
_state_lock = threading.RLock()
_devices: list[dict[str, Any]] = []
_devices_at = 0.0
_active: dict[str, Any] = {}

_pychromecast = None
_import_checked = False


class CastError(RuntimeError):
    """Raised when a Cast device cannot be reached or played to."""


def _load_pychromecast():
    global _pychromecast, _import_checked
    with _state_lock:
        if _import_checked:
            return _pychromecast
        _import_checked = True
        try:
            import pychromecast
        except Exception as exc:  # ImportError plus zeroconf/socket load failures
            LOGGER.warning("pychromecast unavailable: %s", exc)
            _pychromecast = None
        else:
            _pychromecast = pychromecast
        return _pychromecast


def available() -> bool:
    return _load_pychromecast() is not None


def _serialize(info) -> dict[str, Any]:
    return {
        "id": str(info.uuid),
        "name": info.friendly_name or info.model_name or "Cast device",
        "model": info.model_name or "",
        "host": info.host,
        "type": info.cast_type or "cast",
    }


def devices(refresh: bool = False, timeout: float = 2.5) -> list[dict[str, Any]]:
    global _devices, _devices_at
    pychromecast = _load_pychromecast()
    if pychromecast is None:
        return []

    now = time.monotonic()
    with _state_lock:
        if not refresh and _devices and now - _devices_at < DEVICE_TTL:
            return [dict(device) for device in _devices]

    try:
        from pychromecast.discovery import discover_chromecasts

        with _discovery_lock:
            infos, browser = discover_chromecasts(timeout=timeout)
            try:
                found = [_serialize(info) for info in infos]
            finally:
                browser.stop_discovery()
    except Exception as exc:
        LOGGER.warning("Chromecast discovery failed: %s", exc)
        with _state_lock:
            return [dict(device) for device in _devices]

    found.sort(key=lambda device: device["name"].lower())
    with _state_lock:
        _devices = found
        _devices_at = time.monotonic()
    return [dict(device) for device in found]


def _device(device_id: str) -> dict[str, Any]:
    for device in devices():
        if device["id"] == device_id:
            return device
    for device in devices(refresh=True):
        if device["id"] == device_id:
            return device
    raise CastError("That Cast device is not on the network right now.")


def play(
    device_id: str,
    url: str,
    content_type: str = "video/mp4",
    title: str = "",
    position: float | None = None,
    thumb: str | None = None,
) -> dict[str, Any]:
    pychromecast = _load_pychromecast()
    if pychromecast is None:
        raise CastError("pychromecast is not installed. Run: pip install -r requirements.txt")

    try:
        device_uuid = uuid.UUID(device_id)
    except (ValueError, AttributeError, TypeError) as exc:
        raise CastError("Invalid Cast device identifier.") from exc

    device = _device(device_id)
    stop(quiet=True)

    cast = None
    try:
        cast = pychromecast.get_chromecast_from_host(
            (device["host"], CAST_PORT, device_uuid, device["model"] or None, device["name"])
        )
        cast.wait(timeout=10)
        controller = cast.media_controller
        controller.play_media(
            url,
            content_type,
            title=title or None,
            thumb=thumb or None,
            current_time=max(0.0, float(position or 0.0)),
            stream_type="BUFFERED",
        )
        controller.block_until_active(timeout=15)
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            controller.update_status()
            media = controller.status
            state = media.player_state if media is not None else ""
            if state in ("PLAYING", "BUFFERING", "PAUSED"):
                break
            if media is not None and media.idle_reason in ("ERROR", "CANCELLED", "INTERRUPTED"):
                raise CastError(f"The TV rejected the stream ({media.idle_reason}).")
            time.sleep(0.4)
        else:
            raise CastError("The TV did not start the stream in time.")
    except CastError:
        try:
            cast.disconnect(timeout=2)
        except Exception:
            pass
        raise
    except Exception as exc:
        try:
            cast.disconnect(timeout=2)
        except Exception:
            pass
        raise CastError(f"The TV could not start this stream: {exc}") from exc

    with _state_lock:
        _active.clear()
        _active.update(
            {
                "cast": cast,
                "device": device,
                "title": title,
                "url": url,
                "since": time.time(),
            }
        )
    return status()


def status() -> dict[str, Any]:
    with _state_lock:
        cast = _active.get("cast")
        device = dict(_active.get("device") or {})
        fallback_title = _active.get("title") or ""

    if cast is None:
        return {"ok": True, "active": False, "device": None, "state": "IDLE"}

    controller = cast.media_controller
    try:
        controller.update_status()
    except Exception:
        pass

    media = controller.status
    state = "IDLE"
    position = 0.0
    duration = None
    title = fallback_title
    if media is not None:
        state = media.player_state or "UNKNOWN"
        position = float(media.adjusted_current_time or 0.0)
        duration = float(media.duration) if media.duration else None
        title = media.title or fallback_title

    return {
        "ok": True,
        "active": state not in ("IDLE", "UNKNOWN"),
        "device": device or None,
        "state": state,
        "title": title,
        "position": round(position, 2),
        "duration": duration,
        "idle_reason": getattr(media, "idle_reason", None) if media is not None else None,
    }


def control(action: str, value: float | None = None) -> dict[str, Any]:
    with _state_lock:
        cast = _active.get("cast")
    if cast is None:
        raise CastError("Nothing is casting right now.")

    controller = cast.media_controller
    try:
        if action == "play":
            controller.play()
        elif action == "pause":
            controller.pause()
        elif action == "seek":
            controller.seek(max(0.0, float(value or 0.0)))
        elif action == "stop":
            return stop()
        else:
            raise CastError("Unknown playback action.")
    except CastError:
        raise
    except Exception as exc:
        raise CastError(f"Could not control the Cast device: {exc}") from exc
    return status()


def stop(quiet: bool = False) -> dict[str, Any]:
    with _state_lock:
        cast = _active.pop("cast", None)
        device = _active.get("device")
        _active.clear()

    if cast is not None:
        try:
            cast.media_controller.stop()
        except Exception:
            pass
        try:
            cast.disconnect(timeout=2)
        except Exception:
            pass

    if not quiet and device:
        return {"ok": True, "active": False, "device": device, "state": "IDLE"}
    return {"ok": True, "active": False, "device": None, "state": "IDLE"}


def content_type_for(url: str) -> str:
    path = str(url or "").split("?", 1)[0].lower()
    if path.endswith((".m3u8", ".m3u")):
        return "application/x-mpegURL"
    if path.endswith(".webm"):
        return "video/webm"
    if path.endswith(".mkv"):
        return "video/x-matroska"
    if path.endswith(".mov"):
        return "video/quicktime"
    return "video/mp4"
