import http.client
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone


class PublisherError(Exception):
    def __init__(self, message, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_local_mcp_endpoint(endpoint):
    parsed = urllib.parse.urlparse((endpoint or "").strip())
    if parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("MCP 地址必须是本机 HTTP 地址，例如 http://127.0.0.1:18060/mcp")
    hostname = parsed.hostname.lower()
    if hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("为保护账号会话，MCP 连接只允许本机地址")
    if parsed.port not in (None, 1, 80, 443, 18060, 8765, 9222):
        # Loopback is still required; this is a guard against accidental exposure,
        # not an assumption about the MCP service's port.
        pass
    return endpoint.strip()


class MCPClient:
    def __init__(self, endpoint, timeout=30):
        self.endpoint = validate_local_mcp_endpoint(endpoint)
        self.timeout = timeout
        self.session_id = None
        self.protocol_version = "2024-11-05"
        self._next_id = 0
        self._initialized = False

    def _request(self, message, initialize=False, timeout=None):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["MCP-Session-Id"] = self.session_id
            headers["MCP-Protocol-Version"] = self.protocol_version
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(message).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            opener = urllib.request.build_opener(_NoRedirectHandler)
            response = opener.open(request, timeout=timeout or self.timeout)
        except (urllib.error.URLError, socket.timeout, TimeoutError, http.client.HTTPException) as exc:
            raise PublisherError("无法连接本机小红书 MCP 服务：{}".format(exc), uncertain=False)
        with response:
            session_id = response.headers.get("MCP-Session-Id")
            if session_id:
                self.session_id = session_id
            protocol = response.headers.get("MCP-Protocol-Version")
            if protocol:
                self.protocol_version = protocol
            body = response.read()
            content_type = response.headers.get("Content-Type", "")
        if not body:
            return None
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            parsed = None
            for line in body.decode("utf-8", "replace").splitlines():
                if line.startswith("data:"):
                    try:
                        parsed = json.loads(line[5:].strip())
                    except ValueError:
                        continue
            if parsed is None:
                raise PublisherError("MCP 服务返回了无法识别的响应", uncertain=not initialize)
        if isinstance(parsed, list):
            parsed = parsed[-1] if parsed else None
        return parsed

    def initialize(self):
        if self._initialized:
            return
        self._next_id += 1
        response = self._request({
            "jsonrpc": "2.0", "id": self._next_id, "method": "initialize",
            "params": {
                "protocolVersion": self.protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "xhs-content-workbench", "version": "0.1.0"},
            },
        }, initialize=True)
        if not isinstance(response, dict) or response.get("error"):
            raise PublisherError("MCP 初始化失败，请检查服务版本和地址", uncertain=False)
        result = response.get("result") or {}
        self.protocol_version = result.get("protocolVersion", self.protocol_version)
        self._request({"jsonrpc": "2.0", "method": "notifications/initialized"}, initialize=True)
        self._initialized = True

    def list_tools(self):
        self.initialize()
        self._next_id += 1
        response = self._request({
            "jsonrpc": "2.0", "id": self._next_id, "method": "tools/list", "params": {},
        })
        if not isinstance(response, dict) or response.get("error"):
            raise PublisherError("MCP 工具清单读取失败", uncertain=False)
        return (response.get("result") or {}).get("tools", [])

    def call_tool(self, name, arguments=None, call_timeout=None):
        self.initialize()
        self._next_id += 1
        try:
            response = self._request({
                "jsonrpc": "2.0", "id": self._next_id, "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            }, timeout=call_timeout)
        except PublisherError as exc:
            exc.uncertain = True
            raise
        if not isinstance(response, dict):
            raise PublisherError("MCP 发布结果为空，请人工核验小红书", uncertain=True)
        if response.get("error"):
            raise PublisherError("MCP 工具调用结果不明确，请人工核验：{}".format(
                response["error"].get("message", "工具调用失败")
            ), uncertain=True)
        return response.get("result") or {}


class MockPublisher:
    publisher_id = "mock"

    def validate(self, request):
        return {"ok": True, "message": "模拟连接器已就绪；不会向小红书上传内容。"}

    def publish(self, request, idempotency_key):
        return {
            "status": "simulated",
            "result": {
                "message": "模拟提交完成（仅本地演示，未上传到小红书）",
                "idempotency_key": idempotency_key,
            },
        }


class XiaohongshuMCPPublisher:
    publisher_id = "xiaohongshu-mcp"

    def __init__(self, endpoint):
        self.endpoint = validate_local_mcp_endpoint(endpoint)

    @staticmethod
    def _text(result):
        chunks = []
        for item in result.get("content", []) if isinstance(result, dict) else []:
            if isinstance(item, dict) and item.get("type") == "text":
                chunks.append(item.get("text", ""))
        return "\n".join(x for x in chunks if x).strip()

    def validate(self, request=None):
        client = MCPClient(self.endpoint)
        try:
            tools = client.list_tools()
            available = {tool.get("name") for tool in tools}
            content_type = (request or {}).get("content_type", "note")
            expected_tool = "publish_content" if content_type == "note" else "publish_with_video"
            if expected_tool not in available or "check_login_status" not in available:
                return {"ok": False, "message": "MCP 已连接，但未找到图文发布或登录检查工具。"}
            result = client.call_tool("check_login_status", {})
        except PublisherError as exc:
            raise PublisherError(str(exc), uncertain=False)
        if result.get("isError"):
            return {"ok": False, "message": self._text(result) or "登录状态检查失败"}
        message = self._text(result) or "已收到登录状态响应。请在发布前核对会话账号。"
        if any(token in message.lower() for token in ("未登录", "请先登录", "登录失败", "not logged in")):
            return {"ok": False, "message": message}
        return {"ok": True, "message": message}

    @staticmethod
    def _tool_text(result):
        return XiaohongshuMCPPublisher._text(result)

    @staticmethod
    def _tool_json(result, tool_name):
        if not isinstance(result, dict):
            raise PublisherError("MCP {} 返回了无法识别的数据".format(tool_name), uncertain=False)
        if result.get("isError"):
            raise PublisherError("MCP {} 读取失败：{}".format(
                tool_name, XiaohongshuMCPPublisher._tool_text(result) or "服务返回错误"
            ), uncertain=False)
        text = XiaohongshuMCPPublisher._tool_text(result)
        try:
            return json.loads(text)
        except (TypeError, ValueError):
            raise PublisherError("MCP {} 返回了无法识别的数据".format(tool_name), uncertain=False)

    def read_account_snapshot(self):
        """Read current login and own-profile metrics without storing auth tokens."""
        client = MCPClient(self.endpoint, timeout=45)
        login_result = client.call_tool("check_login_status", {}, call_timeout=45)
        login_message = self._tool_text(login_result)
        if login_result.get("isError"):
            snapshot = {"login_status": "error", "message": login_message or "登录状态检查失败"}
        elif any(token in login_message.lower() for token in ("未登录", "请先登录", "登录失败", "not logged in")):
            snapshot = {"login_status": "logged_out", "message": login_message or "当前未登录"}
        else:
            profile_result = client.call_tool("get_my_profile", {"tab": "note"}, call_timeout=90)
            profile = self._tool_json(profile_result, "get_my_profile")
            basic = profile.get("userBasicInfo") or {}
            interactions = profile.get("interactions") or []
            if not isinstance(interactions, list):
                interactions = []
            metrics = {}
            for item in interactions:
                if not isinstance(item, dict):
                    continue
                key = item.get("type") or item.get("name")
                if key in {"follows", "关注"}:
                    metrics["follows"] = str(item.get("count", "—"))
                elif key in {"fans", "粉丝"}:
                    metrics["fans"] = str(item.get("count", "—"))
                elif key in {"interaction", "获赞与收藏"}:
                    metrics["likes_and_saves"] = str(item.get("count", "—"))
            feeds = profile.get("feeds") or []
            if not isinstance(feeds, list):
                feeds = []
            notes = []
            for feed in feeds:
                if not isinstance(feed, dict):
                    continue
                card = feed.get("noteCard") or {}
                interactions = card.get("interactInfo") or {}
                notes.append({
                    "id": str(feed.get("id", "")),
                    "title": str(card.get("displayTitle") or "未命名笔记")[:200],
                    "likes": str(interactions.get("likedCount", "—")),
                    "comments": str(interactions.get("commentCount", "—")),
                    "saves": str(interactions.get("collectedCount", "—")),
                    "shares": str(interactions.get("sharedCount", "—")),
                })
            metrics["loaded_notes"] = len(notes)
            snapshot = {
                "login_status": "logged_in",
                "message": login_message or "登录有效",
                "nickname": str(basic.get("nickname", ""))[:100],
                "red_id": str(basic.get("redId", ""))[:100],
                "description": str(basic.get("desc", ""))[:500],
                "metrics": metrics,
                "notes": notes[:50],
                "metrics_limit": "主页当前加载的笔记；MCP 暂未提供播放量",
            }
        snapshot["source"] = self.publisher_id
        snapshot["observed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return snapshot

    def search_notes(self, keyword):
        keyword = str(keyword or "").strip()[:100]
        if not keyword:
            raise ValueError("请填写要研究的关键词")
        client = MCPClient(self.endpoint, timeout=60)
        result = client.call_tool("search_feeds", {"keyword": keyword}, call_timeout=60)
        data = self._tool_json(result, "search_feeds")
        if isinstance(data, dict):
            feeds = data.get("feeds", [])
        elif isinstance(data, list):
            feeds = data
        else:
            feeds = []
        if not isinstance(feeds, list):
            feeds = []
        notes = []
        for feed in feeds[:20]:
            if not isinstance(feed, dict):
                continue
            card = feed.get("noteCard") or {}
            interactions = card.get("interactInfo") or {}
            note_id = str(feed.get("id", ""))
            notes.append({
                "source": "小红书站内搜索",
                "title": str(card.get("displayTitle") or "未命名笔记")[:200],
                "url": "https://www.xiaohongshu.com/explore/" + note_id if note_id else "",
                "likes": str(interactions.get("likedCount", "—")),
                "comments": str(interactions.get("commentCount", "—")),
            })
        return notes

    def publish(self, request, idempotency_key):
        client = MCPClient(self.endpoint, timeout=60)
        if request.get("content_type") == "note":
            arguments = {
                "title": request["title"],
                "content": request["body"],
                "images": request.get("image_paths", []),
                "tags": request["tags"],
            }
            if not arguments["images"]:
                raise PublisherError("图文笔记至少需要一张图片", uncertain=False)
            result = client.call_tool("publish_content", arguments, call_timeout=900)
            message = self._text(result)
            if result.get("isError"):
                raise PublisherError(
                    "MCP 返回了图文发布错误，请检查小红书状态后再处理：{}".format(message or "无详细信息"),
                    uncertain=True,
                )
            if message:
                return {"status": "submitted", "result": {"message": message}}
            return {
                "status": "unknown_manual_check",
                "result": {"message": "MCP 未返回可识别的提交说明。请先检查小红书，避免重复发布。"},
            }
        arguments = {
            "title": request["title"],
            "content": request["body"],
            "video": request["video_path"],
            "tags": request["tags"],
        }
        result = client.call_tool("publish_with_video", arguments, call_timeout=900)
        message = self._text(result)
        if result.get("isError"):
            raise PublisherError(
                "MCP 返回了发布错误，请检查小红书状态后再处理：{}".format(message or "无详细信息"),
                uncertain=True,
            )
        # An MCP/browser acknowledgement is only a submission signal. It does not
        # prove moderation approval or public visibility.
        if message:
            return {"status": "submitted", "result": {"message": message}}
        return {
            "status": "unknown_manual_check",
            "result": {"message": "MCP 未返回可识别的提交说明。请先检查小红书，避免重复发布。"},
        }


def publisher_for(mode, endpoint):
    if mode == "mock":
        return MockPublisher()
    if mode == "xiaohongshu-mcp":
        return XiaohongshuMCPPublisher(endpoint)
    raise ValueError("不支持的发布模式")
