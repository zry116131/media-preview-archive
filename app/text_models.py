from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request


def parse_json_value(raw: str) -> object:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.I)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        for opening, closing in (("[", "]"), ("{", "}")):
            start = cleaned.find(opening)
            end = cleaned.rfind(closing)
            if start >= 0 and end > start:
                try:
                    return json.loads(cleaned[start:end + 1])
                except json.JSONDecodeError:
                    continue
    raise ValueError("语言模型未返回有效的分镜 JSON")


class TextModelClient:
    def __init__(
        self,
        endpoint: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        provider: str | None = None,
    ):
        self.endpoint = (endpoint or os.environ.get("TEXT_LLM_ENDPOINT", "")).rstrip("/")
        self.model = model or os.environ.get("TEXT_LLM_MODEL", "Qwen3-4B")
        self.api_key = api_key if api_key is not None else os.environ.get("TEXT_LLM_API_KEY", "")
        self.provider = provider or os.environ.get("TEXT_LLM_PROVIDER", "local")

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    @property
    def enabled(self) -> bool:
        return bool(self.endpoint)

    def status(self) -> dict[str, object]:
        if not self.endpoint:
            return {
                "enabled": False,
                "configured": False,
                "model": self.model,
                "message": "独立文本模型尚未配置",
            }
        try:
            request = urllib.request.Request(
                f"{self.endpoint}/models", headers=self._headers(), method="GET"
            )
            with urllib.request.urlopen(request, timeout=8) as response:
                payload = json.load(response)
            available = [item.get("id") for item in payload.get("data", [])]
            return {
                "enabled": self.model in available,
                "configured": True,
                "model": self.model,
                "message": (
                    "文本分镜规划服务在线"
                    if self.model in available
                    else f"接口在线，但没有找到模型 {self.model}"
                ),
            }
        except Exception as exc:
            return {
                "enabled": False,
                "configured": True,
                "model": self.model,
                "message": f"文本分镜规划服务不可用：{exc}",
            }

    def plan_shots(self, script: str) -> object:
        if not self.endpoint:
            raise RuntimeError("独立文本模型尚未配置")
        prompt = (
            "你是视频编导。把下面中文口播文案拆成可执行分镜，输出纯 JSON 对象，不要 Markdown。"
            "对象格式为 {\"shots\":[...]}。"
            "每项字段：narration（对应原文）、visual_request（具体画面需求）、"
            "shot_type（景别或运镜）、keywords（5到8个适合检索现场素材的视觉关键词）。"
            "keywords 必须是 JSON 字符串数组，每个元素只放一个2到8字的词，不要用空格合并多个词。"
            "不要改写原意，不要虚构素材中一定存在的品牌。最多20个分镜。\n\n文案：\n"
            + script[:6000]
        )
        payload = {
            "model": self.model,
            "temperature": 0.1,
            "max_tokens": 4096 if self.provider == "deepseek" else 1800,
            "messages": [{"role": "user", "content": prompt}],
        }
        if self.provider == "local":
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        elif self.provider == "deepseek":
            payload["thinking"] = {"type": "disabled"}
            payload["response_format"] = {"type": "json_object"}
        attempts = 2 if self.provider == "deepseek" else 1
        service_name = "DeepSeek" if self.provider == "deepseek" else "模型服务"
        last_error: Exception | None = None
        for attempt in range(attempts):
            request_payload = dict(payload)
            if self.provider == "deepseek" and attempt > 0:
                request_payload.pop("response_format", None)
                request_payload["messages"] = [{
                    "role": "user",
                    "content": prompt + (
                        "\n\n必须返回非空 JSON，直接以 {\"shots\":[ 开头，"
                        "不要输出解释或 Markdown。"
                    ),
                }]
            request = urllib.request.Request(
                f"{self.endpoint}/chat/completions",
                data=json.dumps(request_payload).encode("utf-8"),
                headers=self._headers(),
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=300) as response:
                    result = json.load(response)
                choice = result.get("choices", [{}])[0]
                finish_reason = choice.get("finish_reason") or "unknown"
                if finish_reason == "content_filter":
                    raise RuntimeError("文案触发 DeepSeek 内容过滤")
                content = (choice.get("message") or {}).get("content") or ""
                if not str(content).strip():
                    raise ValueError(f"DeepSeek 返回空内容（{finish_reason}）")
                return parse_json_value(str(content))
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    body = json.loads(exc.read().decode("utf-8", errors="replace"))
                    detail = str(body.get("error", {}).get("message") or "")[:240]
                except Exception:
                    detail = ""
                last_error = RuntimeError(
                    f"{service_name} HTTP {exc.code}" + (f"：{detail}" if detail else "")
                )
                if exc.code not in {429, 500, 503}:
                    raise last_error from exc
            except RuntimeError:
                raise
            except Exception as exc:
                last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.4)
        if self.provider == "deepseek":
            raise RuntimeError(f"DeepSeek 两次生成均失败：{last_error}") from last_error
        raise RuntimeError(f"模型生成失败：{last_error}") from last_error
