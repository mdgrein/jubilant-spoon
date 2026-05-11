"""
Alibaba vendor backend for harness_common.call_model.

Calls Alibaba DashScope (Qwen models) via the OpenAI-compatible API.
Requires ALIBABA_API_KEY env var.
"""

import logging
import os

logger = logging.getLogger(__name__)

VENDOR = "alibaba"
BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"


def _thinking_supported(model_name: str) -> bool:
    """Qwen3 models support thinking mode; earlier series do not."""
    return model_name.lower().startswith("qwen3")


def call_qwen(prompt: str, model: str | None = None) -> tuple[str, int, int]:
    """Call Alibaba DashScope (Qwen) via the OpenAI-compatible API.

    Requires ALIBABA_API_KEY env var.
    Model defaults to DEFAULT_MODEL; override with the model parameter.

    Returns (text, tokens_in, tokens_out).
    """
    import openai  # optional dep; only imported when vendor=alibaba

    api_key = os.environ.get("ALIBABA_API_KEY")
    if not api_key:
        raise RuntimeError("ALIBABA_API_KEY env var not set")

    if not model:
        raise ValueError("model must be specified for alibaba vendor")
    model_name = model
    client = openai.OpenAI(api_key=api_key, base_url=BASE_URL)
    response_chunks: list[str] = []
    tokens_in = 0
    tokens_out = 0
    with client.chat.completions.create(
        model=model_name,
        messages=[{"role": "user", "content": prompt}],
        stream=True,
        stream_options={"include_usage": True},
        extra_body={"enable_thinking": _thinking_supported(model_name)},
    ) as stream:
        for chunk in stream:
            # The final chunk carries usage; earlier chunks have usage=None.
            if chunk.usage:
                tokens_in = chunk.usage.prompt_tokens or 0
                tokens_out = chunk.usage.completion_tokens or 0

            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta

            # Reasoning phase — log only, do not include in return value.
            thinking = getattr(delta, "reasoning_content", None) or ""
            if thinking:
                logger.model("[thinking] %s", thinking)  # type: ignore[attr-defined]
                print(thinking, end="", flush=True)

            # Response phase — collected and returned.
            content = delta.content or ""
            if content:
                logger.model(content)  # type: ignore[attr-defined]
                print(content, end="", flush=True)
                response_chunks.append(content)

    return "".join(response_chunks), tokens_in, tokens_out
