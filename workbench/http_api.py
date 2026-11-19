#!/usr/bin/env python3
import json
import mimetypes
import os
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from workbench.publishers import PublisherError, publisher_for, validate_local_mcp_endpoint
from workbench.generators import GeneratorError, generate_with_profiles, validate_base_url
from workbench.store import DuplicatePublishError, Repository
from workbench.trends import TrendSourceError, agent_reach_status, scan_hotspots
from workbench.toapis import (
    REGION_ENDPOINTS, ToAPIsError, ToAPIsMCPClient, documented_parameter_names,
    download_result, endpoint_for_region, generation_fields, model_count, model_options,
    note_image_params, tool_text, validate_params_against_docs,
)
from workbench.multipart import PayloadTooLargeError, parse_multipart_stream
from workbench.runtime import (
    RuntimeConfigManager, generator_public_settings as runtime_generator_public_settings,
    load_generator_profiles, save_generator_profiles as save_runtime_generator_profiles,
)
from workbench.services import WorkbenchService


ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT / "web"
MAX_UPLOAD_BYTES = 300 * 1024 * 1024
DATA = Repository()
PUBLISH_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="xhs-publish")
_saved_settings = DATA.get_settings()
GENERATOR_PROVIDER_IDS = {"custom", "openai", "deepseek", "qwen", "aixhan"}


def _load_generator_profiles(settings):
    return load_generator_profiles(settings, validate_base_url, GENERATOR_PROVIDER_IDS)


GENERATOR_PROFILES_RUNTIME = _load_generator_profiles(_saved_settings)
GENERATOR_AUTO_FALLBACK = str(_saved_settings.get("generator_auto_fallback", "1")).lower() not in {"0", "false", "off"}
TOAPIS_RUNTIME = {
    "region": _saved_settings.get("toapis_region", "com"),
    "image_model": _saved_settings.get("toapis_image_model", ""),
    "api_key": os.environ.get("TOAPIS_API_KEY", ""),
}
RUNTIME = RuntimeConfigManager(GENERATOR_PROFILES_RUNTIME, GENERATOR_AUTO_FALLBACK, TOAPIS_RUNTIME)


def generator_public_settings():
    return runtime_generator_public_settings(RUNTIME)


def save_generator_profiles(value):
    global GENERATOR_PROFILES_RUNTIME, GENERATOR_AUTO_FALLBACK
    result = save_runtime_generator_profiles(value, RUNTIME, DATA, validate_base_url, GENERATOR_PROVIDER_IDS)
    GENERATOR_PROFILES_RUNTIME = RUNTIME.get_active_generator_credentials()
    GENERATOR_AUTO_FALLBACK = RUNTIME.get_auto_fallback()
    return result


def toapis_public_settings():
    runtime = RUNTIME.get_toapis()
    region = runtime["region"] if runtime["region"] in REGION_ENDPOINTS else "com"
    return {
        "region": region,
        "endpoint": REGION_ENDPOINTS[region],
        "configured": bool(runtime["api_key"]),
        "image_model": runtime["image_model"],
    }


def toapis_client():
    runtime = RUNTIME.get_toapis()
    region = runtime["region"] if runtime["region"] in REGION_ENDPOINTS else "com"
    return ToAPIsMCPClient(REGION_ENDPOINTS[region], runtime["api_key"])


def toapis_error_message(error):
    return RUNTIME.redact(str(error))[:500]


def _copy_asset_for_response(asset):
    public_asset = dict(asset)
    public_asset.pop("path", None)
    public_asset.pop("extension", None)
    return public_asset


def _copy_toapis_generation_for_response(record):
    public_record = dict(record)
    if public_record.get("asset"):
        public_record["asset"] = _copy_asset_for_response(public_record["asset"])
    return public_record


def _finish_toapis_generation(client_request_id, result):
    record = DATA.get_toapis_generation(client_request_id)
    fields = generation_fields(result)
    if fields["state"] == "failed":
        return DATA.save_toapis_generation(
            client_request_id, record["media_type"], record["model"], "failed",
            fields["task_id"] or record["task_id"], fields["poll_after_seconds"],
            error_text=fields["error"] or "ToAPIs 任务失败",
        )
    if fields["state"] != "completed":
        return DATA.save_toapis_generation(
            client_request_id, record["media_type"], record["model"], fields["state"],
            fields["task_id"] or record["task_id"], fields["poll_after_seconds"],
            error_text=fields["error"],
        )
    if not fields["asset_url"]:
        return DATA.save_toapis_generation(
            client_request_id, record["media_type"], record["model"], "delivery_error",
            fields["task_id"] or record["task_id"], fields["poll_after_seconds"],
            error_text="生成任务已完成，但响应中没有可下载的结果链接；请在 ToAPIs 任务列表核对，不要重复提交。",
        )
    try:
        max_bytes = 25 * 1024 * 1024 if record["media_type"] == "image" else 1024 * 1024 * 1024
        media_bytes, mime_type = download_result(fields["asset_url"], max_bytes=max_bytes)
        if record["media_type"] == "image":
            asset = DATA.save_generated_image(media_bytes, mime_type)
        else:
            asset = DATA.save_generated_video(media_bytes, mime_type, "ToAPIs 生成视频")
        return DATA.save_toapis_generation(
            client_request_id, record["media_type"], record["model"], "completed",
            fields["task_id"] or record["task_id"], fields["poll_after_seconds"], asset["id"],
        )
    except (ToAPIsError, ValueError) as exc:
        return DATA.save_toapis_generation(
            client_request_id, record["media_type"], record["model"], "delivery_error",
            fields["task_id"] or record["task_id"], fields["poll_after_seconds"],
            error_text="生成已完成，但保存结果失败：{}；请勿重新提交生成任务。".format(str(exc)[:300]),
        )


def start_toapis_generation(media_type, model, params, client_request_id, params_factory=None):
    created, existing = DATA.create_toapis_generation(client_request_id, media_type, model)
    if not created:
        return existing
    client = None
    try:
        client = toapis_client()
        listed = client.list_models(media_type)
        available = model_options(listed)
        model_listing = tool_text(listed)
        if ((available and not any(item["id"] == model for item in available))
                or (not available and model not in model_listing)):
            DATA.save_toapis_generation(
                client_request_id, media_type, model, "failed",
                error_text="所选模型未在当前 ToAPIs Key 的模型列表中；请先刷新列表并确认模型 ID。",
            )
            raise ValueError("当前 ToAPIs Key 的模型列表中没有该模型；请刷新模型列表确认模型 ID")
        docs = client.get_model_docs(model)
        if params_factory:
            params = params_factory(docs)
        validate_params_against_docs(params, docs)
        try:
            generated = client.generate(media_type, model, params, client_request_id)
        except ToAPIsError as exc:
            if exc.uncertain:
                return DATA.save_toapis_generation(
                    client_request_id, media_type, model, "unknown",
                    error_text="请求结果暂时无法确认。请查询这个任务状态，不要重新提交。",
                )
            DATA.save_toapis_generation(client_request_id, media_type, model, "failed", error_text=str(exc))
            raise
    except (ToAPIsError, ValueError) as exc:
        current = DATA.get_toapis_generation(client_request_id)
        if current and current["state"] == "starting":
            DATA.save_toapis_generation(
                client_request_id, media_type, model, "failed", error_text=str(exc),
            )
        raise
    finally:
        if client:
            client.close()

    fields = generation_fields(generated)
    DATA.save_toapis_generation(
        client_request_id, media_type, model, fields["state"], fields["task_id"],
        fields["poll_after_seconds"], error_text=fields["error"],
    )
    if fields["state"] == "completed":
        record = _finish_toapis_generation(client_request_id, generated)
    else:
        record = DATA.get_toapis_generation(client_request_id)
    return record


def run_publish(job_id, request, idempotency_key, publisher):
    try:
        outcome = publisher.publish(request, idempotency_key)
        status = outcome.get("status", "unknown_manual_check")
        DATA.update_publish_result(job_id, status, outcome.get("result", {}))
    except PublisherError as exc:
        status = "unknown_manual_check" if exc.uncertain else "failed"
        DATA.update_publish_result(job_id, status, {}, str(exc))
    except Exception as exc:
        status = "unknown_manual_check" if publisher.publisher_id != "mock" else "failed"
        DATA.update_publish_result(job_id, status, {}, "连接器异常：{}".format(exc))


SERVICE = WorkbenchService(DATA, PUBLISH_POOL, run_publish)


class WorkbenchHandler(BaseHTTPRequestHandler):
    server_version = "XHSWorkbench/0.1"

    def log_message(self, fmt, *args):
        # Keep the local console useful without logging request bodies or paths
        # that could contain local file names.
        print("[workbench] " + fmt % args)

    def _security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; media-src 'self'; connect-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )

    def _send_bytes(self, status, payload, content_type="application/json; charset=utf-8", extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, status, value):
        self._send_bytes(status, json.dumps(value, ensure_ascii=False).encode("utf-8"))

    def _error(self, status, message, **extra):
        payload = {"error": str(message)}
        payload.update(extra)
        self._json(status, payload)

    def _toapis_json(self, callback, status=200):
        try:
            value = callback()
        except ToAPIsError as exc:
            self._error(502, str(exc))
        except ValueError as exc:
            self._error(400, str(exc))
        else:
            self._json(status, value)

    def _read_json(self, max_bytes=2 * 1024 * 1024):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            raise ValueError("请求内容为空")
        if length > max_bytes:
            raise ValueError("请求内容过大")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("请求不是有效的 JSON")

    def _origin_is_local(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlparse(origin)
        hostname = (parsed.hostname or "").lower()
        return parsed.scheme == "http" and hostname in {"127.0.0.1", "localhost", "::1"}

    def _local_write_guard(self):
        if not self._origin_is_local():
            self._error(403, "拒绝非本机网页发起的写入请求")
            return False
        return True

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self._static("index.html", "text/html; charset=utf-8")
        elif path in {"/app.js", "/styles.css"} or path.startswith(("/core/", "/views/")):
            name = path.lstrip("/")
            if name.endswith(".js"):
                ctype = "application/javascript; charset=utf-8"
            elif name.endswith(".css"):
                ctype = "text/css; charset=utf-8"
            else:
                self._error(404, "没有找到该文件")
                return
            self._static(name, ctype)
        elif path == "/api/bootstrap":
            self._json(200, {
                "account": DATA.get_account(),
                "assets": DATA.list_assets(),
                "jobs": DATA.list_jobs(),
                "settings": DATA.get_settings(),
                "monitor": DATA.get_monitor_history(20),
                "generator": generator_public_settings(),
                "toapis": toapis_public_settings(),
                "toapis_generations": [
                    _copy_toapis_generation_for_response(item)
                    for item in DATA.list_toapis_generations(10)
                ],
            })
        elif path == "/api/monitor":
            self._json(200, {"history": DATA.get_monitor_history(30)})
        elif path == "/api/generator/settings":
            self._json(200, generator_public_settings())
        elif path == "/api/toapis/settings":
            self._json(200, toapis_public_settings())
        elif path == "/api/toapis/models":
            media_type = urllib.parse.parse_qs(urlparse(self.path).query).get("type", ["image"])[0]
            def read_models():
                with toapis_client() as client:
                    value = client.list_models(media_type)
                    count = model_count(value)
                    return {
                        "models": tool_text(value),
                        "model_options": model_options(value),
                        "model_count": count,
                        "has_models": count > 0,
                    }
            self._toapis_json(read_models)
        elif path == "/api/toapis/balance":
            def read_balance():
                with toapis_client() as client:
                    return {"balance": tool_text(client.get_balance())}
            self._toapis_json(read_balance)
        elif path == "/api/toapis/check":
            try:
                with toapis_client() as client:
                    results = {}
                    model_results = {}
                    for media_type in ("image", "video"):
                        try:
                            value = client.list_models(media_type)
                            model_results[media_type] = {
                                "ok": True,
                                "value": tool_text(value),
                                "model_options": model_options(value),
                                "count": model_count(value),
                            }
                        except (ToAPIsError, ValueError) as exc:
                            model_results[media_type] = {
                                "ok": False,
                                "error": toapis_error_message(exc),
                            }
                    results["models"] = {
                        "ok": all(item["ok"] for item in model_results.values()),
                        "has_models": any(item.get("count", 0) > 0 for item in model_results.values()),
                        "total_count": sum(item.get("count", 0) for item in model_results.values()),
                        "by_type": model_results,
                        "schema": client.list_models_schema(),
                        "type_options": client.list_models_type_options(),
                        "value": "\n\n".join(
                            "{}模型：\n{}".format("图片" if media_type == "image" else "视频", item["value"])
                            for media_type, item in model_results.items() if item["ok"]
                        ),
                    }
                    model_errors = [item["error"] for item in model_results.values() if not item["ok"]]
                    if model_errors:
                        results["models"]["error"] = "；".join(model_errors)
                    try:
                        results["balance"] = {"ok": True, "value": tool_text(client.get_balance())}
                    except (ToAPIsError, ValueError) as exc:
                        results["balance"] = {"ok": False, "error": toapis_error_message(exc)}
                self._json(200, {
                    "configured": True,
                    "connected": any(item["ok"] for item in results.values()),
                    **results,
                })
            except (ToAPIsError, ValueError) as exc:
                self._json(200, {
                    "configured": bool(RUNTIME.get_toapis()["api_key"]),
                    "connected": False,
                    "models": {"ok": False, "error": toapis_error_message(exc)},
                    "balance": {"ok": False, "error": toapis_error_message(exc)},
                })
        elif path.startswith("/api/toapis/generations/"):
            client_request_id = unquote(path.rsplit("/", 1)[-1])
            record = DATA.get_toapis_generation(client_request_id)
            if not record:
                self._error(404, "没有找到 ToAPIs 生成任务")
                return
            if record["state"] in {"starting", "running", "unknown"}:
                try:
                    with toapis_client() as client:
                        result = client.get_generation(client_request_id)
                    record = _finish_toapis_generation(client_request_id, result)
                except ToAPIsError as exc:
                    self._error(502, str(exc))
                    return
                except ValueError as exc:
                    self._error(400, str(exc))
                    return
            self._json(200, _copy_toapis_generation_for_response(record))
        elif path == "/api/trends/status":
            self._json(200, {"agent_reach": agent_reach_status(), "aihot": {"available": True, "name": "AIHOT"}})
        elif path.startswith("/api/jobs/"):
            job_id = path.rsplit("/", 1)[-1]
            job = DATA.get_job(job_id)
            self._json(200, job) if job else self._error(404, "没有找到该任务")
        elif path.startswith("/media/"):
            self._serve_media(path.rsplit("/", 1)[-1])
        else:
            self._error(404, "没有找到该页面")

    def do_POST(self):
        if not self._local_write_guard():
            return
        path = urlparse(self.path).path
        try:
            if path == "/api/account":
                value = self._read_json()
                result = DATA.save_account(value.get("display_name"), value.get("profile"))
                self._json(200, result)
            elif path == "/api/settings":
                value = self._read_json()
                mode = value.get("publisher_mode")
                if mode not in {"mock", "xiaohongshu-mcp"}:
                    raise ValueError("发布模式无效")
                endpoint = validate_local_mcp_endpoint(
                    value.get("mcp_endpoint") or "http://127.0.0.1:18060/mcp"
                )
                saved = DATA.save_settings({"publisher_mode": mode, "mcp_endpoint": endpoint})
                self._json(200, saved)
            elif path == "/api/generator/settings":
                value = self._read_json()
                if "profiles" not in value:
                    # Preserve compatibility with the former single-profile settings API.
                    current = RUNTIME.get_active_generator_credentials()
                    profile_id = current[0]["id"] if current else "legacy-default"
                    metadata = {
                        "id": profile_id,
                        "name": current[0].get("name", "默认文案模型") if current else "默认文案模型",
                        "provider": value.get("provider", "custom"),
                        "base_url": value.get("base_url") or "https://api.openai.com/v1",
                        "protocol": value.get("protocol", "chat_completions"),
                        "model": value.get("model", ""),
                    }
                    profiles = [metadata]
                    profiles.extend({key: item[key] for key in ("id", "name", "provider", "base_url", "protocol", "model")}
                                    for item in current[1:])
                    value = {
                        "profiles": profiles,
                        "auto_fallback": RUNTIME.get_auto_fallback(),
                        "api_key_profile_id": profile_id,
                        "api_key": value.get("api_key", ""),
                        "clear_api_key": value.get("clear_api_key") is True,
                    }
                self._json(200, save_generator_profiles(value))
            elif path == "/api/toapis/settings":
                value = self._read_json()
                region = str(value.get("region") or "com").strip()
                endpoint_for_region(region)
                old_runtime = RUNTIME.get_toapis()
                old_region = old_runtime["region"]
                api_key = str(value.get("api_key") or "").strip()[:2000]
                image_model = "" if region != old_region else str(
                    value.get("image_model", old_runtime["image_model"]) or ""
                ).strip()[:160]
                if value.get("clear_api_key") is True or (region != old_region and not api_key):
                    api_key = ""
                elif api_key:
                    pass
                else:
                    api_key = old_runtime["api_key"]
                RUNTIME.save_toapis({"region": region, "image_model": image_model, "api_key": api_key})
                TOAPIS_RUNTIME.update(RUNTIME.get_toapis())
                DATA.save_settings({"toapis_region": region, "toapis_image_model": image_model})
                self._json(200, toapis_public_settings())
            elif path == "/api/toapis/models/docs":
                value = self._read_json()
                model = str(value.get("model") or "").strip()[:160]
                if not model:
                    raise ValueError("请填写 ToAPIs 模型名称")
                with toapis_client() as client:
                    docs = client.get_model_docs(model)
                self._json(200, {
                    "model": model,
                    "docs": tool_text(docs),
                    "parameter_names": sorted(documented_parameter_names(docs)),
                })
            elif path.startswith("/api/toapis/generations/") and path.endswith("/retry-download"):
                client_request_id = unquote(path.split("/")[-2])
                record = DATA.get_toapis_generation(client_request_id)
                if not record:
                    self._error(404, "没有找到 ToAPIs 生成任务")
                    return
                if record["state"] == "completed" and record.get("asset"):
                    self._json(200, _copy_toapis_generation_for_response(record))
                    return
                if record["state"] != "delivery_error":
                    raise ValueError("只有生成完成但下载失败的任务可以重试下载")
                with toapis_client() as client:
                    result = client.get_generation(client_request_id)
                fields = generation_fields(result)
                if fields["state"] != "completed":
                    record = DATA.save_toapis_generation(
                        client_request_id, record["media_type"], record["model"], fields["state"],
                        fields["task_id"] or record["task_id"], fields["poll_after_seconds"],
                        error_text=fields["error"] or "无法重新读取已完成任务的结果；请先在 ToAPIs 控制台核对。",
                    )
                else:
                    record = _finish_toapis_generation(client_request_id, result)
                self._json(200, _copy_toapis_generation_for_response(record))
            elif path == "/api/toapis/generations":
                value = self._read_json()
                account = DATA.get_account()
                if not account["configured"]:
                    self._error(409, "请先完成账号定位再生成媒体")
                    return
                media_type = str(value.get("media_type") or "").strip()
                model = str(value.get("model") or "").strip()[:160]
                params = value.get("params")
                if media_type not in {"image", "video"}:
                    raise ValueError("请选择图片或视频生成")
                if not model:
                    raise ValueError("请填写 ToAPIs 模型名称")
                if not isinstance(params, dict):
                    raise ValueError("模型参数必须是 JSON 对象")
                client_request_id = str(value.get("client_request_id") or "").strip()
                if not re.fullmatch(r"toapis-[A-Za-z0-9-]{10,96}", client_request_id):
                    raise ValueError("生成任务编号无效，请重新提交")
                record = start_toapis_generation(media_type, model, params, client_request_id)
                self._json(202, _copy_toapis_generation_for_response(record))
            elif path == "/api/note-images/generate":
                value = self._read_json()
                account = DATA.get_account()
                if not account["configured"]:
                    self._error(409, "请先完成账号定位再生成笔记配图")
                    return
                runtime = RUNTIME.get_toapis()
                model = runtime["image_model"]
                if not runtime["api_key"] or not model:
                    self._error(409, "请先在服务设置中连接 ToAPIs 并选择笔记配图模型")
                    return
                prompt = str(value.get("prompt") or "").strip()[:4000]
                aspect_ratio = str(value.get("aspect_ratio") or "2:3").strip()
                if aspect_ratio not in {"2:3", "1:1", "3:2"}:
                    raise ValueError("图片比例无效")
                client_request_id = str(value.get("client_request_id") or "").strip()
                if not re.fullmatch(r"toapis-[A-Za-z0-9-]{10,96}", client_request_id):
                    raise ValueError("生成任务编号无效，请重新提交")
                record = start_toapis_generation(
                    "image", model, None, client_request_id,
                    params_factory=lambda docs: note_image_params(prompt, aspect_ratio, docs),
                )
                self._json(202, _copy_toapis_generation_for_response(record))
            elif path == "/api/generator/test":
                value = self._read_json()
                result = self._generate({
                    "idea": "测试模型连接",
                    "brief": "请只返回简短的标题、正文和话题，不要使用事实数据。",
                    "sources": [],
                }, preferred_profile_id=value.get("profile_id"), allow_fallback=False)
                self._json(200, {
                    "ok": True,
                    "message": "模型生成测试成功",
                    "sample": result["draft"],
                    "profile_name": result["profile_name"],
                    "model": result["model"],
                })
            elif path == "/api/assets":
                self._upload_asset()
            elif path == "/api/connector/check":
                settings = DATA.get_settings()
                publisher = publisher_for(settings["publisher_mode"], settings["mcp_endpoint"])
                result = publisher.validate({})
                self._json(200, result)
            elif path == "/api/monitor/refresh":
                settings = DATA.get_settings()
                if settings["publisher_mode"] != "xiaohongshu-mcp":
                    self._error(409, "请先在平台连接设置中启用本机 xiaohongshu-mcp")
                    return
                publisher = publisher_for(settings["publisher_mode"], settings["mcp_endpoint"])
                snapshot = publisher.read_account_snapshot()
                self._json(200, DATA.save_monitor_snapshot(snapshot))
            elif path == "/api/research/search":
                value = self._read_json()
                account = DATA.get_account()
                if not account["configured"]:
                    self._error(409, "请先完成账号定位再开展选题研究")
                    return
                settings = DATA.get_settings()
                if settings["publisher_mode"] != "xiaohongshu-mcp":
                    self._error(409, "请先连接本机 xiaohongshu-mcp")
                    return
                publisher = publisher_for(settings["publisher_mode"], settings["mcp_endpoint"])
                results = publisher.search_notes(value.get("keyword", ""))
                self._json(200, {"results": results, "source": "小红书站内搜索"})
            elif path == "/api/trends/scan":
                value = self._read_json()
                account = DATA.get_account()
                if not account["configured"]:
                    self._error(409, "请先完成账号定位再扫描热点")
                    return
                query = str(value.get("query") or account["profile"].get("content_lane") or "").strip()
                result = scan_hotspots(query, value.get("channels"))
                self._json(200, result)
            elif path == "/api/generate":
                value = self._read_json()
                account = DATA.get_account()
                if not account["configured"]:
                    self._error(409, "请先完成账号定位再生成文案")
                    return
                result = self._generate(value)
                self._json(200, {
                    "draft": result["draft"],
                    "persona_version": account["current_profile_version"],
                    "model": result["model"],
                    "profile_name": result["profile_name"],
                    "fallback_used": result["fallback_used"],
                    "fallback_reason": result["fallback_reason"],
                })
            elif path == "/api/publish":
                self._start_publish()
            elif path.startswith("/api/jobs/") and path.endswith("/verification"):
                job_id = path.split("/")[-2]
                value = self._read_json()
                job = SERVICE.verify_publish_job(job_id, value.get("outcome"), value.get("note", ""))
                self._json(200, job)
            else:
                self._error(404, "没有找到该接口")
        except DuplicatePublishError as exc:
            self._error(409, "相同内容已有发布任务，请先核对已有任务，避免重复发布。", job_id=exc.job_id)
        except GeneratorError as exc:
            self._error(502, str(exc))
        except ToAPIsError as exc:
            self._error(502, str(exc))
        except TrendSourceError as exc:
            self._error(502, str(exc))
        except PublisherError as exc:
            self._error(502, str(exc))
        except PayloadTooLargeError as exc:
            self.close_connection = True
            self._error(413, str(exc))
        except ValueError as exc:
            self._error(400, str(exc))
        except Exception as exc:
            self._error(500, "工作台处理失败：{}".format(exc))

    def _generate(self, value):
        account = DATA.get_account()
        return generate_with_profiles(
            RUNTIME.get_active_generator_credentials(),
            account["profile"],
            value.get("idea", ""),
            value.get("brief", ""),
            value.get("sources", []),
            auto_fallback=RUNTIME.get_auto_fallback() if value.get("allow_fallback") is not False else False,
            preferred_profile_id=value.get("preferred_profile_id"),
        )

    def _upload_asset(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            self._error(400, "没有收到视频文件")
            return
        if length > MAX_UPLOAD_BYTES + 256 * 1024:
            self.close_connection = True
            self._error(413, "视频不能超过 300 MB")
            return
        try:
            parts = parse_multipart_stream(
                self.rfile, self.headers, length, max_bytes=MAX_UPLOAD_BYTES,
            )
        except (PayloadTooLargeError, ValueError):
            self.close_connection = True
            raise
        item = parts[0]
        try:
            asset = DATA.save_asset(item.filename, item.file, item.content_type)
            asset.pop("path", None)
            asset.pop("extension", None)
            self._json(201, asset)
        finally:
            item.close()

    def _start_publish(self):
        value = self._read_json()
        if value.get("confirmed") is not True:
            self._error(400, "发布前需要明确确认目标账号和内容")
            return
        settings = DATA.get_settings()
        account = DATA.get_account()
        publisher = publisher_for(settings["publisher_mode"], settings["mcp_endpoint"])
        content_type = value.get("content_type", "note")
        if content_type not in {"note", "video"}:
            raise ValueError("内容类型无效")
        asset_ids = value.get("asset_ids") if isinstance(value.get("asset_ids"), list) else []
        asset_ids = list(dict.fromkeys(str(asset_id) for asset_id in asset_ids if asset_id))[:9]
        if not asset_ids and value.get("asset_id"):
            asset_ids = [value["asset_id"]]
        assets = [DATA.get_asset(asset_id) for asset_id in asset_ids]
        if not assets or any(asset is None for asset in assets):
            raise ValueError("请先生成至少一张笔记配图")
        asset = assets[0]
        if settings["publisher_mode"] == "xiaohongshu-mcp":
            validate_local_mcp_endpoint(settings["mcp_endpoint"])
        if settings["publisher_mode"] == "xiaohongshu-mcp":
            # Validate endpoint before creating a task. The actual MCP publish
            # call remains isolated in the connector adapter.
            validate_local_mcp_endpoint(settings["mcp_endpoint"])
        if content_type == "note" and any(item["media_type"] != "image" for item in assets):
            raise ValueError("图文笔记需要使用生成的图片素材")
        if content_type == "video" and (len(assets) != 1 or asset["media_type"] != "video"):
            raise ValueError("视频内容需要一个视频素材")
        request = {
            "account_id": account["id"],
            "account_name": account["display_name"],
            "asset_id": asset["id"],
            "video_path": asset["path"],
            "image_paths": [item["path"] for item in assets] if content_type == "note" else [],
            "asset_ids": asset_ids,
            "content_type": content_type,
            "title": value.get("title", ""),
            "body": value.get("body", ""),
            "tags": value.get("tags", []),
            "source_refs": value.get("source_refs", []),
        }
        job, idempotency_key = DATA.create_publish_job(
            request["asset_id"], request["title"], request["body"],
            request["tags"], publisher.publisher_id, request["source_refs"],
            asset_ids=request["asset_ids"], content_type=content_type,
        )
        SERVICE.enqueue_publish(job, request, idempotency_key, publisher)
        self._json(202, job)

    def _static(self, name, content_type):
        target = (WEB_DIR / name).resolve()
        try:
            target.relative_to(WEB_DIR.resolve())
        except ValueError:
            self._error(404, "没有找到该文件")
            return
        if not target.is_file():
            self._error(404, "没有找到该文件")
            return
        payload = target.read_bytes()
        self._send_bytes(200, payload, content_type, {"Cache-Control": "no-cache"})

    def _serve_media(self, asset_id):
        if not re.fullmatch(r"[a-f0-9]{32}", asset_id):
            self._error(404, "没有找到该视频")
            return
        path, asset = DATA.asset_file(asset_id)
        if not path:
            self._error(404, "没有找到该视频")
            return
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        byte_range = self.headers.get("Range")
        if byte_range:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", byte_range.strip())
            if not match:
                self._send_bytes(416, b"", "text/plain", {"Content-Range": "bytes */{}".format(size)})
                return
            left, right = match.groups()
            if left:
                start = int(left)
                end = int(right) if right else size - 1
            elif right:
                count = int(right)
                start = max(0, size - count)
            if start >= size or end < start:
                self._send_bytes(416, b"", "text/plain", {"Content-Range": "bytes */{}".format(size)})
                return
            end = min(end, size - 1)
            status = 206
        length = end - start + 1
        ctype = mimetypes.guess_type(asset["original_name"])[0] or asset["mime_type"]
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "private, max-age=3600")
        if status == 206:
            self.send_header("Content-Range", "bytes {}-{}/{}".format(start, end, size))
        self._security_headers()
        self.end_headers()
        with path.open("rb") as video:
            video.seek(start)
            remaining = length
            while remaining:
                chunk = video.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)


def main():
    port = int(os.environ.get("XHS_WORKBENCH_PORT", "8765"))
    server = ThreadingHTTPServer(("127.0.0.1", port), WorkbenchHandler)
    server.daemon_threads = True
    print("小红书工作台已启动：http://127.0.0.1:{}".format(port))
    print("数据目录：{}".format(DATA.data_dir))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n工作台已停止")
    finally:
        server.server_close()
        PUBLISH_POOL.shutdown(wait=False)


if __name__ == "__main__":
    main()
