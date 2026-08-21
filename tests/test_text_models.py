import io
import json
import unittest
from unittest.mock import patch

from app.text_models import TextModelClient, parse_json_value


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class TextModelTests(unittest.TestCase):
    def test_unconfigured_status_is_explicit(self):
        client = TextModelClient(endpoint="", model="Qwen3-4B")
        self.assertFalse(client.status()["enabled"])
        self.assertFalse(client.status()["configured"])

    def test_online_status_reads_openai_models(self):
        client = TextModelClient(
            endpoint="http://127.0.0.1:7061/v1",
            model="Qwen3-4B",
            api_key="secret",
        )
        response = _Response(b'{"data":[{"id":"Qwen3-4B"}]}')
        with patch("urllib.request.urlopen", return_value=response) as mocked:
            status = client.status()
        self.assertTrue(status["enabled"])
        self.assertEqual(status["message"], "文本分镜规划服务在线")
        self.assertEqual(mocked.call_args.args[0].get_header("Authorization"), "Bearer secret")

    def test_deepseek_request_uses_json_mode_and_disables_thinking(self):
        client = TextModelClient(
            endpoint="https://api.deepseek.com",
            model="deepseek-v4-flash",
            api_key="secret",
            provider="deepseek",
        )
        body = {
            "choices": [{"message": {"content": json.dumps({"shots": [{"narration": "开场"}]})}}]
        }
        with patch("urllib.request.urlopen", return_value=_Response(json.dumps(body).encode())) as mocked:
            result = client.plan_shots("这是一段开场文案")
        request = mocked.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(result["shots"][0]["narration"], "开场")
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["max_tokens"], 4096)
        self.assertNotIn("chat_template_kwargs", payload)

    def test_deepseek_retries_empty_json_content_in_text_mode(self):
        client = TextModelClient(
            endpoint="https://api.deepseek.com",
            model="deepseek-v4-flash",
            api_key="secret",
            provider="deepseek",
        )
        empty = {"choices": [{"finish_reason": "stop", "message": {"content": ""}}]}
        valid = {
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": '{"shots":[{"narration":"重试成功"}]}'},
            }]
        }
        responses = [
            _Response(json.dumps(empty).encode()),
            _Response(json.dumps(valid).encode()),
        ]
        with patch("urllib.request.urlopen", side_effect=responses) as mocked, patch("time.sleep"):
            result = client.plan_shots("测试重试")
        retry_payload = json.loads(mocked.call_args_list[1].args[0].data)
        self.assertEqual(result["shots"][0]["narration"], "重试成功")
        self.assertNotIn("response_format", retry_payload)
        self.assertIn("必须返回非空 JSON", retry_payload["messages"][0]["content"])

    def test_deepseek_content_filter_does_not_retry(self):
        client = TextModelClient(
            endpoint="https://api.deepseek.com",
            model="deepseek-v4-flash",
            api_key="secret",
            provider="deepseek",
        )
        blocked = {
            "choices": [{"finish_reason": "content_filter", "message": {"content": ""}}]
        }
        with patch(
            "urllib.request.urlopen",
            return_value=_Response(json.dumps(blocked).encode()),
        ) as mocked:
            with self.assertRaisesRegex(RuntimeError, "内容过滤"):
                client.plan_shots("测试")
        self.assertEqual(mocked.call_count, 1)

    def test_local_request_disables_qwen_thinking(self):
        client = TextModelClient(
            endpoint="http://127.0.0.1:7061/v1",
            model="Qwen3-4B",
            provider="local",
        )
        body = {"choices": [{"message": {"content": '{"shots":[]}'}}]}
        with patch("urllib.request.urlopen", return_value=_Response(json.dumps(body).encode())) as mocked:
            client.plan_shots("短文案")
        payload = json.loads(mocked.call_args.args[0].data)
        self.assertEqual(payload["chat_template_kwargs"], {"enable_thinking": False})
        self.assertNotIn("thinking", payload)

    def test_json_code_fence(self):
        result = parse_json_value('```json\n[{"narration":"开场"}]\n```')
        self.assertEqual(result[0]["narration"], "开场")


if __name__ == "__main__":
    unittest.main()
