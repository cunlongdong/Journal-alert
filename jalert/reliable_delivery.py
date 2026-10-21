"""Reliable synchronized WeChat + Bark delivery for journal-alert.

Independent from the original seen.sqlite schema. Uses the original Delivery
translator and persistent cache, and stores immutable pages and channel-specific
acknowledgements in state/dual_channel_delivery.json.
"""
from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from jalert.enhanced_delivery import Delivery, _atomic_json, _now_date

STATE_NAME = "dual_channel_delivery.json"
SITE_URL = "https://cunlongdong.github.io/Journal-alert/"


def _utf8size(text: str) -> int:
    return len(text.encode("utf-8"))


def _critical_tokens(text: str) -> set[str]:
    """Numbers, ranges and measurement tokens that must not silently disappear."""
    text = unicodedata.normalize("NFKC", text).replace("−", "-").replace("–", "-")
    # Avoid false alarms from single-number citations in brackets when possible.
    text = re.sub(r"\[(?:\d+[\s,;–-]*)+\]", "", text)
    return {t.lower() for t in re.findall(r"(?<![A-Za-z])(?<!\d)(?:\d+(?:\.\d+)?)(?:\s*[×x]\s*10\s*\^?\s*[-+]?\d+)?%?", text)}


def quality_report(english: str, chinese: str) -> dict:
    """Conservative check, *not* a proof of semantic correctness."""
    en = str(english or "").strip()
    zh = str(chinese or "").strip()
    source_nums = _critical_tokens(en)
    target_nums = _critical_tokens(zh)
    missing = sorted(source_nums - target_nums)
    letters = len(re.findall(r"[A-Za-z]+", en))
    han = len(re.findall(r"[\u4e00-\u9fff]", zh))
    too_short = letters >= 65 and han < max(30, int(letters * 0.45))
    if not zh or (en and han < 12):
        too_short = True
    terms = [t for t in ("SOT", "STT", "MTJ", "DMI", "PMA", "MRAM") if re.search(r"\b"+t+r"\b", en, re.I) and not re.search(r"\b"+t+r"\b", zh, re.I)]
    return {"ok": not missing and not too_short, "missing_numbers": missing, "possibly_short": too_short, "missing_acronyms_advisory": terms}


def _http_json(url: str, payload: dict, *, form: bool = False, timeout: int = 30) -> dict:
    data = (urllib.parse.urlencode(payload).encode("utf-8") if form else json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    ct = "application/x-www-form-urlencoded" if form else "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": ct, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError("Notification API returned non-200")
        result = json.loads(response.read(128 * 1024).decode("utf-8"))
    if not isinstance(result, dict):
        raise RuntimeError("Notification API did not return an object")
    return result


class ReliableDelivery(Delivery):
    def __init__(self, root: Path, log: logging.Logger | None = None):
        super().__init__(root, log)
        self.dual_path = self.root / "state" / STATE_NAME
        if self.dual_path.exists():
            self.dual = json.loads(self.dual_path.read_text(encoding="utf-8"))
        else:
            self.dual = {"version": 1, "next_id": 1, "active": None, "progress": {}, "usage": {}}
        if self.dual.get("version") != 1 or not isinstance(self.dual.get("progress"), dict):
            raise ValueError("Unexpected dual-channel state format; refusing to overwrite")

    def _persist(self):
        _atomic_json(self.dual_path, self.dual)
        self._save_queue()

    def _call_model(self, name: str, base: str, model: str, key: str, item: dict) -> str:
        value = super()._call_model(name, base, model, key, item)
        q = quality_report(str(item.get("abstract", "")), value)
        if not q["ok"]:
            raise ValueError("translation quality check failed: missing_numbers=" + str(q["missing_numbers"][:8]) + ", suspiciously_short=" + str(q["possibly_short"]))
        if q["missing_acronyms_advisory"]:
            self.log.warning("Translation acronyms require review: %s", q["missing_acronyms_advisory"])
        return value

    def _get_translation(self, item: dict, day: str) -> str:
        if not item.get("abstract", "").strip():
            raise ValueError("English source abstract missing; cannot provide faithful full translation")
        translated = self._summary(item, day)
        check = quality_report(item["abstract"], translated)
        if not check["ok"]:
            raise ValueError("Cached translation failed critical checks: " + str(check["missing_numbers"][:8]))
        return translated

    def _channels(self) -> tuple[str, ...]:
        chans = []
        if os.getenv("JALERT_SERVERCHAN_SENDKEY", "").strip():
            chans.append("wechat")
        if os.getenv("JALERT_BARK_ENABLED", "false").lower() in ("true", "1", "yes"):
            if not os.getenv("JALERT_BARK_DEVICE_KEY", "").strip():
                raise ValueError("JALERT_BARK_ENABLED=true but BARK_DEVICE_KEY secret is missing")
            chans.append("bark")
        if not chans:
            raise ValueError("No push channel configured: add SERVERCHAN_SENDKEY and/or BARK_DEVICE_KEY")
        return tuple(chans)

    @staticmethod
    def _url(item: dict) -> str:
        if item.get("doi"):
            return "https://doi.org/" + item["doi"].strip()
        return str(item.get("url") or "")

    def _block(self, item: dict, translated: str) -> str:
        return (f"【{item.get('journal','')}】{item['title']}\n"
                f"中文摘要（全文直译）：\n{translated}\n"
                f"论文链接：{self._url(item)}\n\n")

    def _cap(self, channels: tuple[str, ...]) -> int:
        # APNs notifications are 4096 bytes including metadata; Bark JSON
        # requests must leave generous room for APS headers and app metadata.
        bark_cap = min(2600, int(os.getenv("JALERT_BARK_BODY_BYTES", "2400")))
        wechat_cap = int(os.getenv("JALERT_WECHAT_MAX_CHARS", "3500"))
        return min((bark_cap if "bark" in channels else 6400), wechat_cap * 2)

    @staticmethod
    def _slice_bytes(source: str, budget: int) -> str:
        used = 0
        chars = []
        for ch in source:
            b = len(ch.encode("utf-8"))
            if used + b > budget:
                break
            chars.append(ch)
            used += b
        return "".join(chars)

    def _make_page(self, channels: tuple[str, ...], day: str) -> dict | None:
        candidates = sorted((x for x in self.queue["items"].values() if x.get("status") == "pending"), key=lambda x: (x.get("enqueued_at", ""), -int(x.get("score", 0)), x["uid"]))
        if not candidates:
            return None
        seq = self.dual["next_id"]
        header = (f"自旋电子学中文文献日报｜第 {seq} 则\n"
                  f"【积压提醒】仍有 {len(candidates)} 篇待处理（含本条）；篇幅超限会续页发送，绝不截断。\n")
        footer = f"\n完整双语卡片墙：{os.getenv('JALERT_REPORT_SITE_URL', SITE_URL)}"
        cap = self._cap(channels)
        budget = cap - _utf8size(header + footer) - 22
        if budget < 280:
            raise ValueError("Push body byte budget too small")
        fragments = []
        advances = {}
        skipped = []
        for item in candidates:
            left = budget - _utf8size("".join(fragments))
            if left < 64:  # Do not call an AI model just to fill a few bytes.
                break
            uid = item["uid"]
            try:
                full = self._block(item, self._get_translation(item, day))
            except (ValueError, RuntimeError, OSError) as ex:
                item["last_ai_error"] = str(ex)[:190]
                item["last_ai_error_at"] = day
                skipped.append(uid)
                # AI allowance may be exhausted: continue using cached items if any.
                continue
            offset = int(self.dual["progress"].get(uid, 0))
            if offset >= len(full):
                raise ValueError("Stale translation progress beyond source length: " + uid)
            rem = full[offset:]
            # Keep exact source characters, never crop/deliberately summarize.
            part = self._slice_bytes(rem, left)
            if not part:
                break
            fragments.append(part)
            advances[uid] = offset + len(part)
            if len(part) != len(rem):
                break
        self._save_queue()
        if not fragments:
            self.log.warning("No translatable pending papers (count=%s); items remain queued; %d errors", len(candidates), len(skipped))
            return None
        body = header + "".join(fragments) + footer
        assert _utf8size(body) <= cap
        title = f"自旋电子学文献日报｜第 {seq} 则"
        message = {"id": seq, "title": title, "body": body,
                   "targets": list(channels), "acked": {x: False for x in channels},
                   "advances": advances, "created": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()}
        self.dual["next_id"] += 1
        self.dual["active"] = message
        self._persist()  # Persist the immutable page before attempting a network send.
        return message

    def _wechat(self, title: str, body: str) -> bool:
        key = os.environ.get("JALERT_SERVERCHAN_SENDKEY", "").strip()
        if not key:
            return False
        if key.lower().startswith("sctp"):
            match = re.match(r"^sctp(\d+)t", key, re.I)
            if not match:
                raise ValueError("Invalid ServerChan Turbo key")
            url = f"https://{match.group(1)}.push.ft07.com/send/{key}.send"
        else:
            url = f"https://sctapi.ftqq.com/{key}.send"
        data = _http_json(url, {"title": title, "desp": body}, form=True)
        return data.get("code") in (0, "0")

    def _bark(self, title: str, body: str) -> bool:
        server = os.environ.get("JALERT_BARK_SERVER", "https://api.day.app").rstrip("/")
        parsed = urllib.parse.urlparse(server)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("Bark server must be externally accessible HTTPS")
        key = os.environ["JALERT_BARK_DEVICE_KEY"]
        payload = {"device_key": key, "title": title, "body": body,
                   "group": "Journal-alert", "url": os.environ.get("JALERT_REPORT_SITE_URL", SITE_URL)}
        data = _http_json(server + "/push", payload)
        return data.get("code") in (0, 200, "0", "200") or data.get("success") is True

    def _send(self, channel: str, title: str, body: str) -> bool:
        return self._wechat(title, body) if channel == "wechat" else self._bark(title, body)

    def _limit(self, channel: str) -> int:
        if channel == "wechat":
            return max(1, min(5, int(os.getenv("JALERT_WECHAT_MAX_MESSAGES_DAY", "4"))))
        return max(1, min(30, int(os.getenv("JALERT_BARK_MAX_MESSAGES_DAY", "12"))))

    def _attempts(self, channel: str, day: str) -> int:
        if channel == "wechat":
            return int(self._usage(day).get("wechat_attempts", 0))
        return int(self.dual["usage"].setdefault(day, {}).get("bark_attempts", 0))

    def _charge(self, channel: str, day: str):
        if channel == "wechat":
            self._usage(day)["wechat_attempts"] += 1
        else:
            v = self.dual["usage"].setdefault(day, {})
            v["bark_attempts"] = int(v.get("bark_attempts", 0)) + 1
        self._persist()

    def status(self) -> dict:
        day = _now_date()
        active = self.dual.get("active")
        bark_on = os.getenv("JALERT_BARK_ENABLED", "false").lower() in ("true", "1", "yes")
        return {"pending_papers": self.pending_count(), "active_message": active["id"] if active else None,
                "channel_acks": active["acked"] if active else {},
                "channels_configured": {
                    "wechat": bool(os.getenv("JALERT_SERVERCHAN_SENDKEY", "").strip()),
                    "bark_enabled": bark_on,
                    "bark_key_present": bool(os.getenv("JALERT_BARK_DEVICE_KEY", "").strip()),
                },
                "wechat_attempts_today": self._attempts("wechat", day),
                "bark_attempts_today": self._attempts("bark", day),
                "recent_errors": [{"uid": x["uid"], "error": x.get("last_ai_error")}
                                  for x in self.queue["items"].values() if x.get("status") == "pending" and x.get("last_ai_error")][:8]}

    def flush_both(self, *, send_func=None, max_pages: int = 10) -> dict:
        """Send exactly the same immutable body to both channels, independently acked.

        Inject send_func(channel,title,body) for offline tests. If one channel
        fails/uses daily quota, no later page overtakes it.
        """
        day = _now_date()
        channels = self._channels() if send_func is None else ("wechat", "bark")
        completed = 0
        for _ in range(max_pages):
            active = self.dual.get("active")
            if active is None:
                if any(self._attempts(ch, day) >= self._limit(ch) for ch in channels):
                    break
                active = self._make_page(channels, day)
                if active is None:
                    break
            for ch in active["targets"]:
                if active["acked"].get(ch):
                    continue
                if self._attempts(ch, day) >= self._limit(ch):
                    continue
                self._charge(ch, day)  # reserve quota before HTTP to prevent loops
                try:
                    success = bool(send_func(ch, active["title"], active["body"])) if send_func else self._send(ch, active["title"], active["body"])
                except (OSError, RuntimeError, ValueError, urllib.error.URLError, urllib.error.HTTPError, KeyError) as ex:
                    self.log.warning("%s push failed: %s", ch, type(ex).__name__)
                    success = False
                if success:
                    active["acked"][ch] = True
                    self._persist()
            if not all(active["acked"].get(ch) for ch in active["targets"]):
                self.log.warning("Notification page %d still waiting for channels %s", active["id"], [ch for ch in active["targets"] if not active["acked"].get(ch)])
                break
            for uid, offset in active["advances"].items():
                item = self.queue["items"][uid]
                full = self._block(item, self._get_translation(item, day))
                if offset >= len(full):
                    item["status"] = "sent"
                    item.pop("abstract", None)
                    self.dual["progress"].pop(uid, None)
                    completed += 1
                else:
                    self.dual["progress"][uid] = offset
            self.dual["active"] = None
            self._persist()
        result = self.status()
        result["completed_papers_this_run"] = completed
        self.log.info("Reliable dual-channel push: %s", json.dumps(result, ensure_ascii=False))
        return result
