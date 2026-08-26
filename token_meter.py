"""Accumulate LLM token usage across LiteLLM calls for a research run."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from typing import Any

import litellm

_CALLBACK_INSTALLED = False


class TokenMeter:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.calls_without_usage = 0
        self.by_model: dict[str, dict[str, int]] = defaultdict(
            lambda: {
                "calls": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "calls_without_usage": 0,
            }
        )

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def record(
        self,
        *,
        model: str | None,
        prompt_tokens: int,
        completion_tokens: int,
        had_usage: bool,
    ) -> None:
        model_key = model or "(unknown)"
        self.calls += 1
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        if not had_usage:
            self.calls_without_usage += 1

        bucket = self.by_model[model_key]
        bucket["calls"] += 1
        bucket["prompt_tokens"] += prompt_tokens
        bucket["completion_tokens"] += completion_tokens
        bucket["total_tokens"] = bucket["prompt_tokens"] + bucket["completion_tokens"]
        if not had_usage:
            bucket["calls_without_usage"] += 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "calls_without_usage": self.calls_without_usage,
            "by_model": {name: dict(stats) for name, stats in sorted(self.by_model.items())},
        }

    def snapshot(self) -> dict[str, Any]:
        return deepcopy(self.as_dict())

    def format_one_line(self) -> str:
        return (
            f"tokens: calls={self.calls} "
            f"in={self.prompt_tokens} out={self.completion_tokens} "
            f"total={self.total_tokens}"
            + (
                f" (no_usage={self.calls_without_usage})"
                if self.calls_without_usage
                else ""
            )
        )

    def format_markdown(self) -> str:
        lines = [
            "",
            "---",
            "",
            "## Uso de tokens (LLM)",
            "",
            f"- Chamadas: {self.calls}",
            (
                f"- Input: {self.prompt_tokens} | Output: {self.completion_tokens} "
                f"| Total: {self.total_tokens}"
            ),
        ]
        if self.calls_without_usage:
            lines.append(
                f"- Chamadas sem usage reportado: {self.calls_without_usage}"
            )
        if self.by_model:
            lines.append("- Por modelo:")
            for name, stats in sorted(self.by_model.items()):
                lines.append(
                    f"  - `{name}`: calls={stats['calls']} "
                    f"in={stats['prompt_tokens']} out={stats['completion_tokens']} "
                    f"total={stats['total_tokens']}"
                )
        lines.append("")
        return "\n".join(lines)


meter = TokenMeter()


def _usage_fields(usage: Any) -> tuple[int, int, bool]:
    if usage is None:
        return 0, 0, False
    if isinstance(usage, dict):
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
    else:
        prompt = getattr(usage, "prompt_tokens", None)
        completion = getattr(usage, "completion_tokens", None)
    if prompt is None and completion is None:
        return 0, 0, False
    return int(prompt or 0), int(completion or 0), True


def _on_litellm_success(
    kwargs: dict[str, Any],
    completion_response: Any,
    start_time: Any,
    end_time: Any,
) -> None:
    del start_time, end_time
    usage = getattr(completion_response, "usage", None)
    if usage is None and isinstance(completion_response, dict):
        usage = completion_response.get("usage")
    prompt, completion, had_usage = _usage_fields(usage)
    model = None
    if isinstance(kwargs, dict):
        model = kwargs.get("model")
        litellm_params = kwargs.get("litellm_params") or {}
        if not model and isinstance(litellm_params, dict):
            model = litellm_params.get("model")
    meter.record(
        model=str(model) if model else None,
        prompt_tokens=prompt,
        completion_tokens=completion,
        had_usage=had_usage,
    )


def install_litellm_callback() -> None:
    """Register the global LiteLLM success callback once."""
    global _CALLBACK_INSTALLED
    if _CALLBACK_INSTALLED:
        return
    callbacks = litellm.success_callback
    if not isinstance(callbacks, list):
        litellm.success_callback = [_on_litellm_success]
    elif _on_litellm_success not in callbacks:
        callbacks.append(_on_litellm_success)
    _CALLBACK_INSTALLED = True
