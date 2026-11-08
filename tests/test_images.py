import base64
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from workbench.images import ImageGeneratorError, OpenAICompatibleImageGenerator


PNG_FIXTURE = b"\x89PNG\r\n\x1a\nfixture"


class ImageState:
    response_format = "valid"
    calls = []
    redirect_target = ""


class ImageHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        ImageState.calls.append(json.loads(self.rfile.read(length).decode("utf-8")))
        if ImageState.response_format == "redirect":
            self.send_response(307)
            self.send_header("Location", ImageState.redirect_target)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        encoded = "%%%bad-base64%%%" if ImageState.response_format == "bad-base64" else base64.b64encode(PNG_FIXTURE).decode("ascii")
        payload = json.dumps({"data": [{"b64_json": encoded, "mime_type": "image/png"}]}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        pass


class ImageRedirectTarget(BaseHTTPRequestHandler):
    calls = []

    def do_POST(self):
        self.calls.append(True)
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, fmt, *args):
        pass


class ImageGeneratorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ImageState.calls = []
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), ImageHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.endpoint = "http://127.0.0.1:{}/v1".format(cls.server.server_port)
        cls.target = ThreadingHTTPServer(("127.0.0.1", 0), ImageRedirectTarget)
        cls.target_thread = threading.Thread(target=cls.target.serve_forever, daemon=True)
        cls.target_thread.start()
        ImageState.redirect_target = "http://127.0.0.1:{}/images/generations".format(cls.target.server_port)
        cls.redirect_endpoint = "http://127.0.0.1:{}/v1".format(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close()
        cls.target.shutdown(); cls.target.server_close()

    def setUp(self):
        ImageState.response_format = "valid"
        ImageState.calls = []
        ImageRedirectTarget.calls = []

    def test_base64_image_response_is_decoded(self):
        image, mime = OpenAICompatibleImageGenerator(self.endpoint, "image-model", "test-key").generate(
            "simple cover", "1024x1536",
        )
        self.assertEqual(image, PNG_FIXTURE)
        self.assertEqual(mime, "image/png")
        self.assertEqual(ImageState.calls[-1]["response_format"], "b64_json")
        self.assertEqual(ImageState.calls[-1]["size"], "1024x1536")

    def test_invalid_base64_is_rejected(self):
        ImageState.response_format = "bad-base64"
        with self.assertRaisesRegex(ImageGeneratorError, "Base64"):
            OpenAICompatibleImageGenerator(self.endpoint, "image-model", "test-key").generate("cover")

    def test_redirect_is_not_followed(self):
        ImageState.response_format = "redirect"
        with self.assertRaises(ImageGeneratorError):
            OpenAICompatibleImageGenerator(self.redirect_endpoint, "image-model", "secret").generate("cover")
        self.assertEqual(ImageRedirectTarget.calls, [])


if __name__ == "__main__":
    unittest.main()
