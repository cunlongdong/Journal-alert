"""WeChat push adapters: ServerChan, PushPlus and Bark.

Each channel is optional and silently skipped until its key/token is filled in
``config.json``. Every adapter reports a structured result instead of raising,
so one broken channel never blocks the run.
"""

from __future__ import annotations

import json
import urllib.parse

from .fetch import FetchError, http_request


def _result(channel: str, ok: bool, detail: str) -> dict:
    return {"channel": channel, "ok": ok, "detail": detail[:300]}


def push_serverchan(sendkey: str, title: str, body: str) -> dict:
    url = f"https://sctapi.ftqq.com/{sendkey}.send"
    data = urllib.parse.urlencode({"title": title, "desp": body}).encode("utf-8")
    try:
        status, raw = http_request(
            url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
    except FetchError as exc:
        return _result("serverchan", False, str(exc))
    text = raw.decode("utf-8", errors="replace")
    if status != 200:
        return _result("serverchan", False, f"HTTP {status}: {text[:160]}")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return _result("serverchan", False, "Invalid JSON response")
    if not isinstance(payload, dict):
        return _result("serverchan", False, "Response must be a JSON object")
    code = payload.get("code")
    ok = type(code) in (int, str) and code in (0, "0")
    return _result("serverchan", bool(ok), f"code={code} {payload.get('message', '')}")


def push_pushplus(token: str, title: str, body: str) -> dict:
    url = "https://www.pushplus.plus/send"
    data = json.dumps(
        {"token": token, "title": title, "content": body, "template": "markdown"},
        ensure_ascii=False,
    ).encode("utf-8")
    try:
        status, raw = http_request(
            url, data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
    except FetchError as exc:
        return _result("pushplus", False, str(exc))
    text = raw.decode("utf-8", errors="replace")
    if status != 200:
        return _result("pushplus", False, f"HTTP {status}: {text[:160]}")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return _result("pushplus", False, "Invalid JSON response")
    if not isinstance(payload, dict):
        return _result("pushplus", False, "Response must be a JSON object")
    code = payload.get("code")
    ok = code in (200, "200")
    return _result("pushplus", bool(ok), f"code={code} {payload.get('msg', '')}")


def push_bark(server: str, key: str, title: str, body: str, sound: str = "bird") -> dict:
    url = f"{server.rstrip('/')}/{key}"
    data = json.dumps(
        {"title": title, "body": body, "sound": sound, "group": "文献日报"}, ensure_ascii=False
    ).encode("utf-8")
    try:
        status, raw = http_request(
            url, data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
    except FetchError as exc:
        return _result("bark", False, str(exc))
    text = raw.decode("utf-8", errors="replace")
    if status != 200:
        return _result("bark", False, f"HTTP {status}: {text[:160]}")
    try:
        payload = json.loads(text)
        ok = isinstance(payload, dict) and payload.get("code") in (200, "200")
    except json.JSONDecodeError:
        ok = False

def dispatch(cfg: dict, title: str, body: str, log) -> list[dict]:
    """Send the digest to every enabled-and-configured channel.

    A channel that is enabled but still has an empty key/token is reported as a
    *skip* rather than a failure, so an unconfigured channel never looks like a
    broken one.
    """
    push_cfg = cfg.get("push", {})
    channels = push_cfg.get("channels", {})
    results: list[dict] = []

    def _unconfigured(name: str, field: str) -> None:
        log.info("push %-12s skipped: %s not configured yet", name, field)

    serverchan = channels.get("serverchan", {})
    if serverchan.get("enabled"):
        if serverchan.get("sendkey"):
            results.append(push_serverchan(serverchan["sendkey"], title, body))
        else:
            _unconfigured("serverchan", "sendkey")

    pushplus = channels.get("pushplus", {})
    if pushplus.get("enabled"):
        if pushplus.get("token"):
            results.append(push_pushplus(pushplus["token"], title, body))
        else:
            _unconfigured("pushplus", "token")

    bark = channels.get("bark", {})
    if bark.get("enabled"):
        if bark.get("key"):
            results.append(
                push_bark(
                    bark.get("server", "https://api.day.app"),
                    bark["key"],
                    title,
                    body,
                    bark.get("sound", "bird"),
                )
            )
        else:
            _unconfigured("bark", "key")

    for result in results:
        if result["ok"]:
            log.info("push %-12s OK   %s", result["channel"], result["detail"])
        else:
            log.warning("push %-12s FAIL %s", result["channel"], result["detail"])
    return results
