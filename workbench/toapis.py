"""Minimal Streamable HTTP MCP client for ToAPIs media tools."""

import json
import http.client
import ipaddress
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ToAPIsError(Exception):
    def __init__(self, message, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


REGION_ENDPOINTS = {
    "com": "https://api.toapis.com/mcp",
    "cn": "https://api.toapis.cn/mcp",
}


def endpoint_for_region(region):
    try:
        return REGION_ENDPOINTS[region]
    except KeyError:
        raise ValueError("ToAPIs 站点无效")


def _parse_response(body, content_type):
    if not body:
        return None
    text = body.decode("utf-8", "replace")
    if "text/event-stream" in (content_type or "").lower():
        messages = []
        data_lines = []
        for line in text.splitlines() + [""]:
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
            elif not line and data_lines:
                joined = "\n".join(data_lines)
                try:
                    messages.append(json.loads(joined))
                except ValueError:
                    pass
                data_lines = []
        if messages:
            return messages[-1]
        raise ToAPIsError("ToAPIs MCP 返回了无法识别的事件流")
    try:
        return json.loads(text)
    except ValueError:
        raise ToAPIsError("ToAPIs MCP 返回了无法识别的 JSON")


class ToAPIsMCPClient:
    """One short-lived MCP session; credentials stay on this instance only."""

    def __init__(self, endpoint, api_key, timeout=45):
        parsed = urllib.parse.urlparse(endpoint or "")
        if parsed.scheme != "https" or parsed.hostname not in {"api.toapis.com", "api.toapis.cn"}:
            raise ValueError("ToAPIs 地址必须使用官方 HTTPS MCP 服务")
        if parsed.path != "/mcp" or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("ToAPIs MCP 地址格式无效")
        if not api_key:
            raise ValueError("请先在服务设置中配置 ToAPIs API Key")
        self.endpoint = endpoint
        self.api_key = api_key
        self.timeout = timeout
        self.protocol_version = "2025-03-26"
        self.session_id = None
        self._request_id = 0
        self._tool_map = None
        self._opener = urllib.request.build_opener(_NoRedirectHandler)
        try:
            self._initialize()
        except Exception:
            self.close()
            raise

    def _post(self, payload, notification=False):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer " + self.api_key,
            "X-Locale": "zh",
            "User-Agent": "rednote-workbench/0.1.0 MCP-client",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
            headers["MCP-Protocol-Version"] = self.protocol_version
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                if response.status == 202 or notification:
                    return None
                body = response.read(8 * 1024 * 1024 + 1)
                if len(body) > 8 * 1024 * 1024:
                    raise ToAPIsError("ToAPIs MCP 响应超过 8 MB")
                self.session_id = response.headers.get("Mcp-Session-Id") or self.session_id
                return _parse_response(body, response.headers.get("Content-Type", ""))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                error_payload = json.loads(exc.read(2048).decode("utf-8", "replace"))
                detail = error_payload.get("detail") or error_payload.get("message") or error_payload.get("error", {}).get("message", "")
            except Exception:
                pass
            raise ToAPIsError("ToAPIs MCP 返回 HTTP {}{}".format(
                exc.code, ("：" + str(detail)[:240]) if detail else "",
            ), uncertain=exc.code >= 500)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ToAPIsError("无法连接 ToAPIs MCP：{}".format(exc), uncertain=True)

    def close(self):
        """Release a server-side Streamable HTTP session when one was created."""
        if not self.session_id:
            return
        request = urllib.request.Request(
            self.endpoint,
            headers={
                "Authorization": "Bearer " + self.api_key,
                "Mcp-Session-Id": self.session_id,
                "MCP-Protocol-Version": self.protocol_version,
                "User-Agent": "rednote-workbench/0.1.0 MCP-client",
            },
            method="DELETE",
        )
        try:
            with self._opener.open(request, timeout=min(self.timeout, 10)) as response:
                response.read(1024)
        except Exception:
            pass
        finally:
            self.session_id = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc_info):
        self.close()

    def _next_id(self):
        self._request_id += 1
        return self._request_id

    def _initialize(self):
        response = self._post({
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": self.protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "rednote-workbench", "version": "0.1.0"},
            },
        })
        if not isinstance(response, dict):
            raise ToAPIsError("ToAPIs MCP 初始化响应缺失")
        if response.get("error"):
            message = response["error"].get("message", "初始化失败")
            raise ToAPIsError("ToAPIs MCP 初始化失败：{}".format(str(message)[:240]))
        negotiated = (response.get("result") or {}).get("protocolVersion")
        if negotiated:
            self.protocol_version = negotiated
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, notification=True)

    @staticmethod
    def _text_from_tool_result(result):
        content = result.get("content", []) if isinstance(result, dict) else []
        return "\n".join(
            str(item.get("text", "")) for item in content
            if isinstance(item, dict) and item.get("type") == "text" and item.get("text")
        )

    def _call(self, name, arguments=None):
        response = self._post({
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        })
        if not isinstance(response, dict):
            raise ToAPIsError("ToAPIs 工具没有返回结果")
        if response.get("error"):
            error = response["error"]
            raise ToAPIsError("ToAPIs 工具调用失败：{}".format(str(error.get("message", "未知错误"))[:300]))
        result = response.get("result") or {}
        if result.get("isError"):
            message = self._text_from_tool_result(result) or "工具执行失败"
            raise ToAPIsError("ToAPIs 工具调用失败：{}".format(message[:300]))
        structured = result.get("structuredContent")
        if isinstance(structured, (dict, list)):
            return structured
        text_content = self._text_from_tool_result(result)
        if text_content:
            try:
                return json.loads(text_content)
            except ValueError:
                return {"text": text_content}
        return result

    def tools(self):
        if self._tool_map is None:
            response = self._post({
                "jsonrpc": "2.0", "id": self._next_id(), "method": "tools/list", "params": {},
            })
            if not isinstance(response, dict) or response.get("error"):
                raise ToAPIsError("无法读取 ToAPIs 工具列表")
            items = (response.get("result") or {}).get("tools", [])
            self._tool_map = {item.get("name"): item for item in items if isinstance(item, dict) and item.get("name")}
        return self._tool_map

    def _model_argument(self, tool_name, model):
        tool = self.tools().get(tool_name)
        schema = (tool or {}).get("inputSchema", {})
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        for name in ("model", "model_id", "model_name"):
            if name in properties:
                return {name: model}
        required = schema.get("required", []) if isinstance(schema, dict) else []
        if len(required) == 1:
            return {required[0]: model}
        raise ToAPIsError("ToAPIs 工具定义缺少模型参数")

    def list_models(self, media_type):
        if media_type not in {"image", "video"}:
            raise ValueError("读取模型时必须选择图片或视频类型")
        tool = self.tools().get("list_models")
        if not tool:
            raise ToAPIsError("ToAPIs 服务未提供 list_models 工具")
        schema = tool.get("inputSchema", {})
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        required = schema.get("required", []) if isinstance(schema, dict) else []
        arguments = {}
        if "type" in properties or "type" in required:
            type_schema = properties.get("type", {})
            enum_values = type_schema.get("enum", []) if isinstance(type_schema, dict) else []
            if enum_values:
                wanted = "image" if media_type == "image" else "video"
                normalized = {str(item).lower().replace("-", "_").replace(" ", "_"): item for item in enum_values}
                aliases = {
                    "image": ("image", "images", "image_generation", "image_generations", "img"),
                    "video": ("video", "videos", "video_generation", "video_generations"),
                }[wanted]
                match = next((normalized[item] for item in aliases if item in normalized), None)
                if match is None:
                    raise ToAPIsError(
                        "ToAPIs 模型列表不支持{}类型；服务端允许值：{}".format(
                            "图片" if wanted == "image" else "视频",
                            "、".join(str(item) for item in enum_values[:12]),
                        )
                    )
                arguments["type"] = match
            else:
                arguments["type"] = media_type
        return self._call("list_models", arguments)

    def list_models_type_options(self):
        tool = self.tools().get("list_models") or {}
        schema = tool.get("inputSchema", {})
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        type_schema = properties.get("type", {}) if isinstance(properties, dict) else {}
        enum_values = type_schema.get("enum", []) if isinstance(type_schema, dict) else []
        return [str(item)[:80] for item in enum_values[:12]]

    def list_models_schema(self):
        tool = self.tools().get("list_models") or {}
        schema = tool.get("inputSchema", {})
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        type_schema = properties.get("type", {}) if isinstance(properties, dict) else {}
        return {
            "required": [str(item)[:80] for item in schema.get("required", [])[:20]] if isinstance(schema, dict) else [],
            "type_enum": [str(item)[:80] for item in type_schema.get("enum", [])[:12]] if isinstance(type_schema, dict) else [],
            "properties": sorted(str(item)[:80] for item in properties)[:40] if isinstance(properties, dict) else [],
        }

    def get_balance(self):
        self._require_tool("get_balance")
        return self._call("get_balance")

    def get_model_docs(self, model):
        return self._call("get_model_docs", self._model_argument("get_model_docs", model))

    def generate(self, media_type, model, params, client_request_id):
        tool_name = "generate_image" if media_type == "image" else "generate_video"
        self._require_tool(tool_name)
        return self._call(tool_name, {
            "model": model,
            "params": params,
            "client_request_id": client_request_id,
        })

    def get_generation(self, client_request_id):
        tool = self.tools().get("get_generation")
        if not tool:
            raise ToAPIsError("ToAPIs 服务没有提供 get_generation 工具")
        schema = tool.get("inputSchema", {})
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        for name in ("client_request_id", "request_id", "id"):
            if name in properties:
                return self._call("get_generation", {name: client_request_id})
        return self._call("get_generation", {"client_request_id": client_request_id})

    def _require_tool(self, name):
        if name not in self.tools():
            raise ToAPIsError("ToAPIs 服务未提供 {} 工具".format(name))


def tool_text(value):
    if isinstance(value, dict):
        text = value.get("text")
        if isinstance(text, str):
            return text
        markdown = value.get("markdown")
        if isinstance(markdown, str):
            return markdown
        return json.dumps(value, ensure_ascii=False, indent=2)
    return json.dumps(value, ensure_ascii=False, indent=2)


def model_count(value):
    return len(model_options(value))


def model_options(value):
    """Return stable model IDs and display labels from common list shapes."""
    if isinstance(value, str):
        try:
            return model_options(json.loads(value))
        except ValueError:
            lines = [line.strip() for line in value.splitlines() if line.strip()]
            header = None
            options = []
            seen = set()
            for line in lines:
                if not line.startswith("|"):
                    continue
                columns = [cell.strip().strip("`* ") for cell in line.strip("|").split("|")]
                if all(set(cell) <= {"-", ":", " "} for cell in columns if cell):
                    continue
                normalized = [cell.lower().replace(" ", "_") for cell in columns]
                id_index = next((index for index, name in enumerate(normalized)
                                 if name in {"id", "model", "model_id", "model_name", "模型", "模型id", "模型名称"}), None)
                if id_index is not None:
                    header = (id_index, next((index for index, name in enumerate(normalized)
                                              if name in {"name", "display_name", "名称", "显示名称"}), id_index))
                    continue
                if header and max(header) < len(columns):
                    model_id = columns[header[0]]
                    label = columns[header[1]] or model_id
                    if model_id and model_id.lower() not in {"id", "model", "模型"} and model_id not in seen:
                        seen.add(model_id)
                        options.append({"id": model_id[:160], "label": label[:200]})
            if options:
                return options
            for line in lines:
                candidate = re.sub(r"^[\s>*-]+", "", line)
                candidate = re.sub(r"^(?:model(?:_id)?|模型(?:id|名称)?)\s*[:：]\s*", "", candidate, flags=re.I)
                if (re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{1,159}", candidate)
                        and candidate.lower() not in {"models", "available", "model", "image", "video"}
                        and candidate not in seen):
                    seen.add(candidate)
                    options.append({"id": candidate, "label": candidate})
            return options
    if isinstance(value, dict):
        for key in ("models", "data", "items", "results"):
            items = value.get(key)
            if isinstance(items, (dict, list)):
                return model_options(items)
        for key in ("text", "markdown"):
            if isinstance(value.get(key), str):
                parsed = model_options(value[key])
                if parsed:
                    return parsed
        return []
    if not isinstance(value, list):
        return []
    options = []
    seen = set()
    for item in value:
        if isinstance(item, str):
            model_id = item.strip()
            label = model_id
        elif isinstance(item, dict):
            model_id = next((str(item.get(key) or "").strip()
                             for key in ("id", "model_id", "model", "name", "slug")
                             if item.get(key)), "")
            label = str(item.get("name") or item.get("display_name") or model_id).strip()
        else:
            continue
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        options.append({"id": model_id[:160], "label": label[:200]})
    return options


def documented_parameter_names(docs):
    """Conservatively extract parameter names from model docs for allowlisting."""
    text = docs if isinstance(docs, str) else tool_text(docs)
    names = set()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("|"):
            columns = [cell.strip().strip("`* ") for cell in stripped.strip("|").split("|")]
            if columns and columns[0] and columns[0].lower() not in {"parameter", "name", "参数", "字段"}:
                candidate = columns[0]
                if candidate.replace("_", "").isalnum() and candidate.lower() not in {"---", "required", "type"}:
                    names.add(candidate)
        for marker in ("`", "**"):
            if stripped.startswith(marker):
                end = stripped.find(marker, len(marker))
                if end > len(marker):
                    candidate = stripped[len(marker):end]
                    if candidate.replace("_", "").isalnum():
                        names.add(candidate)
    if "prompt" in text.lower():
        names.add("prompt")
    return names


def validate_params_against_docs(params, docs):
    if not isinstance(params, dict) or not params:
        raise ValueError("请按所选模型参数说明填写参数 JSON")
    allowed = {name.lower() for name in documented_parameter_names(docs)}
    unknown = [str(name) for name in params if str(name).lower() not in allowed]
    if unknown:
        raise ValueError("参数说明中没有找到这些字段：{}".format("、".join(unknown[:8])))
    return params


def note_image_params(prompt, aspect_ratio, docs):
    """Build only parameters named by the selected model's documentation."""
    text = docs if isinstance(docs, str) else tool_text(docs)
    names = documented_parameter_names(docs)
    by_normalized_name = {name.lower().replace("_", "").replace("-", ""): name for name in names}
    prompt_name = by_normalized_name.get("prompt")
    if not prompt_name:
        raise ValueError("所选 ToAPIs 图片模型参数说明中没有 prompt 字段")
    params = {prompt_name: str(prompt or "").strip()[:4000]}
    if not params[prompt_name]:
        raise ValueError("请填写配图描述")

    ratio_values = {"2:3": "1024x1536", "1:1": "1024x1024", "3:2": "1536x1024"}
    aspect_name = next((by_normalized_name.get(candidate) for candidate in
                        ("aspectratio", "imageratio", "ratio") if by_normalized_name.get(candidate)), None)
    size_name = next((by_normalized_name.get(candidate) for candidate in
                      ("size", "imagesize") if by_normalized_name.get(candidate)), None)
    if aspect_name and re.search(r"(?<![0-9]){}(?![0-9])".format(re.escape(aspect_ratio)), text):
        params[aspect_name] = aspect_ratio
    elif size_name:
        size_value = ratio_values.get(aspect_ratio, ratio_values["2:3"])
        if size_value.lower() in text.lower():
            params[size_name] = size_value
    return params


def extract_result_url(value):
    url_keys = {"url", "image_url", "video_url", "image_urls", "video_urls", "result_url", "output_url", "download_url", "asset_url"}
    if isinstance(value, dict):
        for key, child in value.items():
            if key.lower() in url_keys:
                if isinstance(child, str) and child.startswith("https://"):
                    return child
                if isinstance(child, list):
                    for candidate in child:
                        if isinstance(candidate, str) and candidate.startswith("https://"):
                            return candidate
        for child in value.values():
            found = extract_result_url(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = extract_result_url(child)
            if found:
                return found
    elif isinstance(value, str) and value.startswith("{"):
        try:
            return extract_result_url(json.loads(value))
        except ValueError:
            return ""
    return ""


def generation_fields(value):
    if not isinstance(value, dict):
        value = {"text": value}
    state = str(value.get("state") or value.get("status") or "running").lower()
    if state in {"success", "succeeded", "done", "complete", "completed"}:
        state = "completed"
    elif state in {"error", "failure", "failed", "cancelled", "canceled"}:
        state = "failed"
    elif state in {"unknown", "not_found", "generation_not_found"}:
        state = "unknown"
    elif state not in {"running", "queued", "pending", "processing", "submitted"}:
        state = "unknown"
    if state in {"queued", "pending", "processing", "submitted"}:
        state = "running"
    return {
        "state": state,
        "task_id": str(value.get("task_id") or value.get("id") or "")[:200],
        "poll_after_seconds": max(1, min(int(value.get("poll_after_seconds") or value.get("poll_after") or 5), 60)),
        "asset_url": extract_result_url(value),
        "error": str(value.get("error") or value.get("message") or "")[:500],
    }


def _public_https_url(raw_url):
    parsed = urllib.parse.urlparse(raw_url or "")
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ToAPIsError("生成结果地址必须是安全的 HTTPS 链接")
    if parsed.port not in (None, 443):
        raise ToAPIsError("生成结果地址只允许使用 HTTPS 标准端口")
    host = parsed.hostname.lower().rstrip(".")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise ToAPIsError("生成结果地址不能指向本机")
    try:
        addresses = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            addresses = {ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
        except OSError as exc:
            raise ToAPIsError("无法解析生成结果地址：{}".format(exc))
    if not addresses or any(not address.is_global for address in addresses):
        raise ToAPIsError("生成结果地址解析到了非公网主机")
    return raw_url, sorted(str(address) for address in addresses)


def download_result(url, max_bytes, timeout=180):
    """Download a returned media URL with HTTPS, public-host, redirect and size checks."""
    current_url = url
    for redirects in range(6):
        current_url, validated_addresses = _public_https_url(current_url)
        parsed = urllib.parse.urlparse(current_url)
        host = parsed.hostname
        request_target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        connection = http.client.HTTPSConnection(
            host, 443, timeout=timeout, context=ssl.create_default_context(),
        )
        pinned_ip = validated_addresses[0]
        host_header = "[{}]".format(host) if ":" in host else host
        connection._create_connection = lambda address, connect_timeout, source_address: socket.create_connection(
            (pinned_ip, address[1]), connect_timeout, source_address,
        )
        try:
            connection.request("GET", request_target, headers={
                "Host": host_header,
                "User-Agent": "rednote-workbench/0.1.0",
            })
            response = connection.getresponse()
            if 300 <= response.status < 400:
                location = response.getheader("Location")
                if not location or redirects >= 5:
                    raise ToAPIsError("生成结果下载重定向次数过多或缺少地址")
                current_url = urllib.parse.urljoin(current_url, location)
                continue
            if response.status < 200 or response.status >= 300:
                raise ToAPIsError("生成结果下载返回 HTTP {}".format(response.status))
            declared_size = int(response.getheader("Content-Length") or 0)
            if declared_size > max_bytes:
                raise ToAPIsError("生成结果超过文件大小限制")
            chunks = []
            size = 0
            while True:
                chunk = response.read(min(1024 * 1024, max_bytes + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise ToAPIsError("生成结果超过文件大小限制")
                chunks.append(chunk)
            return b"".join(chunks), (response.getheader("Content-Type") or "application/octet-stream").split(";", 1)[0].lower()
        except (http.client.HTTPException, TimeoutError, OSError) as exc:
            raise ToAPIsError("无法下载生成结果：{}".format(exc))
        finally:
            connection.close()
    raise ToAPIsError("生成结果下载重定向次数过多")
