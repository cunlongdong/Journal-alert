"""Reliable independent per-article Bark and paginated WeChat delivery.

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
    return {t.lower() for t in re.findall(r"(?<![A-Za-z0-9])(?:\d+(?:\.\d+)?)(?:\s*[×x]\s*10\s*\^?\s*[-+]?\d+)?%?(?![A-Za-z0-9])", text)}


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
    """Bark-only, one paper per accepted notification.

    The publication and translated-title caches survive restarts. No message is
    sent if the source abstract or verified Chinese translation is unavailable.
    """

    def __init__(self, root: Path, log: logging.Logger | None = None):
        super().__init__(root, log)
        self.dual_path = self.root / "state" / STATE_NAME
        if self.dual_path.is_file():
            self.dual = json.loads(self.dual_path.read_text(encoding="utf-8"))
        else:
            self.dual = {"version": 1, "next_id": 1, "active": None, "progress": {}, "usage": {}}
        if self.dual.get("version") != 1 or not isinstance(self.dual.get("usage"), dict):
            raise ValueError("Unrecognized Bark delivery state: refusing overwrite")
        # Previously Bark-acknowledged papers must not be resent solely because
        # the now-removed WeChat channel still showed them as pending.
        changed = False
        for item in self.queue["items"].values():
            if item.get("status") == "pending" and item.get("bark_done"):
                item["status"] = "sent"
                changed = True
        if changed:
            self._save_queue()

    def _persist(self):
        _atomic_json(self.dual_path, self.dual)
        self._save_queue()

    def _call_model(self, name: str, base: str, model: str, key: str, item: dict) -> str:
        translated = super()._call_model(name, base, model, key, item)
        check = quality_report(str(item.get("abstract") or ""), translated)
        if not check["ok"]:
            raise ValueError("translation quality check failed: " + str(check["missing_numbers"][:8]))
        if check["missing_acronyms_advisory"]:
            self.log.warning("Translated acronyms warrant review: %s", check["missing_acronyms_advisory"])
        return translated

    def _get_translation(self, item: dict, day: str) -> str:
        abstract = str(item.get("abstract") or "").strip()
        if not abstract:
            raise ValueError("English source abstract missing; source lookup required")
        translated = self._summary(item, day)
        check = quality_report(abstract, translated)
        if not check["ok"]:
            raise ValueError("Cached translation failed fidelity checks: " + str(check["missing_numbers"][:8]))
        return translated

    @staticmethod
    def _url(item: dict) -> str:
        if item.get("doi"):
            return "https://doi.org/" + str(item["doi"]).strip()
        return str(item.get("url") or "")

    @staticmethod
    def _slice_bytes(source: str, budget: int) -> str:
        size = 0
        out = []
        for ch in source:
            cost = len(ch.encode("utf-8"))
            if size + cost > budget:
                break
            out.append(ch)
            size += cost
        return "".join(out)

    def _cap(self, channels=("bark",)) -> int:
        return max(500, min(2600, int(os.getenv("JALERT_BARK_BODY_BYTES", "2400"))))

    def _translate_title(self, item: dict, day: str) -> str:
        import hashlib
        import time
        from jalert.enhanced_delivery import _int_env, DEFAULTS

        title = str(item.get("title") or "").strip()
        if not title:
            raise ValueError("Missing source article title")
        key = hashlib.sha256(title.encode("utf-8")).hexdigest()
        cache = self.cache.setdefault("titles", {})
        old = cache.get(key)
        if old and old.get("zh_title"):
            return old["zh_title"]
        budget = _int_env("JALERT_AI_MAX_CALLS_DAY", DEFAULTS["max_ai_calls_day"], 1, 200)
        errors = []
        for name, base, model, secret in self._model_endpoints():
            if not base or not model or (name == "deepseek" and not secret):
                errors.append(name + ": missing AI settings")
                continue
            if self._usage(day)["ai_attempts"] >= budget:
                raise RuntimeError(f"AI daily request budget exhausted ({budget}), title pending")
            self._usage(day)["ai_attempts"] += 1
            self._save_queue()  # reserve before HTTP
            endpoint = base.rstrip("/")
            if not endpoint.endswith("/chat/completions"):
                endpoint += "/chat/completions"
            payload = {
                "model": model,
                "temperature": 0.1,
                "max_tokens": 256,
                "stream": False,
                "messages": [
                    {"role": "system", "content": "你是自旋电子学论文标题英译中译者。只输出忠实、完整的中文论文标题；保留材料化学式、学术缩写、数字和单位，不添加解释。"},
                    {"role": "user", "content": title},
                ],
            }
            if name == "custom" and model.lower().startswith("deepseek-v4.1-flash"):
                payload["reasoning_effort"] = "none"
            if name == "deepseek":
                payload["thinking"] = {"type": "disabled"}
            headers = {"Content-Type": "application/json", "Accept": "application/json"}
            if secret:
                headers["Authorization"] = "Bearer " + secret
            req = urllib.request.Request(endpoint, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                         headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=90) as response:
                    answer = json.loads(response.read(512000).decode("utf-8"))
                choice = answer["choices"][0]
                if choice.get("finish_reason") == "length":
                    raise ValueError("title translation truncated")
                zh = str(choice["message"]["content"]).strip().strip('"“”')
                if len(re.findall(r"[\u4e00-\u9fff]", zh)) < 2:
                    raise ValueError("title translation contains no meaningful Chinese")
                if _critical_tokens(title) - _critical_tokens(zh):
                    raise ValueError("title translation omitted important numerals")
            except (OSError, RuntimeError, ValueError, KeyError, TypeError, IndexError) as exc:
                errors.append(f"{name}: {type(exc).__name__}")
                self.log.warning("Title translation deferred: %s %s", title[:65], type(exc).__name__)
                continue
            cache[key] = {"title": title, "zh_title": zh, "created": day}
            # The existing bilingual card enhancer uses complete-summary cache
            # records, so attach the title translation to those records too.
            for rec in self.cache.get("summaries", {}).values():
                if rec.get("mode") == "literal-full-translation" and rec.get("title") == title:
                    rec["zh_title"] = zh
            self._save_cache()
            return zh
        raise RuntimeError("Title translation unavailable: " + "; ".join(errors))

    def _bark_article(self, item: dict, day: str) -> tuple[str, str]:
        # Translate before marking any paper as sent. Never silently replace
        # Chinese with English because an AI limit was exhausted.
        chinese = self._get_translation(item, day)
        zh_title = self._translate_title(item, day)
        item.pop("last_ai_error", None)
        item.pop("last_ai_error_at", None)
        title = "论文｜" + zh_title[:80]
        header = (f"中文标题：{zh_title}\n英文标题：{item.get('title','')}\n"
                  f"期刊：{item.get('journal','')}\n中文摘要（全文直译）：\n")
        footer = (f"\n论文链接：{self._url(item)}\n"
                  f"完整中英文摘要：{os.getenv('JALERT_REPORT_SITE_URL', SITE_URL)}")
        cap = self._cap()
        body = header + chinese + footer
        if _utf8size(body) > cap:
            note = "\n【摘要较长：请点击论文链接或双语网页查看完整版本】"
            room = cap - _utf8size(header + note + footer)
            if room <= 0:
                header = "中文标题：" + self._slice_bytes(zh_title, 135) + "\n"
                room = cap - _utf8size(header + note + footer)
            if room <= 0:
                raise ValueError("Bark notification cannot fit links")
            body = header + self._slice_bytes(chinese, room) + note + footer
        return title, body

    def _bark(self, title: str, body: str) -> bool:
        server = os.getenv("JALERT_BARK_SERVER", "https://api.day.app").rstrip("/")
        parsed = urllib.parse.urlparse(server)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("Bark server must use HTTPS")
        key = os.getenv("JALERT_BARK_DEVICE_KEY", "").strip()
        if not key:
            raise ValueError("BARK_DEVICE_KEY secret is missing")
        result = _http_json(server + "/push", {
            "device_key": key, "title": title, "body": body, "group": "Journal-alert",
            "url": os.getenv("JALERT_REPORT_SITE_URL", SITE_URL),
        })
        code = result.get("code")
        return code in (0, 200, "0", "200") or result.get("success") is True

    def _charge(self, day: str):
        state = self.dual["usage"].setdefault(day, {})
        state["bark_attempts"] = int(state.get("bark_attempts", 0)) + 1
        self._persist()

    def status(self) -> dict:
        day = _now_date()
        pending = [x for x in self.queue["items"].values() if x.get("status") == "pending"]
        return {
            "pending_papers": len(pending),
            "bark_papers_acknowledged": sum(bool(x.get("bark_done")) for x in self.queue["items"].values()),
            "bark_articles_waiting": len(pending),
            "pending_without_english": sum(not str(x.get("abstract") or "").strip() for x in pending),
            "channels_configured": {
                "bark_enabled": os.getenv("JALERT_BARK_ENABLED", "true").lower() in ("true", "1", "yes"),
                "bark_key_present": bool(os.getenv("JALERT_BARK_DEVICE_KEY", "").strip()),
            },
            "bark_attempts_today": int(self.dual["usage"].get(day, {}).get("bark_attempts", 0)),
            "ai_attempts_today": int(self._usage(day).get("ai_attempts", 0)),
            "recent_errors": [
                {"uid": x.get("uid"), "error": x.get("last_ai_error")}
                for x in pending if x.get("last_ai_error")
            ][:10],
        }

    def flush_both(self, *, send_func=None, max_pages: int = 10) -> dict:
        """Compatibility method name: now only sends individual Bark papers.

        send_func accepts (channel,title,body) for offline tests.
        max_pages does not constrain individual paper count; each run uses a
        separate bounded safety cap.
        """
        if os.getenv("JALERT_BARK_ENABLED", "true").lower() not in ("true", "1", "yes"):
            raise ValueError("Bark must be enabled")
        if send_func is None and not os.getenv("JALERT_BARK_DEVICE_KEY", "").strip():
            raise ValueError("BARK_DEVICE_KEY secret is missing")
        day = _now_date()
        attempted = sent = 0
        deferred = 0
        max_attempts = max(1, min(10000, int(os.getenv("JALERT_BARK_MAX_MESSAGES_RUN", "5000"))))
        for item in sorted(self.queue["items"].values(),
                           key=lambda x: (x.get("enqueued_at", ""), x.get("uid", ""))):
            if item.get("status") != "pending" or item.get("bark_done"):
                continue
            if attempted >= max_attempts:
                self.log.warning("Bark run safety cap reached (%d)", max_attempts)
                break
            try:
                title, body = self._bark_article(item, day)
            except (ValueError, RuntimeError, OSError) as exc:
                item["last_ai_error"] = str(exc)[:190]
                item["last_ai_error_at"] = day
                deferred += 1
                self._save_queue()
                # Stop repeated work on other titles if quota is exhausted.
                if "budget exhausted" in str(exc):
                    break
                continue
            self._charge(day)
            attempted += 1
            try:
                accepted = bool(send_func("bark", title, body)) if send_func else self._bark(title, body)
            except (OSError, RuntimeError, ValueError, urllib.error.URLError, KeyError) as exc:
                item["last_ai_error"] = f"Bark transmission: {type(exc).__name__}"
                accepted = False
            if accepted:
                item["bark_done"] = True
                item["status"] = "sent"
                # Keep full original abstract in the cache and generated report.
                # Queue metadata may be compacted after final acknowledgment.
                item.pop("abstract", None)
                sent += 1
            self._persist()
        result = self.status()
        result["bark_articles_sent_this_run"] = sent
        result["bark_articles_failed_this_run"] = attempted - sent
        result["bark_articles_deferred_this_run"] = deferred
        self.log.info("Bark-only Chinese delivery: %s", json.dumps(result, ensure_ascii=False))
        return result
