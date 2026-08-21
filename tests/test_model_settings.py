import unittest

from app.server import validate_text_model_payload


class ModelSettingsTests(unittest.TestCase):
    def test_deepseek_uses_official_endpoint_and_requires_key(self):
        with self.assertRaisesRegex(ValueError, "API Key"):
            validate_text_model_payload({"provider": "deepseek", "model": "deepseek-v4-flash"})
        config = validate_text_model_payload({
            "provider": "deepseek",
            "endpoint": "https://wrong.example/v1",
            "model": "deepseek-v4-flash",
            "api_key": "secret",
        })
        self.assertEqual(config["endpoint"], "https://api.deepseek.com")

    def test_custom_provider_accepts_openai_compatible_endpoint(self):
        config = validate_text_model_payload({
            "provider": "custom",
            "endpoint": "https://models.example/v1/",
            "model": "planner-model",
        })
        self.assertEqual(config["endpoint"], "https://models.example/v1")
        self.assertEqual(config["model"], "planner-model")

    def test_local_provider_discards_api_key(self):
        config = validate_text_model_payload({
            "provider": "local", "model": "Qwen3-4B", "api_key": "must-not-persist"
        })
        self.assertEqual(config["api_key"], "")


if __name__ == "__main__":
    unittest.main()
