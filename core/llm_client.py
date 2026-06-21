"""统一 LLM 客户端 —— 支持 Ollama / MiniMax / Opus.

三者均走 OpenAI 兼容协议 (/v1/chat/completions), 通过 base_url + api_key + model 区分.
配置来自 config.yaml 的 llm 段.
"""
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import Any


class BaseLLMClient(ABC):
    @abstractmethod
    def chat_json(self, system: str, examples: list[dict], user: str) -> dict[str, Any]: ...

    @property
    @abstractmethod
    def model(self) -> str: ...

    @property
    @abstractmethod
    def provider(self) -> str: ...


def _salvage_ok_json(text: str) -> dict[str, Any] | None:
    """reason 内未转义引号导致 JSON 非法时, 仅 salvage ok/reason."""
    ok_m = re.search(r'"ok"\s*:\s*(true|false)', text, re.IGNORECASE)
    if not ok_m:
        return None
    ok = ok_m.group(1).lower() == "true"
    reason_m = re.search(r'"reason"\s*:\s*"(.+)"\s*\}?\s*$', text, re.DOTALL)
    reason = reason_m.group(1) if reason_m else ""
    return {"ok": ok, "reason": reason or "(JSON 已修复解析)"}


def _extract_json(text: str) -> dict[str, Any]:
    """从 LLM 回复里提取第一个 JSON 对象, 容忍 ```json 包裹 / 思考标签 / 多余文本."""
    text = (text or "").strip()
    # 剥掉 <think>...</think> (DeepSeek-R1 等推理模型)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start < 0:
            raise
        # 按括号匹配找到第一个完整的 JSON 对象
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if escape:
                escape = False
                continue
            if ch == "\\":
                if in_string:
                    escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break
        salvaged = _salvage_ok_json(text)
        if salvaged is not None:
            return salvaged
        # 兜底: 简单取首尾
        end = text.rfind("}")
        if end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                pass
        salvaged = _salvage_ok_json(text)
        if salvaged is not None:
            return salvaged
        raise


class _OpenAICompatClient(BaseLLMClient):
    """OpenAI 兼容协议客户端, 三 provider 共用."""

    def __init__(
        self,
        provider: str,
        base_url: str,
        api_key: str,
        model: str,
        timeout: int,
        max_tokens: int,
        temperature: float,
        use_response_format: bool = True,
    ) -> None:
        from openai import OpenAI

        if not base_url:
            raise RuntimeError(f"{provider}.base_url 未在 config.yaml 配置")
        # 自动补 /v1
        normalized = base_url.rstrip("/")
        if not normalized.endswith("/v1"):
            normalized = normalized + "/v1"
        self._client = OpenAI(
            api_key=api_key or "ollama",   # ollama 无需 key, SDK 要求非空占位
            base_url=normalized,
            timeout=timeout,
        )
        self._provider = provider
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._use_response_format = use_response_format

    @property
    def model(self) -> str:
        return self._model

    @property
    def provider(self) -> str:
        return self._provider

    def chat_json(self, system: str, examples: list[dict], user: str) -> dict[str, Any]:
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for ex in examples:
            messages.append({"role": "user", "content": ex["input"]})
            messages.append({"role": "assistant", "content": json.dumps(ex["output"], ensure_ascii=False)})
        messages.append({"role": "user", "content": user})

        kwargs: dict[str, Any] = dict(
            model=self._model,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
            messages=messages,
        )
        if self._use_response_format:
            kwargs["response_format"] = {"type": "json_object"}

        try:
            print(f"🤖 请求LLM [{self._model}]")
            resp = self._client.chat.completions.create(**kwargs)
        except Exception as e:
            # 模型不支持 response_format 时自动降级
            if self._use_response_format and "response_format" in str(e).lower():
                kwargs.pop("response_format", None)
                print(f"🤖 请求LLM [{self._model}] (降级无json模式)")
                resp = self._client.chat.completions.create(**kwargs)
            else:
                raise
        text = resp.choices[0].message.content or ""
        return _extract_json(text)


def build_llm_client(llm_cfg: dict[str, Any]) -> BaseLLMClient:
    """根据 config.yaml 的 llm 配置段构建客户端."""
    provider = (llm_cfg.get("provider") or "ollama").strip().lower()
    max_tokens = int(llm_cfg.get("max_tokens", 2048))
    temperature = float(llm_cfg.get("temperature", 0.0))

    if provider == "ollama":
        c = llm_cfg.get("ollama", {})
        return _OpenAICompatClient(
            provider="ollama",
            base_url=c.get("base_url", "http://localhost:11434"),
            api_key="",
            model=c.get("model", "deepseek-r1:7b"),
            timeout=int(c.get("timeout_seconds", 120)),
            max_tokens=max_tokens,
            temperature=temperature,
            use_response_format=False,   # 多数本地模型不支持
        )

    if provider == "minimax":
        c = llm_cfg.get("minimax", {})
        if not c.get("api_key"):
            raise RuntimeError("config.yaml 中 llm.minimax.api_key 未配置")
        return _OpenAICompatClient(
            provider="minimax",
            base_url=c.get("base_url", ""),
            api_key=c["api_key"],
            model=c.get("model", "MiniMax-M2.5"),
            timeout=int(c.get("timeout_seconds", 60)),
            max_tokens=max_tokens,
            temperature=temperature,
            use_response_format=True,
        )

    if provider == "opus":
        c = llm_cfg.get("opus", {})
        if not c.get("api_key"):
            raise RuntimeError("config.yaml 中 llm.opus.api_key 未配置")
        return _OpenAICompatClient(
            provider="opus",
            base_url=c.get("base_url", ""),
            api_key=c["api_key"],
            model=c.get("model", "claude-opus-4-6"),
            timeout=int(c.get("timeout_seconds", 120)),
            max_tokens=max_tokens,
            temperature=temperature,
            use_response_format=True,
        )

    raise ValueError(f"未知 LLM provider: {provider} (可选: ollama | minimax | opus)")
