import json
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from workbench.generators import GeneratorError, OpenAICompatibleGenerator, generate_with_profiles


class GeneratorState:
    calls = []
    responses_calls = []
    redirected_calls = []
    redirect_target = ""


class GeneratorHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        if self.path.endswith("/responses"):
            GeneratorState.responses_calls.append({"headers": dict(self.headers), "body": body})
            content = json.dumps({
                "title": "Responses 标题",
                "body": "Responses 协议正文。",
                "tags": ["AI", "接口"],
            }, ensure_ascii=False)
            response = ({"output": [{"content": [{"type": "output_text", "text": content}]}]}
                        if body.get("model") == "nested-model" else {"output_text": content})
        else:
            GeneratorState.calls.append({"headers": dict(self.headers), "body": body})
            response = {
                "choices": [{"message": {"content": json.dumps({
                    "title": "三个 AI 小技巧",
                    "body": "把一个具体问题拆成步骤，再选择合适工具。",
                    "tags": ["AI", "效率"],
                }, ensure_ascii=False)}}],
            }
        encoded = json.dumps(response, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, fmt, *args):
        pass


class RedirectGeneratorHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        GeneratorState.redirected_calls.append(dict(self.headers))
        self.send_response(307)
        self.send_header("Location", GeneratorState.redirect_target)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt, *args):
        pass


class GeneratorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        GeneratorState.calls = []
        GeneratorState.responses_calls = []
        GeneratorState.redirected_calls = []
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), GeneratorHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.endpoint = "http://127.0.0.1:{}/v1".format(cls.server.server_port)

        cls.redirect_server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectGeneratorHandler)
        cls.redirect_thread = threading.Thread(target=cls.redirect_server.serve_forever, daemon=True)
        cls.redirect_thread.start()
        GeneratorState.redirect_target = cls.endpoint + "/chat/completions"
        cls.redirect_endpoint = "http://127.0.0.1:{}/v1".format(cls.redirect_server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close()
        cls.redirect_server.shutdown(); cls.redirect_server.server_close()

    def test_openai_compatible_response_is_normalized(self):
        draft = OpenAICompatibleGenerator(self.endpoint, "local-model", "test-key").generate(
            {"positioning": "AI 知识"}, "整理会议纪要",
        )
        self.assertEqual(draft["title"], "三个 AI 小技巧")
        self.assertEqual(draft["tags"], ["AI", "效率"])
        self.assertEqual(GeneratorState.calls[-1]["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(GeneratorState.calls[-1]["body"]["model"], "local-model")

    def test_responses_api_request_and_output_text_are_supported(self):
        draft = OpenAICompatibleGenerator(
            self.endpoint, "relay-model", "relay-key", protocol="responses",
        ).generate({"positioning": "AI 知识"}, "整理会议纪要")
        call = GeneratorState.responses_calls[-1]
        self.assertEqual(draft["title"], "Responses 标题")
        self.assertEqual(draft["tags"], ["AI", "接口"])
        self.assertEqual(call["headers"]["Authorization"], "Bearer relay-key")
        self.assertEqual(call["body"]["model"], "relay-model")
        self.assertIn("instructions", call["body"])
        self.assertIn("账号定位", call["body"]["input"])

    def test_responses_nested_output_content_is_supported(self):
        draft = OpenAICompatibleGenerator(
            self.endpoint, "nested-model", "relay-key", protocol="responses",
        ).generate({}, "测试")
        self.assertEqual(draft["body"], "Responses 协议正文。")

    def test_redirect_is_not_followed_with_bearer_token(self):
        GeneratorState.calls = []
        with self.assertRaises(GeneratorError):
            OpenAICompatibleGenerator(self.redirect_endpoint, "local-model", "secret").generate(
                {}, "测试",
            )
        # The redirecting endpoint receives the original request, but the
        # bearer token must never be sent to the target endpoint.
        self.assertEqual(GeneratorState.calls, [])

    def test_router_falls_back_after_explicit_retryable_error(self):
        calls = []

        class FakeGenerator:
            def __init__(self, base_url, model, api_key, protocol="chat_completions"):
                self.model = model

            def generate(self, *args):
                calls.append(self.model)
                if self.model == "low-cost":
                    raise GeneratorError("HTTP 429", retryable=True, http_status=429)
                return {"title": "备用标题", "body": "备用正文", "tags": []}

        profiles = [
            {"id": "cheap", "name": "低成本", "base_url": self.endpoint, "model": "low-cost", "protocol": "chat_completions", "api_key": "cheap-key"},
            {"id": "backup", "name": "备用", "base_url": self.endpoint, "model": "strong-model", "protocol": "chat_completions", "api_key": "backup-key"},
        ]
        with patch("workbench.generators.OpenAICompatibleGenerator", FakeGenerator):
            result = generate_with_profiles(profiles, {}, "测试选题", auto_fallback=True)
        self.assertEqual(calls, ["low-cost", "strong-model"])
        self.assertTrue(result["fallback_used"])
        self.assertEqual(result["profile_name"], "备用")

    def test_only_rate_limit_http_error_is_retryable(self):
        generator = OpenAICompatibleGenerator(self.endpoint, "test-model", "test-key")
        for status, retryable in ((429, True), (502, False), (504, False)):
            with self.subTest(status=status):
                http_error = urllib.error.HTTPError(
                    self.endpoint, status, "test error", {}, None,
                )
                with patch("workbench.generators.urllib.request.build_opener") as build_opener:
                    build_opener.return_value.open.side_effect = http_error
                    with self.assertRaises(GeneratorError) as raised:
                        generator.generate({}, "测试")
                self.assertEqual(raised.exception.retryable, retryable)

    def test_router_does_not_retry_uncertain_or_nonretryable_errors(self):
        calls = []

        class FakeGenerator:
            def __init__(self, base_url, model, api_key, protocol="chat_completions"):
                self.model = model

            def generate(self, *args):
                calls.append(self.model)
                raise GeneratorError("连接中断，状态不明", retryable=False)

        profiles = [
            {"id": "primary", "name": "首选", "base_url": self.endpoint, "model": "primary-model", "protocol": "chat_completions", "api_key": "primary-key"},
            {"id": "backup", "name": "备用", "base_url": self.endpoint, "model": "backup-model", "protocol": "chat_completions", "api_key": "backup-key"},
        ]
        with patch("workbench.generators.OpenAICompatibleGenerator", FakeGenerator):
            with self.assertRaisesRegex(GeneratorError, "连接中断"):
                generate_with_profiles(profiles, {}, "测试选题", auto_fallback=True)
        self.assertEqual(calls, ["primary-model"])


if __name__ == "__main__":
    unittest.main()
