"""OpenAI-compatible LLM 协议和实现。默认不启用，不能执行工具。"""
import base64
from dataclasses import dataclass, field
import hashlib
import json
import re
import time
from typing import Protocol, TypeVar

import httpx
from pydantic import BaseModel

from .config import settings

T = TypeVar("T", bound=BaseModel)


@dataclass
class LLMResponse:
    model_id: str
    parsed: BaseModel | None
    raw_text: str = ""
    input_hash: str = ""
    latency_ms: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    error: str | None = None


class LLMProvider(Protocol):
    def complete_json(self, *, system_prompt: str, user_prompt: str, schema: type[T], task: str) -> LLMResponse:
        ...


def _hash_input(system_prompt: str, user_prompt: str, task: str) -> str:
    return hashlib.sha256(f"{task}\n{system_prompt}\n{user_prompt}".encode()).hexdigest()


class HttpxOpenAICompatibleProvider:
    def __init__(self, *, base_url: str | None = None, api_key: str | None = None, model: str | None = None):
        self.base_url = (base_url or settings.llm_base_url).rstrip("/")
        self.api_key = api_key or settings.llm_api_key
        self.model = model or settings.llm_model

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    def complete_json(self, *, system_prompt: str, user_prompt: str, schema: type[T], task: str) -> LLMResponse:
        input_hash = _hash_input(system_prompt, user_prompt, task)
        started = time.perf_counter()
        if not self.base_url or not self.api_key or not self.model:
            return LLMResponse(self.model or "", None, input_hash=input_hash, error="LLM 配置不完整")
        schema_text = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        system_content = (
            f"{system_prompt}\n\n"
            "Return only one JSON object matching this JSON schema exactly. "
            "Do not add any field that is not in the schema.\n"
            f"{schema_text}"
        )
        payload = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": settings.llm_max_output_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_content},
                {"role": "user", "content": user_prompt},
            ],
        }
        try:
            response = httpx.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
                timeout=settings.llm_timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
            raw = data["choices"][0]["message"]["content"]
            parsed = schema.model_validate_json(raw)
            usage = data.get("usage") or {}
            return LLMResponse(self.model, parsed, raw, input_hash, int((time.perf_counter() - started) * 1000), usage)
        except Exception as exc:
            return LLMResponse(self.model, None, input_hash=input_hash, latency_ms=int((time.perf_counter() - started) * 1000), error=str(exc))

    def complete_vision_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        images: list[tuple[str, bytes]],
        schema: type[T],
        task: str,
        model: str | None = None,
    ) -> LLMResponse:
        """多模态：图片 + 文本，输出结构化 JSON。images 为 [(mime, bytes)]。"""
        input_hash = _hash_input(system_prompt, user_prompt, task)
        started = time.perf_counter()
        if not self.base_url or not self.api_key:
            return LLMResponse(model or self.model, None, input_hash=input_hash, error="LLM 配置不完整")
        vision_model = model or settings.llm_vision_model
        schema_text = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        system_content = (
            f"{system_prompt}\n\n"
            "Return only one JSON object matching this JSON schema exactly. "
            "Do not add any field that is not in the schema.\n"
            f"{schema_text}"
        )
        content: list[dict] = [{"type": "text", "text": user_prompt}]
        for mime, data in images:
            b64 = base64.b64encode(data).decode()
            content.append(
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
            )
        payload = {
            "model": vision_model,
            "temperature": 0,
            "max_tokens": max(settings.llm_max_output_tokens, 2000),
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_content},
                {"role": "user", "content": content},
            ],
        }
        try:
            response = httpx.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
                timeout=settings.llm_timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
            raw = data["choices"][0]["message"]["content"]
            parsed = schema.model_validate_json(_extract_json_object(raw))
            usage = data.get("usage") or {}
            return LLMResponse(vision_model, parsed, raw, input_hash, int((time.perf_counter() - started) * 1000), usage)
        except Exception as exc:
            return LLMResponse(vision_model, None, input_hash=input_hash, latency_ms=int((time.perf_counter() - started) * 1000), error=str(exc))

    def complete_with_tools(self, messages: list[dict], tools: list[dict], *, model: str | None = None) -> dict:
        """调用 LLM 并启用 function calling。返回 API 原始响应 dict。"""
        if not self.base_url or not self.api_key:
            raise RuntimeError("LLM 配置不完整")
        payload = {
            "model": model or self.model,
            "temperature": 0,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
        }
        try:
            response = httpx.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
                timeout=settings.llm_timeout_seconds,
            )
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            raise RuntimeError(f"function calling 调用失败：{exc}") from exc


def _extract_json_object(text: str) -> str:
    """从可能被 markdown 代码块或推理文字包裹的输出里提取 JSON 对象。"""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text


class FakeLLMProvider:
    def __init__(self, response: BaseModel | None = None, *, model_id: str = "fake-model", error: str | None = None):
        self.response = response
        self.model_id = model_id
        self.error = error
        self.calls: list[dict[str, str]] = []

    def complete_json(self, *, system_prompt: str, user_prompt: str, schema: type[T], task: str) -> LLMResponse:
        input_hash = _hash_input(system_prompt, user_prompt, task)
        self.calls.append({"task": task, "system": system_prompt, "user": user_prompt})
        if self.error:
            return LLMResponse(self.model_id, None, input_hash=input_hash, error=self.error)
        if self.response is None:
            return LLMResponse(self.model_id, None, input_hash=input_hash, error="Fake Provider 未配置响应")
        try:
            parsed = schema.model_validate(self.response.model_dump())
        except Exception as exc:
            return LLMResponse(self.model_id, None, input_hash=input_hash, error=str(exc))
        return LLMResponse(self.model_id, parsed, parsed.model_dump_json(), input_hash, latency_ms=1)
