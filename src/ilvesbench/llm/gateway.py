from __future__ import annotations

from abc import ABC, abstractmethod
import json
from urllib import request

from ilvesbench.config import LLMConfig
from ilvesbench.models import LLMResult


class LLMGateway(ABC):
    @abstractmethod
    def generate(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        max_tokens: int = 256,
    ) -> LLMResult:
        raise NotImplementedError

    @classmethod
    def from_config(cls, config: LLMConfig) -> "LLMGateway":
        backend = config.backend.lower()
        if backend in {"aviary", "openai_compatible", "openai-compatible"}:
            return OpenAICompatibleGateway(config)
        raise ValueError(f"Unsupported LLM backend: {config.backend}")


class OpenAICompatibleGateway(LLMGateway):
    def __init__(self, config: LLMConfig) -> None:
        self._config = config

    def generate(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        max_tokens: int = 256,
    ) -> LLMResult:
        payload = json.dumps(
            {
                "model": model or self._config.model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": 0,
                "stream": False,
            }
        ).encode("utf-8")
        req = request.Request(
            self._config.base_url,
            data=payload,
            headers=self._build_headers(),
            method="POST",
        )
        with request.urlopen(req, timeout=self._config.timeout_seconds) as response:
            raw = json.loads(response.read().decode("utf-8"))

        message = raw["choices"][0].get("message", {})
        content = message.get("content", "")
        if isinstance(content, list):
            content = "\n".join(str(item.get("text", item)) if isinstance(item, dict) else str(item) for item in content)
        if content is None:
            content = ""
        return LLMResult(
            backend=self._config.backend,
            model=model or self._config.model,
            response_text=content,
            raw_response=raw,
        )

    def _build_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._config.api_key:
            headers["Authorization"] = f"Bearer {self._config.api_key}"
        return headers
