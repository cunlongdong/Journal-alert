"""Persistent Chinese-abstract delivery for journal-alert.

Independent of the existing state/seen.sqlite and original push pipeline.
Python >=3.10, standard library only. Never commits API keys.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROMPT_VERSION = "literal-translation-zh-v2"
DEFAULTS = {
    "max_messages": 5,             # ServerChan free tier hard ceiling
    "max_chars": 3500,             # conservative message length, incl. header
    "max_ai_calls_day": 25,        # maximum *attempts*, including failures/fallback
    "max_ai_tokens": 2200,         # tokens generated per source chunk
    "max_input_chars": 20000,    # refuse excessively long source, never silently cut
}
QUEUE_NAME = "enhanced_wechat_outbox.json"
CACHE_NAME = "enhanced_zh_cache.json"
SITE_URL = "https://cunlongdong.github.io/Journal-alert/"


def _atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _load_json(path: Path, defaults: dict) -> dict:
    if not path.exists():
        return json.loads(json.dumps(defaults))
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Refusing to overwrite corrupted JSON: {path}")
    return data


def _now_date() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def _short(x: str, n: int) -> str:
    text = re.sub(r"\s+", " ", str(x or "")).strip()
    return text if len(text) <= n else text[:n-1].rstrip() + "…"


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        return min(max(int(os.getenv(name, str(default))), minimum), maximum)
    except ValueError:
        raise ValueError(f"Invalid integer environment variable: {name}") from None


class Delivery:
    def __init__(self, root: Path, log: logging.Logger | None = None):
        self.root = Path(root)
        self.log = log or logging.getLogger("journal-alert-enhanced")
        self.queue_path = self.root / "state" / QUEUE_NAME
        self.cache_path = self.root / "state" / CACHE_NAME
        self.queue = _load_json(self.queue_path, {"version": 1, "items": {}, "usage": {}})
        self.cache = _load_json(self.cache_path, {"version": 1, "summaries": {}})
        if not isinstance(self.queue.get("items"), dict) or not isinstance(self.queue.get("usage"), dict):
            raise ValueError("Delivery queue file has unexpected structure; not overwriting")
        if not isinstance(self.cache.get("summaries"), dict):
            raise ValueError("Summary cache has unexpected structure; not overwriting")

    def _save_queue(self) -> None:
        _atomic_json(self.queue_path, self.queue)

    def _save_cache(self) -> None:
        _atomic_json(self.cache_path, self.cache)

    def enqueue(self, entries: list[tuple[object, object]]) -> int:
        """Called only with newly accepted original-DB entries, not old history."""
        count = 0
        for item, scored in entries:
            uid = str(item.uid)
            if uid in self.queue["items"]:
                continue
            self.queue["items"][uid] = {
                "uid": uid,
                "title": str(item.title or ""),
                "journal": str(item.journal or ""),
                "date": str(item.date or ""),
                "doi": str(item.doi or ""),
                "url": str(item.url or ""),
                "abstract": str(item.abstract or ""),
                "topics": list(scored.labels or []),
                "score": int(scored.score),
                "enqueued_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
                "status": "pending",
                "attempts": 0,
            }
            count += 1
        if count:
            self._save_queue()
        self.log.info("enhanced outbox: added=%d pending=%d", count, self.pending_count())
        return count

    def pending_count(self) -> int:
        return sum(1 for x in self.queue["items"].values() if x.get("status") == "pending")

    def _usage(self, day: str) -> dict:
        usage = self.queue["usage"].setdefault(day, {"wechat_attempts": 0, "ai_attempts": 0})
        # Avoid unbounded usage history while preserving recent accounting.
        if len(self.queue["usage"]) > 45:
            for old in sorted(self.queue["usage"])[:-40]:
                self.queue["usage"].pop(old, None)
        return usage

    def _model_endpoints(self) -> list[tuple[str, str, str, str]]:
        def endpoint(name: str) -> tuple[str, str, str, str]:
            if name == "deepseek":
