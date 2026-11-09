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
            "region": "com", "image_model": "gpt-image-2", "api_key": "test-only-toapis-key",
        })
        self.assertEqual(status, 200)
        self.assertTrue(settings["configured"])
        self.assertEqual(settings["image_model"], "gpt-image-2")
        self.assertNotIn("api_key", settings)
        status, bootstrap = self.request("/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertEqual(bootstrap["toapis"]["image_model"], "gpt-image-2")
        self.assertNotIn("test-only-toapis-key", json.dumps(bootstrap))
        self.assertNotIn("image_generator", bootstrap)
        status, switched = self.request("/api/toapis/settings", {"region": "cn"})
        self.assertEqual(status, 200)
        self.assertEqual(switched["region"], "cn")
        self.assertFalse(switched["configured"])
        self.assertEqual(switched["image_model"], "")

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

    def test_multiple_generator_profiles_keep_keys_out_of_storage_and_responses(self):
        profiles = [
            {
                "id": "qwen-cheap", "name": "千问低成本", "provider": "qwen",
                "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "protocol": "chat_completions", "model": "qwen-plus",
            },
            {
                "id": "deepseek-backup", "name": "DeepSeek 备用", "provider": "deepseek",
                "base_url": "https://api.deepseek.com",
                "protocol": "chat_completions", "model": "deepseek-chat",
            },
        ]
        status, settings = self.request("/api/generator/settings", {
            "profiles": profiles,
            "auto_fallback": True,
            "api_key_profile_id": "qwen-cheap",
            "api_key": "test-only-profile-key",
        })
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in settings["profiles"]], ["qwen-cheap", "deepseek-backup"])
        self.assertTrue(settings["profiles"][0]["key_in_memory"])
        self.assertFalse(settings["profiles"][1]["key_in_memory"])
        self.assertTrue(settings["auto_fallback"])
        self.assertNotIn("api_key", json.dumps(settings))

        _, bootstrap = self.request("/api/bootstrap")
        saved = Repository(self.data_dir).get_settings()
        self.assertNotIn("test-only-profile-key", json.dumps(bootstrap))
        self.assertNotIn("test-only-profile-key", saved["generator_profiles_json"])

        status, keyed_backup = self.request("/api/generator/settings", {
            "profiles": profiles,
            "auto_fallback": True,
            "api_key_profile_id": "deepseek-backup",
            "api_key": "test-only-backup-key",
        })
        self.assertEqual(status, 200)
        self.assertTrue(keyed_backup["profiles"][0]["key_in_memory"])
        self.assertTrue(keyed_backup["profiles"][1]["key_in_memory"])

        changed_profiles = [dict(profile) for profile in profiles]
        changed_profiles[0]["base_url"] = "https://custom-qwen-relay.example/v1"
        status, changed_endpoint = self.request("/api/generator/settings", {
            "profiles": changed_profiles,
            "auto_fallback": True,
            "api_key_profile_id": "deepseek-backup",
        })
        self.assertEqual(status, 200)
        self.assertFalse(changed_endpoint["profiles"][0]["key_in_memory"])
        self.assertTrue(changed_endpoint["profiles"][1]["key_in_memory"])

        reordered = list(reversed(profiles))
        status, changed = self.request("/api/generator/settings", {
            "profiles": reordered, "auto_fallback": False,
        })
        self.assertEqual(status, 200)
        self.assertEqual(changed["profiles"][0]["id"], "deepseek-backup")
        self.assertFalse(changed["auto_fallback"])

        status, deleted = self.request("/api/generator/settings", {
            "profiles": [], "auto_fallback": True,
        })
        self.assertEqual(status, 200)
        self.assertEqual(deleted["profiles"], [])
        saved = Repository(self.data_dir).get_settings()
        self.assertEqual(saved["generator_profiles_json"], "[]")
        self.assertEqual(saved["generator_model"], "")

        script = (
            "import json, os; from app import _load_generator_profiles; "
            "from workbench.store import Repository; "
            "settings = Repository(os.environ['XHS_WORKBENCH_DATA_DIR']).get_settings(); "
            "print(len(_load_generator_profiles(settings)))"
        )
        env = dict(os.environ)
        env["XHS_WORKBENCH_DATA_DIR"] = str(self.data_dir)
        env["XHS_LLM_MODEL"] = "stale-legacy-model"
        env["XHS_LLM_API_KEY"] = "test-only-legacy-key"
        restarted = subprocess.run(
            [sys.executable, "-c", script], cwd=str(ROOT), env=env,
            text=True, capture_output=True, timeout=10, check=True,
        )
        self.assertEqual(restarted.stdout.strip(), "0")

    def test_legacy_environment_key_requires_exact_model_and_endpoint_match(self):
        profile = {
            "id": "qwen", "name": "通义", "provider": "qwen",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "protocol": "chat_completions", "model": "shared-model-name",
        }
        script = (
            "import json, os; from app import _load_generator_profiles; "
            "profiles = _load_generator_profiles(json.loads(os.environ['TEST_SETTINGS'])); "
            "print(json.dumps([bool(item['api_key']) for item in profiles]))"
        )
        env = dict(os.environ)
        env["XHS_WORKBENCH_DATA_DIR"] = str(self.data_dir)
        env["XHS_LLM_MODEL"] = "shared-model-name"
        env["XHS_LLM_BASE_URL"] = "https://api.openai.com/v1"
        env["XHS_LLM_API_KEY"] = "test-only-env-key"
        env["TEST_SETTINGS"] = json.dumps({"generator_profiles_json": json.dumps([profile])})
        mismatched = subprocess.run(
            [sys.executable, "-c", script], cwd=str(ROOT), env=env,
            text=True, capture_output=True, timeout=10, check=True,
        )
        self.assertEqual(json.loads(mismatched.stdout), [False])

        env["XHS_LLM_BASE_URL"] = profile["base_url"]
        matched = subprocess.run(
            [sys.executable, "-c", script], cwd=str(ROOT), env=env,
            text=True, capture_output=True, timeout=10, check=True,
        )
        self.assertEqual(json.loads(matched.stdout), [True])

    def test_legacy_saved_model_keeps_environment_key_during_migration(self):
        script = (
            "import json, os; from app import _load_generator_profiles; "
            "profiles = _load_generator_profiles(json.loads(os.environ['TEST_SETTINGS'])); "
            "print(json.dumps([(item['model'], item['base_url'], bool(item['api_key'])) for item in profiles]))"
        )
        env = dict(os.environ)
        env["XHS_WORKBENCH_DATA_DIR"] = str(self.data_dir)
        env["XHS_LLM_MODEL"] = ""
        env["XHS_LLM_BASE_URL"] = ""
        env["XHS_LLM_API_KEY"] = "test-only-legacy-key"
        env["TEST_SETTINGS"] = json.dumps({
            "generator_model": "saved-custom-model",
            "generator_base_url": "https://saved-relay.example/v1",
            "generator_protocol": "chat_completions",
            "generator_provider": "custom",
        })
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=str(ROOT), env=env,
            text=True, capture_output=True, timeout=10, check=True,
        )
        self.assertEqual(json.loads(result.stdout), [[
            "saved-custom-model", "https://saved-relay.example/v1", True,
        ]])


if __name__ == "__main__":
    unittest.main()
