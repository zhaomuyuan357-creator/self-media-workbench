import unittest

from workbench.runtime import RuntimeConfigManager


class RuntimeConfigManagerTests(unittest.TestCase):
    def test_public_profiles_exclude_credentials_and_errors_are_redacted(self):
        runtime = RuntimeConfigManager([
            {
                "id": "primary", "name": "test", "provider": "custom",
                "base_url": "https://api.example/v1", "protocol": "chat_completions",
                "model": "model-1", "api_key": "private-test-key",
            },
        ], toapis={"api_key": "toapis-private-key", "region": "com", "image_model": "image-1"})
        public = runtime.list_generator_profiles_public()
        self.assertTrue(public[0]["configured"])
        self.assertNotIn("api_key", public[0])
        self.assertEqual(runtime.redact("request private-test-key / toapis-private-key failed"),
                         "request [已隐藏] / [已隐藏] failed")

    def test_profile_updates_preserve_runtime_reference_and_fallback_flag(self):
        original = [{"id": "old", "api_key": "old-key", "model": "old"}]
        runtime = RuntimeConfigManager(original, auto_fallback=True)
        runtime.save_generator_profiles([{"id": "new", "api_key": "new-key", "model": "new"}], False)
        self.assertEqual(original[0]["id"], "new")
        self.assertEqual(runtime.get_active_generator_credentials()[0]["api_key"], "new-key")
        self.assertFalse(runtime.get_auto_fallback())


if __name__ == "__main__":
    unittest.main()
