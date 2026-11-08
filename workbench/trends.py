"""Read-only trend discovery adapters for Agent-Reach and AIHOT."""

import concurrent.futures
import json
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


class TrendSourceError(Exception):
    pass


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


CHANNEL_ACTIONS = {
    "web": ("网页", "search"),
    "github": ("GitHub", "search"),
    "youtube": ("YouTube", "search"),
    "bilibili": ("B站", "search"),
    "v2ex": ("V2EX", "hot"),
    "reddit": ("Reddit", "search"),
    "twitter": ("X", "search"),
}
DEFAULT_CHANNELS = ("web", "github", "youtube", "bilibili", "v2ex")
PUBLIC_WEB_CHANNELS = {"reddit", "twitter"}


def _json_command(command, timeout=8):
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TrendSourceError("Agent-Reach 当前不可用：{}".format(exc))
    if result.returncode != 0:
        raise TrendSourceError((result.stderr or result.stdout or "Agent-Reach 执行失败")[:500])
    try:
        value = json.loads(result.stdout)
    except ValueError:
        raise TrendSourceError("Agent-Reach 返回了无法识别的数据")
    if not isinstance(value, dict) or value.get("ok") is False:
        error = value.get("error") if isinstance(value, dict) else None
        message = error.get("message") if isinstance(error, dict) else "搜索失败"
        raise TrendSourceError(str(message)[:500])
    return value


def _agent_command():
    return shutil.which("agent-reach") or str(Path.home() / ".local" / "bin" / "agent-reach")


def agent_reach_status():
    binary = _agent_command()
    try:
        value = _json_command([binary, "doctor"], timeout=8)
    except TrendSourceError as exc:
        return {"installed": False, "version": "", "channels": [], "message": str(exc)}
    result = value.get("result") or {}
    channels = result.get("channels") or []
    return {
        "installed": True,
        "version": str(result.get("version") or ""),
        "channels": [
            {
                "id": item.get("channel"),
                "name": CHANNEL_ACTIONS.get(item.get("channel"), (item.get("channel"), ""))[0],
                "status": item.get("status", "unknown"),
                "hint": item.get("hint", ""),
            }
            for item in channels if isinstance(item, dict) and item.get("channel") in CHANNEL_ACTIONS
        ],
        "message": "Agent-Reach {} 已安装".format(result.get("version") or ""),
    }


def _safe_url(value):
    value = str(value or "").strip()
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    # Remove session and signature tokens before showing or persisting a result.
    sensitive = {"xsec_token", "xsectoken", "token", "access_token", "signature"}
    query = [(key, val) for key, val in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
             if key.lower() not in sensitive]
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query), ""))


def _normalize_agent_items(channel, value):
    label = CHANNEL_ACTIONS[channel][0]
    result = value.get("result") or {}
    rows = value.get("items") or result.get("items") or []
    if not isinstance(rows, list):
        rows = []
    items = []
    for row in rows[:10]:
        if not isinstance(row, dict):
            continue
        raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
        title = str(row.get("title") or raw.get("title") or "").strip()[:300]
        url = _safe_url(row.get("url") or raw.get("url"))
        summary = str(row.get("text") or row.get("summary") or raw.get("description") or "").strip()[:1800]
        if not title and not summary:
            continue
        items.append({
            "id": "{}:{}".format(channel, url or title[:80]),
            "title": title or summary[:100],
            "url": url,
            "summary": summary,
            "source": label,
            "author": str(row.get("author") or raw.get("author") or "")[:150],
            "published_at": str(row.get("published_at") or raw.get("published") or "")[:80],
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
    return items


def _run_agent_channel(channel, query):
    label, action = CHANNEL_ACTIONS[channel]
    binary = _agent_command()
    command = [binary, channel, action]
    if action == "search":
        command.extend([query[:300], "--limit", "6"])
    else:
        command.extend(["--limit", "6"])
    if channel in PUBLIC_WEB_CHANNELS:
        # These optional/experimental channels have a documented public-web
        # backend that does not require installing their authenticated backend.
        command.extend(["--backend", "public-web"])
    command.extend(["--timeout", "30"])
    value = _json_command(command, timeout=36)
    items = _normalize_agent_items(channel, value)
    if not items:
        raise TrendSourceError("{} 暂无可用结果".format(label))
    return {"source": label, "status": "ok", "count": len(items), "items": items}


def _aihot_markdown(path, query=""):
    url = "https://aihot.news/api/v1/agent/" + path
    if query:
        url += "?" + urllib.parse.urlencode({"q": query[:100]})
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "aihot-skill/2.0.0 (+https://aihot.news/aihot-skill/)", "Accept": "text/markdown"},
        method="GET",
    )
    opener = urllib.request.build_opener(_NoRedirectHandler)
    try:
        with opener.open(request, timeout=10) as response:
            if response.geturl().split("/", 3)[2].lower() != "aihot.news":
                raise TrendSourceError("AIHOT 返回了非预期地址")
            payload = response.read(1024 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        raise TrendSourceError("AIHOT 暂时无法读取（HTTP {}）".format(exc.code))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TrendSourceError("AIHOT 暂时无法连接：{}".format(exc))
    if len(payload) > 1024 * 1024:
        raise TrendSourceError("AIHOT 返回内容过大")
    return payload.decode("utf-8", "replace")


def _parse_aihot(path, markdown):
    rows = []
    if "［AIHOT 不可信外部资料开始］" not in markdown:
        return rows
    section = markdown.split("［AIHOT 不可信外部资料开始］", 1)[1].split("［AIHOT 不可信外部资料结束］", 1)[0]
    starts = list(re.finditer(r"(?m)^(?:\d+\.|第\s*\d+\s*名：)\s*\[([^\]]+)\]\((https://aihot\.news/items/[^)]+)\)", section))
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(section)
        block = section[match.end():end]
        title = match.group(1).strip()
        details = []
        source_line = re.search(r"(?:公众号|来源|信源)：([^\n]+)", block)
        summary = re.search(r"(?:摘要|来龙去脉)：([^\n]+)", block)
        reason = re.search(r"推荐理由：([^\n]+)", block)
        original = re.search(r"原文：(https?://\S+)", block)
        if source_line:
            details.append(source_line.group(1).strip())
        if summary:
            details.append(summary.group(1).strip())
        if reason:
            details.append("推荐理由：" + reason.group(1).strip())
        rows.append({
            "id": "aihot:{}:{}".format(path, match.group(2).rsplit("/", 1)[-1]),
            "title": title[:300],
            "url": match.group(2),
            "summary": "\n".join(details)[:1800],
            "source": "AIHOT",
            "author": source_line.group(1).strip()[:150] if source_line else "AIHOT 精选",
            "published_at": "",
            "original_url": _safe_url(original.group(1)) if original else "",
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
    return rows[:10]


def _run_aihot(query):
    paths = [("hot", ""), ("latest", ""), ("search", query)]
    all_items = []
    errors = []
    for path, search in paths:
        if path == "search" and not search:
            continue
        try:
            markdown = _aihot_markdown(path, search)
            all_items.extend(_parse_aihot(path, markdown))
        except TrendSourceError as exc:
            errors.append(str(exc))
    deduped = []
    seen = set()
    for item in all_items:
        key = item["url"] or item["title"]
        if key not in seen:
            seen.add(key)
            deduped.append(item)
    if not deduped:
        raise TrendSourceError("；".join(errors) or "AIHOT 暂无结果")
    return {"source": "AIHOT", "status": "ok", "count": len(deduped), "items": deduped[:20]}


def scan_hotspots(query, channels=None):
    query = str(query or "").strip()[:300]
    if not query:
        raise ValueError("请先填写要扫描的主题或账号赛道")
    status = agent_reach_status()
    available = {row["id"] for row in status.get("channels", []) if row["status"] != "not_added"}
    requested = DEFAULT_CHANNELS if channels is None else channels
    if not isinstance(requested, list):
        requested = []
    chosen = []
    unavailable = []
    for channel in requested:
        if not isinstance(channel, str) or channel not in CHANNEL_ACTIONS or channel in chosen:
            continue
        if channel in PUBLIC_WEB_CHANNELS or channel in available:
            chosen.append(channel)
        else:
            label = CHANNEL_ACTIONS[channel][0]
            unavailable.append({
                "source": label,
                "status": "not_available",
                "count": 0,
                "message": "渠道尚未安装或当前不可用",
                "items": [],
            })
    providers = [("AIHOT", lambda: _run_aihot(query))]
    providers.extend((channel, lambda channel=channel: _run_agent_channel(channel, query)) for channel in chosen)
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(providers) or 1)) as pool:
        futures = {pool.submit(fn): channel for channel, fn in providers}
        for future in concurrent.futures.as_completed(futures):
            channel = futures[future]
            label = "AIHOT" if channel == "AIHOT" else CHANNEL_ACTIONS[channel][0]
            try:
                results.append(future.result())
            except Exception as exc:
                results.append({"source": label, "status": "error", "count": 0, "message": str(exc)[:500], "items": []})
    by_source = {item["source"]: item for item in results}
    ordered = [by_source[key] for key in ["AIHOT"] if key in by_source]
    ordered.extend(by_source[CHANNEL_ACTIONS[ch][0]] for ch in chosen if CHANNEL_ACTIONS[ch][0] in by_source)
    ordered.extend(unavailable)
    return {
        "query": query,
        "scanned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "agent_reach": status,
        "sources": [{key: row[key] for key in ("source", "status", "count", "message") if key in row} for row in ordered],
        "items": [item for source in ordered for item in source["items"]],
    }
