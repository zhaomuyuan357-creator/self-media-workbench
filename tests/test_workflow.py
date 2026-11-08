import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from workbench.store import Repository


ROOT = Path(__file__).resolve().parents[1]


class WorkflowHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="xhs-workbench-test-")
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        cls.port = sock.getsockname()[1]
        sock.close()
        env = dict(os.environ)
        env.pop("TOAPIS_API_KEY", None)
        cls.data_dir = Path(cls.temp.name) / "data"
        env["XHS_WORKBENCH_DATA_DIR"] = str(cls.data_dir)
        env["XHS_WORKBENCH_PORT"] = str(cls.port)
        cls.proc = subprocess.Popen(
            [sys.executable, "app.py"], cwd=str(ROOT), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        cls.base = "http://127.0.0.1:{}".format(cls.port)
        deadline = time.time() + 8
        while time.time() < deadline:
            if cls.proc.poll() is not None:
                raise RuntimeError("MVP HTTP server exited before starting")
            try:
                cls.request("/api/bootstrap")
                break
            except Exception:
                time.sleep(.1)
        else:
            raise RuntimeError("MVP HTTP server did not start")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=4)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
        cls.temp.cleanup()

    @classmethod
    def request(cls, path, value=None, data=None, content_type=None):
        headers = {}
        if value is not None:
            data = json.dumps(value).encode("utf-8")
            content_type = "application/json"
        if content_type:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(cls.base + path, data=data, headers=headers, method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                body = response.read()
                return response.status, json.loads(body.decode("utf-8")) if body else {}
        except urllib.error.HTTPError as exc:
            body = exc.read()
            return exc.code, json.loads(body.decode("utf-8")) if body else {}

    def test_upload_profile_simulated_publish_and_duplicate_guard(self):
        self.request("/api/toapis/settings", {"region": "com", "clear_api_key": True})
        status, bootstrap = self.request("/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertEqual(bootstrap["settings"]["publisher_mode"], "mock")
        self.assertFalse(bootstrap["toapis"]["configured"])
        self.assertNotIn("api_key", bootstrap["toapis"])
        status, connection_error = self.request("/api/toapis/models")
        self.assertEqual(status, 400)
        self.assertIn("API Key", connection_error["error"])
        old_version = bootstrap["account"]["current_profile_version"]
        profile_payload = {
            "display_name": "AI 知识分享",
            "profile": {
                "content_lane": "AI 知识分享", "positioning": "用例测试定位",
                "audience": "测试用户", "tone": "具体直白", "structure": "问题-方法-限制",
                "boundaries": "不编造", "fact_rules": "保留来源",
                "preferred_topics": ["AI", "效率工具"],
            },
        }
        status, account = self.request("/api/account", profile_payload)
        self.assertEqual(status, 200)
        self.assertGreater(account["current_profile_version"], old_version)

        boundary = "----workbench-test-boundary"
        body = (
            "--{}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"demo.mp4\"\r\n"
            "Content-Type: video/mp4\r\n\r\n".format(boundary).encode("ascii")
            + b"not-a-real-video-but-valid-test-payload"
            + "\r\n--{}--\r\n".format(boundary).encode("ascii")
        )
        status, asset = self.request(
            "/api/assets", data=body,
            content_type="multipart/form-data; boundary={}".format(boundary),
        )
        self.assertEqual(status, 201)
        self.assertEqual(asset["original_name"], "demo.mp4")
        self.assertIn("sha256", asset)

        image_repo = Repository(self.data_dir)
        image = image_repo.save_generated_image(b"\x89PNG\r\n\x1a\nworkflow-image", "image/png")
        image_2 = image_repo.save_generated_image(b"\x89PNG\r\n\x1a\nworkflow-image-2", "image/png")
        payload = {
            "confirmed": True, "content_type": "note", "asset_ids": [image["id"], image_2["id"]],
            "title": "测试内容", "body": "这是一段仅在本机模拟的内容。", "tags": ["AI"],
        }
        status, job = self.request("/api/publish", payload)
        self.assertEqual(status, 202)
        deadline = time.time() + 5
        while time.time() < deadline:
            _, job = self.request("/api/jobs/" + job["id"])
            if job["status"] != "submitting":
                break
            time.sleep(.05)
        self.assertEqual(job["status"], "simulated")
        self.assertIn("未上传", job["attempt"]["result"]["message"])
        self.assertEqual(job["persona_version"], account["current_profile_version"])
        self.assertEqual(job["content_type"], "note")
        self.assertEqual(job["asset_ids"], [image["id"], image_2["id"]])

        # Keep the pre-existing video path working for old content records.
        video_status, video_job = self.request("/api/publish", {
            "confirmed": True, "content_type": "video", "asset_id": asset["id"],
            "title": "兼容视频", "body": "旧视频流程兼容测试。", "tags": [],
        })
        self.assertEqual(video_status, 202)
        deadline = time.time() + 5
        while time.time() < deadline:
            _, video_job = self.request("/api/jobs/" + video_job["id"])
            if video_job["status"] != "submitting":
                break
            time.sleep(.05)
        self.assertEqual(video_job["status"], "simulated")
        self.assertEqual(video_job["content_type"], "video")

        status, _ = self.request("/api/jobs/{}/verification".format(job["id"]), {"outcome": "visible"})
        self.assertEqual(status, 400)

    def test_publish_requires_explicit_confirmation(self):
        status, result = self.request("/api/publish", {
            "confirmed": False,
            "asset_id": "not-used",
            "title": "另一个测试标题", "body": "测试正文", "tags": [],
        })
        self.assertEqual(status, 400)
        self.assertIn("明确确认", result["error"])

    def test_toapis_settings_keep_key_only_in_process_memory(self):
        status, settings = self.request("/api/toapis/settings", {
            "region": "com", "api_key": "test-only-toapis-key",
        })
        self.assertEqual(status, 200)
        self.assertTrue(settings["configured"])
        self.assertNotIn("api_key", settings)
        status, bootstrap = self.request("/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertNotIn("test-only-toapis-key", json.dumps(bootstrap))

    def test_toapis_bootstrap_does_not_expose_local_asset_paths(self):
        repo = Repository(self.data_dir)
        asset = repo.save_generated_image(b"\x89PNG\r\n\x1a\nfixture", "image/png")
        repo.save_toapis_generation(
            "toapis-path-check-123", "image", "image-model", "completed", asset_id=asset["id"],
        )
        status, bootstrap = self.request("/api/bootstrap")
        self.assertEqual(status, 200)
        generation = next(item for item in bootstrap["toapis_generations"]
                          if item["client_request_id"] == "toapis-path-check-123")
        self.assertNotIn("path", generation["asset"])
        self.assertNotIn("extension", generation["asset"])

    def test_generator_relay_settings_support_protocol_and_clear_old_key_on_switch(self):
        status, configured = self.request("/api/generator/settings", {
            "provider": "aixhan", "base_url": "https://api.aixhan.com/v1",
            "protocol": "responses", "model": "relay-model", "api_key": "test-only-key",
        })
        self.assertEqual(status, 200)
        self.assertEqual(configured["provider"], "aixhan")
        self.assertEqual(configured["protocol"], "responses")
        self.assertTrue(configured["key_in_memory"])
        self.assertNotIn("api_key", configured)

        status, switched = self.request("/api/generator/settings", {
            "provider": "custom", "base_url": "https://relay.example/v1",
            "protocol": "chat_completions", "model": "another-model",
        })
        self.assertEqual(status, 200)
        self.assertFalse(switched["key_in_memory"])
        self.assertEqual(switched["provider"], "custom")
        self.assertEqual(switched["protocol"], "chat_completions")


if __name__ == "__main__":
    unittest.main()
