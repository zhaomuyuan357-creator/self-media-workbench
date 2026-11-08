import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from workbench.generators import GeneratorError, OpenAICompatibleGenerator


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


if __name__ == "__main__":
    unittest.main()
