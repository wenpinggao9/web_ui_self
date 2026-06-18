"""步骤⑱ 大模型适配器 —— 所有 AI 能力的底层驱动.

封装 OpenAI 兼容接口, 提供:
  - complete_json(): 返回 LLMResult(含解析后的 dict + 原始文本 + 完整提示词)
  - complete_text(): 返回纯文本
  - 失败重试 + JSON 提取 (复用 core.llm_client._extract_json)
  - 观测回调 (on_call): 把完整提示词和原始返回交给可观测性收集器
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# 复用旧实现的 JSON 提取 (字符串感知括号计数法)
from ..llm_client import _extract_json


@dataclass
class LLMResult:
    """一次 LLM 调用的结构化结果, 同时保留原始文本便于排查."""

    data: dict[str, Any]
    raw: str
    system: str
    user: str


# 观测回调签名: (stage, system, user, raw) -> None
ObserveFn = Callable[[str, str, str, str], None]


class LLMAdapter:
    """统一封装不同 OpenAI 兼容模型供应商的调用细节."""

    def __init__(
        self,
        llm_cfg: dict[str, Any],
        max_retries: int = 2,
        retry_delay_s: float = 1.0,
        observe: Optional[ObserveFn] = None,
    ) -> None:
        self._cfg = llm_cfg
        self._max_retries = max_retries
        self._retry_delay = retry_delay_s
        self._observe = observe
        self._build_client()

    def _build_client(self) -> None:
        from openai import OpenAI

        # provider 决定读取 llm.<provider> 下的 base_url/model/api_key.
        provider = (self._cfg.get("provider") or "ollama").strip().lower()
        sub = self._cfg.get(provider, {}) or {}
        base_url = (sub.get("base_url") or "").rstrip("/")
        if base_url and not base_url.endswith("/v1"):
            # OpenAI SDK 需要 base_url 指向兼容协议的 /v1 根路径.
            base_url += "/v1"
        self.provider = provider
        self.model = sub.get("model", "")
        self._timeout = int(sub.get("timeout_seconds", 120))
        self._max_tokens = int(self._cfg.get("max_tokens", 2048))
        self._temperature = float(self._cfg.get("temperature", 0.0))
        self._use_response_format = provider != "ollama"
        self._client = OpenAI(
            # Ollama 本地兼容接口也要求 api_key 字段, 用占位值即可.
            api_key=sub.get("api_key") or "ollama",
            base_url=base_url or "http://localhost:11434/v1",
            timeout=self._timeout,
        )

    # ---------- 核心调用 ----------
    def _chat(self, system: str, user: str, examples: Optional[list[dict]], json_mode: bool) -> str:
        """构造 messages 并调用 chat.completions, 返回模型原始文本."""
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for ex in examples or []:
            messages.append({"role": "user", "content": ex["input"]})
            messages.append({"role": "assistant", "content": ex["output"]
                             if isinstance(ex["output"], str) else _dumps(ex["output"])})
        messages.append({"role": "user", "content": user})

        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
            messages=messages,
        )
        if json_mode and self._use_response_format:
            # 非 Ollama 供应商优先请求 JSON 模式; 不支持时会自动降级.
            kwargs["response_format"] = {"type": "json_object"}

        last_err: Optional[Exception] = None
        for attempt in range(self._max_retries + 1):
            try:
                resp = self._client.chat.completions.create(**kwargs)
                return resp.choices[0].message.content or ""
            except Exception as e:  # noqa: BLE001
                last_err = e
                err = str(e).lower()
                # response_format 不被支持时降级一次
                if "response_format" in kwargs and "response_format" in err:
                    kwargs.pop("response_format", None)
                    continue
                if attempt < self._max_retries:
                    # 网络抖动/网关短暂失败时做简单固定间隔重试.
                    time.sleep(self._retry_delay)
                    continue
                raise
        raise last_err  # type: ignore[misc]

    def complete_json(
        self,
        stage: str,
        system: str,
        user: str,
        examples: Optional[list[dict]] = None,
    ) -> LLMResult:
        last_raw = ""
        last_err: Optional[Exception] = None
        # JSON 解析失败时重试整轮调用 (模型偶发输出未转义引号等非法 JSON)
        for attempt in range(self._max_retries + 1):
            raw = self._chat(system, user, examples, json_mode=True)
            last_raw = raw
            if self._observe:
                # 观测回调记录完整 prompt 和 raw, 用于报告/调试.
                self._observe(stage, system, user, raw)
            try:
                data = _extract_json(raw)
                return LLMResult(data=data, raw=raw, system=system, user=user)
            except Exception as e:  # noqa: BLE001
                last_err = e
                continue
        raise ValueError(f"{stage} 返回非法JSON, 已重试{self._max_retries}次: {last_err}\n原始: {last_raw[:300]}")

    def complete_text(self, stage: str, system: str, user: str) -> str:
        """纯文本调用, 用于不要求 JSON schema 的场景."""
        raw = self._chat(system, user, None, json_mode=False)
        if self._observe:
            self._observe(stage, system, user, raw)
        return raw


def _dumps(obj: Any) -> str:
    """few-shot 示例中的 dict/list 输出转成中文友好的 JSON 字符串."""
    import json
    return json.dumps(obj, ensure_ascii=False)
