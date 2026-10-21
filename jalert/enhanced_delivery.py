"""Durable article outbox and Chinese translation caching for Bark.

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
                "authors": [str(name) for name in (getattr(item, "authors", None) or []) if str(name).strip()][:30],
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
        usage = self.queue["usage"].setdefault(day, {"ai_attempts": 0})
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
        if item.get("_translation_retry_warning"):
            numbers = re.findall(r"\d+(?:\.\d+)?", str(abstract))
            system += (
                " 上次译文未通过数字或长度忠实性检查；请重新翻译全文。"
                "特别检查所有数字、化学式下标、参考编号、测量单位和百分比，"
                "不可用'若干'或'全部'代替实验数值。"
                "英文中的数值包括：" + "、".join(dict.fromkeys(numbers[:40])) + "。"
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
            accepted = False
            for name, base, model, key in endpoints:
                if name == "deepseek" and not key:
                    failures.append("deepseek: missing DEEPSEEK_API_KEY")
                    continue
                if not base or not model:
                    failures.append(f"{name}: missing API base URL or model")
                    continue
                # Reserve daily budget per HTTP call; allow just one quality-
                # targeted retry, never two blind network retries.
                for retry_index in range(2):
                    if self._usage(day)["ai_attempts"] >= max_calls:
                        raise RuntimeError(f"daily AI request budget exhausted ({max_calls}); translation queued")
                    self._usage(day)["ai_attempts"] += 1
                    self._save_queue()
                    attempt_item = dict(chunk_item)
                    if retry_index:
                        attempt_item["_translation_retry_warning"] = True
                    try:
                        translated = self._call_model(name, base, model, key, attempt_item)
                        self.cache["summaries"][part_signature] = {
                            "summary": translated, "provider": name, "model": model, "date": day,
                            "part": number + 1, "part_count": len(chunks)
                        }
                        self._save_cache()
                        result_parts.append(translated)
                        accepted = True
                        break
                    except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as ex:
                        message = f"{name}: {type(ex).__name__} ({str(ex)[:100]})"
                        failures.append(message)
                        self.log.warning("AI translation failed for %s part %d/%d: %s",
                                         _short(item["title"], 48), number + 1, len(chunks), message)
                        quality_issue = isinstance(ex, ValueError) and (
                            "translation quality check failed" in str(ex)
                            or "translation suspiciously short" in str(ex)
                            or "model output truncated" in str(ex))
                        if retry_index == 0 and quality_issue:
                            continue
                        break
                if accepted:
                    break
            if not accepted:
                raise RuntimeError("; ".join(failures))

        whole = "\n".join(result_parts)
        self.cache["summaries"][signature] = {
            "summary": whole, "provider": endpoints[0][0], "date": day,
            "mode": "literal-full-translation", **self._public_translation_meta(item, abstract),
        }
        self._save_cache()
        return whole

    # Legacy ServerChan batching was removed. Bark delivery now lives in
    # jalert.reliable_delivery and uses this class only for durable AI caching.
