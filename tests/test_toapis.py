import json
import unittest
from unittest.mock import patch
import socket

from workbench.toapis import (
    ToAPIsMCPClient, ToAPIsError, _parse_response, _public_https_url,
    documented_parameter_names, generation_fields, model_count, tool_text,
    validate_params_against_docs,
)


class FakeResponse:
    def __init__(self, payload, status=200, headers=None):
        self.payload = json.dumps(payload).encode("utf-8") if payload is not None else b""
        self.status = status
        self.headers = {"Content-Type": "application/json", **(headers or {})}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
        if size < 0:
            value, self.payload = self.payload, b""
            return value
        value, self.payload = self.payload[:size], self.payload[size:]
        return value


class FakeMCPTransport:
    def __init__(self):
        self.calls = []

    def open(self, request, timeout=None):
        if request.get_method() == "DELETE":
            self.calls.append({"request": {"method": "DELETE"}, "headers": dict(request.header_items())})
            return FakeResponse(None, status=200)
        body = json.loads(request.data.decode("utf-8"))
        self.calls.append({"request": body, "headers": dict(request.header_items())})
        method = body["method"]
        if method == "notifications/initialized":
            return FakeResponse(None, status=202, headers={"Content-Length": "0"})
        if method == "initialize":
            result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}, "serverInfo": {"name": "mock", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": [
                {"name": "list_models", "inputSchema": {
                    "type": "object", "properties": {"type": {"type": "string", "enum": ["image", "video"]}},
                    "required": ["type"],
                }},
                {"name": "get_balance", "inputSchema": {"type": "object", "properties": {}}},
                {"name": "get_model_docs", "inputSchema": {"type": "object", "properties": {"model": {"type": "string"}}}},
                {"name": "generate_image", "inputSchema": {"type": "object", "properties": {"model": {}, "params": {"type": "object"}, "client_request_id": {}}}},
                {"name": "get_generation", "inputSchema": {"type": "object", "properties": {"client_request_id": {}}}},
            ]}
        elif method == "tools/call":
            name = body["params"]["name"]
            if name == "list_models":
                payload = {"models": [{"id": "gpt-image-2", "type": body["params"]["arguments"]["type"]}]}
            else:
                results = {
                    "get_model_docs": "| Parameter | Required | Type |\n|---|---|---|\n| prompt | yes | string |\n| size | no | string |",
                    "generate_image": {"state": "running", "task_id": "task-123", "poll_after_seconds": 3},
                    "get_generation": {"state": "completed", "task_id": "task-123", "result": {"image_url": "https://files.toapis.example/result.png"}},
                    "get_balance": {"credits": 100},
                }
                payload = results[name]
            result = {"content": [{"type": "text", "text": json.dumps(payload) if not isinstance(payload, str) else payload}]}
        else:
            raise AssertionError("unexpected method " + method)
        return FakeResponse(
            {"jsonrpc": "2.0", "id": body.get("id"), "result": result},
            headers={"Mcp-Session-Id": "mock-session"},
        )


class ToAPIsTests(unittest.TestCase):
    def test_mcp_tools_use_bearer_auth_locale_and_parse_tool_text(self):
        transport = FakeMCPTransport()
        with patch("workbench.toapis.urllib.request.build_opener", return_value=transport):
            client = ToAPIsMCPClient("https://api.toapis.com/mcp", "test-only-key")
            models = client.list_models("image")
            video_models = client.list_models("video")
            balance = client.get_balance()
            docs = client.get_model_docs("gpt-image-2")
            generation = client.generate("image", "gpt-image-2", {"prompt": "A clean cover"}, "toapis-test-id")
            checked = client.get_generation("toapis-test-id")
            client.close()
        self.assertEqual(models["models"][0]["id"], "gpt-image-2")
        self.assertEqual(video_models["models"][0]["type"], "video")
        self.assertEqual(client.list_models_type_options(), ["image", "video"])
        self.assertEqual(balance["credits"], 100)
        self.assertIn("prompt", docs["text"])
        self.assertEqual(generation["task_id"], "task-123")
        self.assertEqual(checked["state"], "completed")
        call = next(item for item in transport.calls if item["request"]["method"] == "tools/call")
        self.assertEqual(call["headers"]["Authorization"], "Bearer test-only-key")
        self.assertEqual(call["headers"]["X-locale"], "zh")
        generation_args = [item["request"]["params"]["arguments"] for item in transport.calls
                           if item["request"].get("params", {}).get("name") == "generate_image"]
        self.assertEqual(generation_args[0]["client_request_id"], "toapis-test-id")
        model_args = [item["request"]["params"]["arguments"] for item in transport.calls
                      if item["request"].get("params", {}).get("name") == "list_models"]
        self.assertEqual(model_args, [{"type": "image"}, {"type": "video"}])
        self.assertEqual(transport.calls[-1]["request"]["method"], "DELETE")

    def test_mcp_sse_response_and_documented_parameter_allowlist(self):
        result = _parse_response(
            b"event: message\ndata: {\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"ok\":true}}\n\n",
            "text/event-stream",
        )
        self.assertTrue(result["result"]["ok"])
        docs = "| Parameter | Required |\n|---|---|\n| prompt | yes |\n| duration | no |"
        self.assertEqual(documented_parameter_names(docs), {"prompt", "duration"})
        structured_docs = {"model": "example", "markdown": docs}
        self.assertEqual(tool_text(structured_docs), docs)
        self.assertEqual(documented_parameter_names(tool_text(structured_docs)), {"prompt", "duration"})
        self.assertEqual(validate_params_against_docs({"prompt": "portrait"}, docs)["prompt"], "portrait")
        with self.assertRaisesRegex(ValueError, "没有找到"):
            validate_params_against_docs({"unexpected": True}, docs)
        self.assertEqual(model_count({"models": []}), 0)
        self.assertEqual(model_count({"models": [{"id": "one"}]}), 1)

    def test_generation_states_and_private_result_hosts_are_checked(self):
        self.assertEqual(generation_fields({"status": "succeeded", "output": [{"url": "https://cdn.example/image.png"}]})["state"], "completed")
        self.assertEqual(generation_fields({"status": "pending", "task_id": "t1"})["state"], "running")
        with self.assertRaises(ToAPIsError):
            _public_https_url("http://127.0.0.1/secret")
        with patch("workbench.toapis.socket.getaddrinfo", return_value=[(0, 0, 0, "", ("10.0.0.2", 443))]):
            with self.assertRaisesRegex(ToAPIsError, "非公网"):
                _public_https_url("https://media.example/result.mp4")
        with self.assertRaisesRegex(ToAPIsError, "标准端口"):
            _public_https_url("https://media.example:9443/result.mp4")

    def test_media_download_connects_to_the_validated_public_ip(self):
        class FakeDownloadResponse:
            status = 200

            def __init__(self):
                self.payload = b"media"

            def getheader(self, name):
                return {"Content-Length": "5", "Content-Type": "image/png"}.get(name)

            def read(self, size=-1):
                payload, self.payload = self.payload[:size], self.payload[size:]
                return payload

        class FakeConnection:
            def __init__(self, host, port, timeout, context):
                self.host, self.port = host, port
                self.response = FakeDownloadResponse()
                self.pinned_address = None

            def request(self, method, target, headers):
                self.method, self.target, self.headers = method, target, headers
                self.pinned_address = self._create_connection((self.host, self.port), 5, None)

            def getresponse(self):
                return self.response

            def close(self):
                pass

        connection = FakeConnection("media.example", 443, 180, None)
        with patch("workbench.toapis.socket.getaddrinfo", return_value=[(0, 0, 0, "", ("93.184.216.34", 443))]), \
             patch("workbench.toapis.socket.create_connection", return_value="pinned-socket") as connect, \
             patch("workbench.toapis.http.client.HTTPSConnection", return_value=connection):
            from workbench.toapis import download_result
            body, mime_type = download_result("https://media.example/path?token=1", 1024)
        self.assertEqual(body, b"media")
        self.assertEqual(mime_type, "image/png")
        self.assertEqual(connection.pinned_address, "pinned-socket")
        self.assertEqual(connect.call_args.args[0], ("93.184.216.34", 443))
        self.assertEqual(connection.target, "/path?token=1")


if __name__ == "__main__":
    unittest.main()
