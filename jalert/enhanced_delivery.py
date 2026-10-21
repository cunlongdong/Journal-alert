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
                return ("deepseek", "https://api.deepseek.com", os.getenv("DEEPSEEK_MODEL", "deepseek-flash"), os.getenv("DEEPSEEK_API_KEY", ""))
            if name == "custom":
                return ("custom", os.getenv("CUSTOM_AI_BASE_URL", ""), os.getenv("CUSTOM_AI_MODEL", ""), os.getenv("CUSTOM_AI_API_KEY", ""))
            raise ValueError("JALERT_AI_PRIMARY must be deepseek or custom")
        primary = os.getenv("JALERT_AI_PRIMARY", "deepseek").strip().lower()
        fallback = os.getenv("JALERT_AI_FALLBACK", "none").strip().lower()
        if fallback not in ("none", "deepseek", "custom"):
            raise ValueError("JALERT_AI_FALLBACK must be none/deepseek/custom")
        result = [endpoint(primary)]
        if fallback != "none" and fallback != primary:
            result.append(endpoint(fallback))
        return result

    def _call_model(self, name: str, base: str, model: str, key: str, item: dict) -> str:
        if not model or not base:
            raise ValueError(f"{name}: base URL/model missing")
        parsed = urllib.parse.urlparse(base)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError(f"{name}: HTTPS public base URL required (GitHub Actions cannot reach localhost)")
        if name == "deepseek" and not key:
            raise ValueError("DEEPSEEK_API_KEY secret missing")
        endpoint = base.rstrip("/")
        if not endpoint.endswith("/chat/completions"):
            endpoint += "/chat/completions"
        title = _short(item.get("title", ""), 300)
        abstract = item.get("abstract", "")
        system = (
            "你是科研论文摘要的英译中译者，不是摘要撰写者。"
            "任务是对所给英文 Abstract 的【全部文字】进行忠实、完整、逐句中文翻译，"
            "严格按照原文信息顺序，不得总结、概括、缩写、跳过句子、删除限定条件、"
            "改动数值及单位、添加推测或引入原文没有的结论。"
            "专业术语按自旋电子学学术用法翻译，SOT/STT/MTJ、公式、化学式、参考编号保持准确。"
            "保留否定、比较、误差、条件和不确定性表达。"
            "只输出这段英文文本对应的中文译文，不写解释、标题、摘要点评或 Markdown。"
        )
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": f"英文论文标题：{title}\n英文Abstract：{abstract}"},
            ],
            "temperature": 0.1,
            "max_tokens": _int_env("JALERT_AI_MAX_TOKENS", DEFAULTS["max_ai_tokens"], 1200, 4096),
            "stream": False,
        }
        # DeepSeek enables costly/thinking mode by default in 2026; short faithful
        # abstract translations should use the non-thinking endpoint behavior.
        if name == "deepseek":
            payload["thinking"] = {"type": "disabled"}
        # Preserve the already-working no-reasoning setting used by the user's gateway.
        if name == "custom" and model.lower().startswith("deepseek-v4.1-flash"):
            payload["reasoning_effort"] = "none"
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if key:
            headers["Authorization"] = "Bearer " + key
        request = urllib.request.Request(endpoint, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=_int_env("JALERT_AI_TIMEOUT_SECONDS", 90, 15, 180)) as response:
                result = json.loads(response.read(1024 * 1024).decode("utf-8"))
        except urllib.error.HTTPError as ex:
            raise RuntimeError(f"{name} HTTP {ex.code}") from None
        except (urllib.error.URLError, TimeoutError) as ex:
            raise RuntimeError(f"{name} network error: {type(ex).__name__}") from None
        try:
            choice = result["choices"][0]
            value = choice["message"]["content"].strip()
            if choice.get("finish_reason") == "length":
                raise ValueError("model output truncated")
        except (KeyError, IndexError, TypeError, AttributeError):
            # Minimal safe diagnostics: schema only; never print response text or tokens.
            choices = result.get("choices") if isinstance(result, dict) else None
            first = choices[0] if isinstance(choices, list) and choices else None
            msg = first.get("message") if isinstance(first, dict) else None
            content_type = type(msg.get("content")).__name__ if isinstance(msg, dict) else "missing"
            finish = first.get("finish_reason") if isinstance(first, dict) else "missing"
            raise RuntimeError(f"{name}: malformed completion response; content_type={content_type}; finish_reason={finish}") from None
        value = re.sub(r"\s+", " ", value).strip(' \n\t"“”')
        chinese_count = len(re.findall(r"[\u4e00-\u9fff]", value))
        english_word_count = len(re.findall(r"[A-Za-z]+(?:-[A-Za-z]+)*", abstract))
        # Heuristic catches a clearly short summary, but cannot certify translation fidelity.
        if chinese_count < max(12, int(english_word_count * 0.75)):
            raise ValueError(f"{name}: translation suspiciously short ({chinese_count} Chinese chars for {english_word_count} English words)")
        return value

    @staticmethod
    def _split_abstract(abstract: str, max_chars: int = 1800) -> list[str]:
        """Chunk only at nearby English sentence or word boundaries; never omit text."""
        segments = []
        remainder = abstract.strip()
        while len(remainder) > max_chars:
            prefix = remainder[:max_chars]
            # Prefer completing a sentence, but avoid cutting very small fragments.
            options = [m.end() for m in re.finditer(r"[.!?][\"')\]]*\s+", prefix)]
            usable = [x for x in options if x >= max_chars // 2]
            cut = usable[-1] if usable else prefix.rfind(" ", max_chars // 2)
            if cut <= 0:
                cut = max_chars
            segments.append(remainder[:cut].strip())
            remainder = remainder[cut:].lstrip()
        if remainder:
            segments.append(remainder)
        return segments

    @staticmethod
    def _public_translation_meta(item: dict, abstract: str) -> dict:
        """Only PUBLIC paper metadata. Never persist the model API key.

        This repository is public: full translations/abstracts become public via
        cache and the GitHub Pages page. Review publisher terms if relevant.
        """
        return {
            "uid": str(item.get("uid") or ""),
            "doi": str(item.get("doi") or ""),
            "title": str(item.get("title") or ""),
            "english_abstract": abstract,
        }

    def _summary(self, item: dict, day: str) -> str:
        """Exact-scope translation, no summarization. Cache full and per-chunk results."""
        abstract = str(item.get("abstract", "")).strip()
        if not abstract:
            return "原始数据源未提供英文摘要，无法进行中文直译。请打开原文核查。"
        if len(abstract) > DEFAULTS["max_input_chars"]:
            raise ValueError("source abstract too long for safe translation; left pending, not truncated")
        signature = hashlib.sha256((PROMPT_VERSION + "\n" + item["title"] + "\n" + abstract).encode("utf-8")).hexdigest()
        cached = self.cache["summaries"].get(signature)
        if cached:
            # Older cache entries are still usable. Attach public article metadata
            # so the card wall can find the corresponding full translation.
            if not cached.get("title") and cached.get("mode") == "literal-full-translation":
                cached.update(self._public_translation_meta(item, abstract))
                self._save_cache()
            return cached["summary"]
        chunks = self._split_abstract(abstract)
        result_parts = []
        max_calls = _int_env("JALERT_AI_MAX_CALLS_DAY", DEFAULTS["max_ai_calls_day"], 1, 200)
        endpoints = self._model_endpoints()
        for number, chunk in enumerate(chunks):
            part_signature = hashlib.sha256((signature + "\npart\n" + str(number) + "\n" + chunk).encode("utf-8")).hexdigest()
            chunk_cached = self.cache["summaries"].get(part_signature)
            if chunk_cached:
                result_parts.append(chunk_cached["summary"])
                continue
            failures = []
            chunk_item = dict(item)
            chunk_item["abstract"] = chunk
            for name, base, model, key in endpoints:
                if name == "deepseek" and not key:
                    failures.append("deepseek: missing DEEPSEEK_API_KEY")
                    continue
                if not base or not model:
                    failures.append(f"{name}: missing API base URL or model")
                    continue
                if self._usage(day)["ai_attempts"] >= max_calls:
                    raise RuntimeError(f"daily AI request budget exhausted ({max_calls}); translation queued")
                self._usage(day)["ai_attempts"] += 1
                self._save_queue()
                try:
                    translated = self._call_model(name, base, model, key, chunk_item)
                    self.cache["summaries"][part_signature] = {
                        "summary": translated, "provider": name, "model": model, "date": day,
                        "part": number + 1, "part_count": len(chunks)
                    }
                    self._save_cache()
                    result_parts.append(translated)
                    break
                except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as ex:
                    failures.append(f"{name}: {type(ex).__name__} ({str(ex)[:100]})")
                    self.log.warning("AI translation failed for %s part %d/%d: %s", _short(item["title"], 48), number + 1, len(chunks), failures[-1])
            else:
                raise RuntimeError("; ".join(failures))
        whole = "\n".join(result_parts)
        self.cache["summaries"][signature] = {
            "summary": whole, "provider": endpoints[0][0], "date": day,
            "mode": "literal-full-translation", **self._public_translation_meta(item, abstract),
        }
        self._save_cache()
        return whole

    def _block(self, item: dict, summary: str) -> str:
        link = ("https://doi.org/" + item["doi"] if item.get("doi") else item.get("url", ""))
        title = _short(item["title"], 180).replace("[", "\\[").replace("]", "\\]")
        label = _short("、".join(item.get("topics", [])), 100)
        heading = f"**{title}**"
        if link.startswith("https://") or link.startswith("http://"):
            heading = f"[{title}]({link.replace(')', '%29')})"
        return (f"{heading}  \n"
                f"{_short(item['journal'], 85)} · {item['date']} · {label}  \n"
                f"**中文摘要直译：** {summary}")

    def _send(self, sendkey: str, title: str, body: str) -> bool:
        # Legacy SCT key, and newer sctp key variant advertised in ServerChan docs.
        if sendkey.lower().startswith("sctp"):
            m = re.match(r"^sctp(\d+)t", sendkey, re.I)
            if not m:
                raise RuntimeError("Malformed sctp SendKey")
            endpoint = f"https://{m.group(1)}.push.ft07.com/send/{sendkey}.send"
        else:
            endpoint = f"https://sctapi.ftqq.com/{sendkey}.send"
        body_data = urllib.parse.urlencode({"title": title, "desp": body}).encode("utf-8")
        req = urllib.request.Request(endpoint, data=body_data, headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                if response.status != 200:
                    self.log.warning("ServerChan HTTP %s", response.status)
                    return False
                data = json.loads(response.read(131072).decode("utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("Notification response must be a JSON object")
                code = data.get("code")
                if type(code) in (int, str) and code in (0, "0"):
                    return True
                self.log.warning("ServerChan non-success code=%s", str(data.get("code"))[:30])
                return False
        except urllib.error.HTTPError as ex:
            self.log.warning("ServerChan HTTP %d (message stays pending)", ex.code)
            return False
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as ex:
            # Avoid printing error URL: it contains the SendKey.
            self.log.warning("ServerChan response uncertain (%s); message stays pending", type(ex).__name__)
            return False

    def flush(self, send_func=None) -> dict:
        """Send oldest pending papers in bounded batches; status saved after each result.

        send_func(title, body) is a test seam. A remote acknowledgement isn't
        proof of final mobile delivery. A network timeout may have delivered;
        retry can duplicate, but will never silently erase a queued article.
        """
        day = _now_date()
        limit = _int_env("JALERT_WECHAT_MAX_MESSAGES_DAY", DEFAULTS["max_messages"], 1, 5)
        max_chars = _int_env("JALERT_WECHAT_MAX_CHARS", DEFAULTS["max_chars"], 1000, 10000)
        key = os.getenv("JALERT_SERVERCHAN_SENDKEY", "")
        if not key and send_func is None:
            self.log.warning("SERVERCHAN_SENDKEY missing; all papers remain queued")
            return {"sent": 0, "pending": self.pending_count(), "reason": "missing_sendkey"}
        sent = 0
        blocked_this_run: set[str] = set()
        usage = self._usage(day)
        while usage["wechat_attempts"] < limit:
            pending = sorted((x for x in self.queue["items"].values() if x.get("status") == "pending" and x["uid"] not in blocked_this_run), key=lambda x: (x["enqueued_at"], -x["score"], x["uid"]))
            if not pending:
                break
            blocks: list[str] = []
            selected: list[dict] = []
            header = f"自旋电子学文献速递 · {day}\n\n"
            # Keep page link visible in every batch.
            footer = f"\n\n[在线论文卡片墙]({os.getenv('JALERT_REPORT_SITE_URL', SITE_URL)})"
            for item in pending:
                try:
                    summary = self._summary(item, day)
                except (ValueError, RuntimeError, OSError) as ex:
                    # A single untranslatable article must not starve all later
                    # papers. Retain it for the next run and send other articles.
                    blocked_this_run.add(item["uid"])
                    item["last_ai_error"] = _short(str(ex), 160)
                    item["last_ai_error_at"] = day
                    self._save_queue()
                    if self._usage(day)["ai_attempts"] >= _int_env("JALERT_AI_MAX_CALLS_DAY", DEFAULTS["max_ai_calls_day"], 1, 200):
                        self.log.warning("Daily AI budget reached; remaining translations will wait")
                        break
                    continue
                full_block = self._block(item, summary)
                offset = max(0, int(item.get("delivery_offset", 0)))
                if offset >= len(full_block):
                    # Safety: do not silently mark an inconsistent cursor as delivered.
                    blocked_this_run.add(item["uid"])
                    self.log.error("Outbox translation cursor inconsistent for %s", item["uid"])
                    continue
                continuation = f"**{_short(item['title'], 70)} · 译文续页**  \n" if offset else ""
                block = continuation + full_block[offset:]
                prospect = header + "\n\n".join(blocks + [block]) + footer
                if len(prospect) > max_chars:
                    if blocks:
                        break
                    budget = max_chars - len(header) - len(footer) - len(continuation) - 22
                    if budget < 200:
                        blocked_this_run.add(item["uid"])
                        self.log.error("JALERT_WECHAT_MAX_CHARS too small for a complete paper block")
                        continue
                    # Full translation is NOT shortened: it continues in the next message.
                    fragment = full_block[offset:offset + budget]
                    # Avoid breaking a word or translated sentence when possible.
                    for delimiter in ("。", "；", "，", " "):
                        pos = fragment.rfind(delimiter, budget // 2)
                        if pos > 0:
                            fragment = fragment[:pos + 1]
                            break
                    if not fragment:
                        fragment = full_block[offset:offset + budget]
                    block = continuation + fragment + "\n\n（本篇译文未完，下条继续）"
                    item["_next_offset"] = offset + len(fragment)
                else:
                    item.pop("_next_offset", None)
                blocks.append(block)
                selected.append(item)
                if item.get("_next_offset") is not None:
                    break
                if len(blocks) >= 15:
                    break
            if not selected:
                self.log.warning("No translated articles available for this batch; pending=%d", self.pending_count())
                break
            body = header + "\n\n".join(blocks) + footer
            title = f"自旋电子学日报 {day}｜{len(selected)} 篇中文摘要｜待发 {len(pending)} 篇"
            # Reserve a request BEFORE network IO. Even failures count toward a safe daily ceiling.
            usage["wechat_attempts"] += 1
            self._save_queue()
            try:
                ok = bool(send_func(title, body)) if send_func else self._send(key, title, body)
            except Exception as ex:
                self.log.warning("Send failed (%s); batch remains pending", type(ex).__name__)
                ok = False
            if not ok:
                for item in selected:
                    item.pop("_next_offset", None)
                    item["attempts"] = int(item.get("attempts", 0)) + 1
                    item["last_attempt"] = day
                self._save_queue()
                break
            for item in selected:
                next_offset = item.pop("_next_offset", None)
                if next_offset is not None:
                    item["delivery_offset"] = next_offset
                    # Keep pending: the full text has not been delivered yet.
                else:
                    item["status"] = "sent"
                    item.pop("delivery_offset", None)
                    item["sent_at"] = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
                    item.pop("abstract", None)  # less long-term full-text storage
            self._save_queue()
            fully_delivered = sum(1 for x in selected if x.get("status") == "sent")
            sent += fully_delivered
            self.log.info("Enhanced WeChat accepted %d article block(s) in 1 batch; fully delivered=%d; pending=%d", len(selected), fully_delivered, self.pending_count())
        result = {"sent": sent, "pending": self.pending_count(), "wechat_attempts_today": usage["wechat_attempts"], "ai_attempts_today": usage["ai_attempts"]}
        self.log.info("Enhanced daily delivery summary: %s", json.dumps(result, ensure_ascii=False))
        return result
