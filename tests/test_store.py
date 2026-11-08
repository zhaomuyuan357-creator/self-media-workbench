import tempfile
import unittest
from pathlib import Path

from workbench.store import DuplicatePublishError, Repository


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xhs-store-test-")
        self.repo = Repository(self.temp.name)
        self.asset = self.repo.save_asset("owned.mp4", _BytesReader(b"test-video"), "video/mp4")
        self.image = self.repo.save_generated_image(b"\x89PNG\r\n\x1a\nfixture", "image/png")

    def configure_account(self):
        return self.repo.save_account("测试账号", {
            "content_lane": "AI 知识",
            "positioning": "用具体案例分享 AI 方法",
            "audience": "想提升效率的职场人",
            "tone": "清楚、具体、克制",
        })

    def test_unconfigured_account_cannot_publish(self):
        with self.assertRaisesRegex(ValueError, "完成并保存账号定位"):
            self.repo.create_publish_job(
                self.asset["id"], "测试标题", "测试正文", ["AI"], "mock",
            )

    def tearDown(self):
        self.temp.cleanup()

    def test_uncertain_real_publish_blocks_duplicate_but_simulation_is_separate(self):
        account = self.configure_account()
        job, _ = self.repo.create_publish_job(
            self.asset["id"], "测试标题", "测试正文", ["AI"], "xiaohongshu-mcp",
        )
        self.repo.update_publish_result(job["id"], "unknown_manual_check", {"message": "timeout"})
        with self.assertRaises(DuplicatePublishError) as captured:
            self.repo.create_publish_job(
                self.asset["id"], "测试标题", "测试正文", ["AI"], "xiaohongshu-mcp",
            )
        self.assertEqual(captured.exception.job_id, job["id"])

        simulated, _ = self.repo.create_publish_job(
            self.asset["id"], "测试标题", "测试正文", ["AI"], "mock",
        )
        self.repo.update_publish_result(simulated["id"], "simulated", {"message": "no upload"})
        self.assertEqual(self.repo.get_job(simulated["id"])["status"], "simulated")

    def test_note_job_stores_multiple_images_and_legacy_video_still_works(self):
        account = self.configure_account()
        second_image = self.repo.save_generated_image(b"\x89PNG\r\n\x1a\nfixture-2", "image/png")
        note, _ = self.repo.create_publish_job(
            self.image["id"], "图文测试", "笔记正文", ["AI"], "mock",
            asset_ids=[self.image["id"], second_image["id"]], content_type="note",
        )
        self.assertEqual(note["content_type"], "note")
        self.assertEqual(note["asset_ids"], [self.image["id"], second_image["id"]])
        self.assertEqual(note["persona_version"], account["current_profile_version"])

        video, _ = self.repo.create_publish_job(
            self.asset["id"], "旧视频任务", "兼容正文", [], "mock",
            content_type="video",
        )
        self.assertEqual(video["content_type"], "video")
        self.assertEqual(video["asset_ids"], [self.asset["id"]])
        with self.repo._connect() as db:
            package_id = db.execute(
                "SELECT content_package_id FROM publish_jobs WHERE id=?", (video["id"],)
            ).fetchone()["content_package_id"]
            db.execute("UPDATE content_packages SET asset_ids_json='[]' WHERE id=?", (package_id,))
        legacy_read = self.repo.get_job(video["id"])
        self.assertEqual(legacy_read["asset_ids"], [self.asset["id"]])

    def test_generated_image_mime_must_match_image_signature(self):
        with self.assertRaisesRegex(ValueError, "格式与生图接口声明不符"):
            self.repo.save_generated_image(b"not-an-image", "image/png")

    def test_generated_video_is_validated_and_saved_as_video_asset(self):
        video = self.repo.save_generated_video(b"....ftypisom....video", "video/mp4")
        self.assertEqual(video["media_type"], "video")
        self.assertEqual(video["mime_type"], "video/mp4")
        self.assertTrue(video["path"].endswith(".mp4"))
        with self.assertRaisesRegex(ValueError, "格式不匹配"):
            self.repo.save_generated_video(b"not-an-mp4", "video/mp4")

    def test_toapis_generation_status_does_not_store_remote_result_urls(self):
        saved = self.repo.save_toapis_generation(
            "toapis-test-id", "image", "image-model", "running", "task-1", 4,
        )
        self.assertEqual(saved["state"], "running")
        self.assertNotIn("api_key", saved)
        self.assertEqual(self.repo.list_toapis_generations()[0]["client_request_id"], "toapis-test-id")

    def test_toapis_generation_creation_is_idempotent(self):
        created, first = self.repo.create_toapis_generation(
            "toapis-idempotency-123", "image", "image-model",
        )
        duplicate, existing = self.repo.create_toapis_generation(
            "toapis-idempotency-123", "video", "different-model",
        )
        self.assertTrue(created)
        self.assertFalse(duplicate)
        self.assertEqual(existing["media_type"], first["media_type"])
        self.assertEqual(existing["model"], first["model"])

    def test_monitor_snapshot_does_not_store_auth_fields(self):
        self.configure_account()
        snapshot = self.repo.save_monitor_snapshot({
            "login_status": "logged_in",
            "nickname": "测试账号",
            "metrics": {"fans": "12"},
            "xsecToken": "should-never-be-persisted",
            "cookie": "secret",
        })
        self.assertNotIn("xsecToken", snapshot)
        self.assertNotIn("cookie", snapshot)
        stored = self.repo.get_monitor_history()[0]
        self.assertNotIn("xsecToken", stored)
        self.assertNotIn("cookie", stored)


class _BytesReader:
    def __init__(self, value):
        self.value = value
        self.read_once = False

    def read(self, size=-1):
        if self.read_once:
            return b""
        self.read_once = True
        return self.value


if __name__ == "__main__":
    unittest.main()
