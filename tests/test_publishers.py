import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from workbench.publishers import (
    PublisherError,
    XiaohongshuMCPPublisher,
    validate_local_mcp_endpoint,
)


class MCPTestState:
    calls = []
    fail_publish = False
    redirect_target = ""


class RedirectHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.send_response(307)
        self.send_header("Location", MCPTestState.redirect_target)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt, *args):
        pass


class MCPHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        message = json.loads(self.rfile.read(length).decode("utf-8"))
        method = message.get("method")
        result = {}
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "test", "version": "1"},
            }
            body = {"jsonrpc": "2.0", "id": message["id"], "result": result}
        elif method == "notifications/initialized":
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        elif method == "tools/list":
            result = {"tools": [{"name": "check_login_status"}, {"name": "publish_content"}, {"name": "publish_with_video"}, {"name": "get_my_profile"}]}
            body = {"jsonrpc": "2.0", "id": message["id"], "result": result}
        elif method == "tools/call":
            MCPTestState.calls.append(message["params"])
            tool = message["params"]["name"]
            if tool in {"publish_content", "publish_with_video"} and MCPTestState.fail_publish:
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if tool == "check_login_status":
                text = "登录状态：已登录"
            elif tool == "get_my_profile":
                text = json.dumps({
                    "userBasicInfo": {"nickname": "测试账号", "redId": "red-1", "xsecToken": "secret"},
                    "interactions": [
                        {"type": "follows", "count": "12"},
                        {"type": "fans", "count": "34"},
                        {"type": "interaction", "count": "56"},
                    ],
                    "feeds": [{"id": "note-1", "noteCard": {"displayTitle": "测试笔记", "xsecToken": "secret", "interactInfo": {
                        "likedCount": "7", "commentCount": "2", "collectedCount": "3", "sharedCount": "1",
                    }}}],
                    "xsecToken": "secret",
                }, ensure_ascii=False)
            else:
                text = "视频已提交，等待平台审核"
            body = {
                "jsonrpc": "2.0", "id": message["id"],
                "result": {"content": [{"type": "text", "text": text}]},
            }
        else:
            body = {"jsonrpc": "2.0", "id": message.get("id"), "error": {"message": "unknown method"}}
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("MCP-Session-Id", "test-session")
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, fmt, *args):
        pass


class PublisherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        MCPTestState.calls = []
        MCPTestState.fail_publish = False
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), MCPHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.endpoint = "http://127.0.0.1:{}/mcp".format(cls.server.server_port)
        MCPTestState.redirect_target = cls.endpoint
        cls.redirect_server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        cls.redirect_thread = threading.Thread(target=cls.redirect_server.serve_forever, daemon=True)
        cls.redirect_thread.start()
        cls.redirect_endpoint = "http://127.0.0.1:{}/redirect".format(cls.redirect_server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.redirect_server.shutdown()
        cls.redirect_server.server_close()

    def setUp(self):
        MCPTestState.calls = []
        MCPTestState.fail_publish = False

    def test_only_local_mcp_addresses_are_allowed(self):
        self.assertEqual(validate_local_mcp_endpoint("http://localhost:18060/mcp"), "http://localhost:18060/mcp")
        with self.assertRaises(ValueError):
            validate_local_mcp_endpoint("https://example.com/mcp")
        with self.assertRaises(ValueError):
            validate_local_mcp_endpoint("http://example.com/mcp")

    def test_mcp_redirect_is_rejected(self):
        with self.assertRaises(PublisherError):
            XiaohongshuMCPPublisher(self.redirect_endpoint).validate()

    def test_connection_check_finds_expected_tools_and_login(self):
        result = XiaohongshuMCPPublisher(self.endpoint).validate()
        self.assertTrue(result["ok"])
        self.assertIn("已登录", result["message"])
        self.assertEqual(MCPTestState.calls[-1]["name"], "check_login_status")

    def test_account_snapshot_contains_readable_metrics_without_credentials(self):
        snapshot = XiaohongshuMCPPublisher(self.endpoint).read_account_snapshot()
        self.assertEqual(snapshot["login_status"], "logged_in")
        self.assertEqual(snapshot["nickname"], "测试账号")
        self.assertEqual(snapshot["metrics"]["fans"], "34")
        self.assertEqual(snapshot["notes"][0]["likes"], "7")
        encoded = json.dumps(snapshot, ensure_ascii=False)
        self.assertNotIn("xsecToken", encoded)

    def test_video_publish_is_reported_as_submitted_not_verified(self):
        publisher = XiaohongshuMCPPublisher(self.endpoint)
        result = publisher.publish({
            "title": "一条测试视频",
            "body": "测试正文",
            "video_path": "/tmp/owned-video.mp4",
            "tags": ["AI"],
        }, "test-key")
        self.assertEqual(result["status"], "submitted")
        call = MCPTestState.calls[-1]
        self.assertEqual(call["name"], "publish_with_video")
        self.assertEqual(call["arguments"]["video"], "/tmp/owned-video.mp4")
        self.assertNotEqual(result["status"], "verified_visible")

    def test_note_publish_uses_publish_content_with_local_images_and_tags(self):
        publisher = XiaohongshuMCPPublisher(self.endpoint)
        result = publisher.publish({
            "content_type": "note",
            "title": "AI 工具笔记",
            "body": "分享一个具体用法。",
            "image_paths": ["/tmp/cover.png", "/tmp/step-2.png"],
            "tags": ["AI", "效率工具"],
        }, "test-key")
        self.assertEqual(result["status"], "submitted")
        call = MCPTestState.calls[-1]
        self.assertEqual(call["name"], "publish_content")
        self.assertEqual(call["arguments"], {
            "title": "AI 工具笔记",
            "content": "分享一个具体用法。",
            "images": ["/tmp/cover.png", "/tmp/step-2.png"],
            "tags": ["AI", "效率工具"],
        })

    def test_connection_loss_during_publish_is_uncertain(self):
        MCPTestState.fail_publish = True
        publisher = XiaohongshuMCPPublisher(self.endpoint)
        with self.assertRaises(PublisherError) as captured:
            publisher.publish({
                "title": "一条测试视频", "body": "测试正文",
                "video_path": "/tmp/owned-video.mp4", "tags": [],
            }, "test-key")
        self.assertTrue(captured.exception.uncertain)


if __name__ == "__main__":
    unittest.main()
