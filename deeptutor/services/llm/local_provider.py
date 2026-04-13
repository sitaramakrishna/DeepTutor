"""
Local LLM Provider
==================

Handles all local/self-hosted LLM calls (LM Studio, Ollama, vLLM, llama.cpp, etc.)
Uses aiohttp instead of httpx for better compatibility with local servers.

Key features:
- Uses aiohttp (httpx has known 502 issues with some local servers like LM Studio)
- Handles thinking tags (<think>) from reasoning models like Qwen
- Extended timeouts for potentially slower local inference
"""

from collections.abc import AsyncGenerator
import json
import logging
import time

import aiohttp

from .exceptions import LLMAPIError, LLMConfigError
from .query_context import get_query_context
from .telemetry import _log_complete_call, log_stream_call
from .utils import (
    build_auth_headers,
    build_chat_url,
    clean_thinking_tags,
    collect_model_names,
    extract_response_content,
    sanitize_url,
)

logger = logging.getLogger(__name__)


def _log_local_query(
    *,
    provider_name: str,
    model: str,
    messages: list[dict[str, object]],
    temperature: float,
    max_tokens: int | None,
    mode: str,
) -> None:
    """Log the query being sent to a local/custom LLM server."""
    ctx = get_query_context()
    parts = [f"QUERY | provider={provider_name}", f"model={model}", f"mode={mode}"]
    if ctx.agent:
        parts.append(f"agent={ctx.agent}")
    if ctx.stage:
        parts.append(f"stage={ctx.stage}")
    if ctx.capability:
        parts.append(f"capability={ctx.capability}")
    parts.append(f"temp={temperature}")
    if max_tokens is not None:
        parts.append(f"max_tokens={max_tokens}")
    parts.append(f"msgs={len(messages)}")
    logger.info(" ".join(parts))

    for i, msg in enumerate(messages):
        role = str(msg.get("role", "unknown"))
        content = msg.get("content", "")
        content_str = str(content) if not isinstance(content, list) else " ".join(
            str(b.get("text", "")) if isinstance(b, dict) and b.get("type") == "text" else "[image]"
            for b in content if isinstance(b, dict)
        )
        label = "QUERY.SYSTEM" if role == "system" else (
            "QUERY.USER" if role == "user" else f"QUERY.MSG[{i}]({role})"
        )
        logger.debug("%s | %s", label, content_str)


def _extract_usage_from_result(result: dict[str, object]) -> dict[str, int]:
    """Extract token usage from a local server JSON response."""
    # OpenAI-compatible format
    usage_raw = result.get("usage")
    if isinstance(usage_raw, dict):
        return {
            "prompt_tokens": int(usage_raw.get("prompt_tokens", 0) or 0),
            "completion_tokens": int(usage_raw.get("completion_tokens", 0) or 0),
            "total_tokens": int(usage_raw.get("total_tokens", 0) or 0),
        }
    # Ollama native format (fallback for non-OpenAI-compatible endpoints)
    prompt_eval = result.get("prompt_eval_count", 0)
    eval_count = result.get("eval_count", 0)
    if prompt_eval or eval_count:
        p = int(prompt_eval or 0)
        c = int(eval_count or 0)
        return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}
    return {}


def _extract_message_from_payload(payload: dict[str, object]) -> str:
    """Extract message content from a local provider payload.

    Args:
        payload: Provider response payload.
    Returns:
        Extracted content string.
    Raises:
        None.
    """
    if not payload:
        return ""

    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        choice = choices[0]
        for key in ("message", "delta"):
            if not isinstance(choice, dict):
                break
            part = choice.get(key)
            if part is not None:
                return extract_response_content(part)
        if isinstance(choice, dict) and "text" in choice:
            return str(choice.get("text") or "")

    if "message" in payload:
        return extract_response_content(payload.get("message"))

    return ""


# Extended timeout for local servers (may be slower than cloud)
DEFAULT_TIMEOUT = 300  # 5 minutes


async def complete(
    prompt: str,
    system_prompt: str = "You are a helpful assistant.",
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    messages: list[dict[str, str]] | None = None,
    **kwargs: object,
) -> str:
    """
    Complete a prompt using local LLM server.

    Uses aiohttp for better compatibility with local servers.

    Args:
        prompt: The user prompt (ignored if messages provided)
        system_prompt: System prompt for context
        model: Model name
        api_key: API key (optional for most local servers)
        base_url: Base URL for the local server
        messages: Pre-built messages array (optional)
        **kwargs: Additional parameters (temperature, max_tokens, etc.)

    Returns:
        str: The LLM response
    """
    if not base_url:
        raise LLMConfigError("base_url is required for local LLM provider")

    # Sanitize URL and build chat endpoint
    base_url = sanitize_url(base_url, model or "")
    url = build_chat_url(base_url)

    # Build headers using unified utility
    headers = build_auth_headers(api_key)

    # Build messages
    if messages:
        msg_list = messages
    else:
        msg_list = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]

    temperature_val = float(kwargs.get("temperature", 0.7))  # type: ignore[arg-type]
    max_tokens_val: int | None = int(kwargs["max_tokens"]) if kwargs.get("max_tokens") else None  # type: ignore[arg-type]

    _log_local_query(
        provider_name="local",
        model=model or "default",
        messages=msg_list,
        temperature=temperature_val,
        max_tokens=max_tokens_val,
        mode="complete",
    )

    # Build request data
    data = {
        "model": model or "default",
        "messages": msg_list,
        "temperature": temperature_val,
        "stream": False,
    }

    # Add optional parameters
    if max_tokens_val is not None:
        data["max_tokens"] = max_tokens_val

    timeout_value = kwargs.get("timeout", DEFAULT_TIMEOUT)
    timeout_seconds = (
        float(timeout_value) if isinstance(timeout_value, (int, float)) else DEFAULT_TIMEOUT
    )
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    t0 = time.perf_counter()
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=data, headers=headers) as response:
            if response.status != 200:
                error_text = await response.text()
                raise LLMAPIError(
                    f"Local LLM error: {error_text}",
                    status_code=response.status,
                    provider="local",
                )

            result = await response.json()
            elapsed = time.perf_counter() - t0

            usage = _extract_usage_from_result(result)
            _log_complete_call(
                provider="local",
                model=model or "default",
                usage=usage,
                cost=0.0,
                finish_reason=None,
                elapsed=elapsed,
                is_stream=False,
            )

            content = _extract_message_from_payload(result)
            content = clean_thinking_tags(content)
            if content:
                return content

            logger.warning("Local LLM returned no choices: %s", result)
            return ""


async def stream(
    prompt: str,
    system_prompt: str = "You are a helpful assistant.",
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    messages: list[dict[str, str]] | None = None,
    **kwargs: object,
) -> AsyncGenerator[str, None]:
    """
    Stream a response from local LLM server.

    Uses aiohttp for better compatibility with local servers.
    Falls back to non-streaming if streaming fails.

    Args:
        prompt: The user prompt (ignored if messages provided)
        system_prompt: System prompt for context
        model: Model name
        api_key: API key (optional for most local servers)
        base_url: Base URL for the local server
        messages: Pre-built messages array (optional)
        **kwargs: Additional parameters (temperature, max_tokens, etc.)

    Yields:
        str: Response chunks
    """
    if not base_url:
        raise LLMConfigError("base_url is required for local LLM provider")

    # Sanitize URL and build chat endpoint
    base_url = sanitize_url(base_url, model or "")
    url = build_chat_url(base_url)

    # Build headers using unified utility
    headers = build_auth_headers(api_key)

    # Build messages
    if messages:
        msg_list = messages
    else:
        msg_list = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]

    temperature_val = float(kwargs.get("temperature", 0.7))  # type: ignore[arg-type]
    max_tokens_val: int | None = int(kwargs["max_tokens"]) if kwargs.get("max_tokens") else None  # type: ignore[arg-type]

    _log_local_query(
        provider_name="local",
        model=model or "default",
        messages=msg_list,
        temperature=temperature_val,
        max_tokens=max_tokens_val,
        mode="stream",
    )

    # Build request data
    data = {
        "model": model or "default",
        "messages": msg_list,
        "temperature": temperature_val,
        "stream": True,
    }

    if max_tokens_val is not None:
        data["max_tokens"] = max_tokens_val

    timeout_value = kwargs.get("timeout", DEFAULT_TIMEOUT)
    timeout_seconds = (
        float(timeout_value) if isinstance(timeout_value, (int, float)) else DEFAULT_TIMEOUT
    )
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    stream_usage: dict[str, int] = {}
    t0 = time.perf_counter()
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=data, headers=headers) as response:
                if response.status != 200:
                    error_text = await response.text()
                    raise LLMAPIError(
                        f"Local LLM stream error: {error_text}",
                        status_code=response.status,
                        provider="local",
                    )

                # Track if we're inside a thinking block
                in_thinking_block = False
                thinking_buffer = ""

                async for line in response.content:
                    line_str = line.decode("utf-8").strip()

                    # Skip empty lines
                    if not line_str:
                        continue

                    # Handle SSE format
                    if line_str.startswith("data:"):
                        data_str = line_str[5:].strip()

                        if data_str == "[DONE]":
                            break

                        try:
                            chunk_data = json.loads(data_str)
                            # Capture usage from any chunk that contains it
                            chunk_usage = _extract_usage_from_result(chunk_data)
                            if chunk_usage:
                                stream_usage = chunk_usage
                            content = _extract_message_from_payload(chunk_data)
                            if content:
                                # Handle thinking tags in streaming
                                if "<think>" in content:
                                    in_thinking_block = True
                                    # Handle case where content has text BEFORE <think>
                                    parts = content.split("<think>", 1)
                                    if parts[0]:
                                        yield parts[0]
                                    thinking_buffer = "<think>" + parts[1]

                                    # Check if closed immediately in same chunk
                                    if "</think>" in thinking_buffer:
                                        cleaned = clean_thinking_tags(thinking_buffer)
                                        if cleaned:
                                            yield cleaned
                                        thinking_buffer = ""
                                        in_thinking_block = False
                                    continue
                                elif in_thinking_block:
                                    thinking_buffer += content
                                    if "</think>" in thinking_buffer:
                                        # Block finished
                                        cleaned = clean_thinking_tags(thinking_buffer)
                                        if cleaned:
                                            yield cleaned
                                        in_thinking_block = False
                                        thinking_buffer = ""
                                    continue
                                else:
                                    yield content

                        except json.JSONDecodeError:
                            # Log and skip malformed JSON chunks
                            logger.warning(
                                "Skipping malformed JSON chunk: %s...",
                                data_str[:50],
                            )
                            continue

                    # Some servers don't use SSE format
                    elif line_str.startswith("{"):
                        try:
                            chunk_data = json.loads(line_str)
                            content = _extract_message_from_payload(chunk_data)
                            if content:
                                # TODO: Implement <think> tag parsing for non-SSE JSON streams if supported
                                yield content
                        except json.JSONDecodeError:
                            pass

    except LLMAPIError:
        raise  # Re-raise LLM errors as-is
    except Exception as e:
        # Streaming failed, fall back to non-streaming
        logger.warning("Streaming failed (%s), falling back to non-streaming", e)

        try:
            content = await complete(
                prompt=prompt,
                system_prompt=system_prompt,
                model=model,
                api_key=api_key,
                base_url=base_url,
                messages=messages,
                **kwargs,
            )
            if content:
                yield content
        except Exception as e2:
            raise LLMAPIError(
                f"Local LLM failed: streaming={e}, non-streaming={e2}",
                provider="local",
            )
    finally:
        elapsed = time.perf_counter() - t0
        log_stream_call(
            provider="local",
            model=model or "default",
            usage=stream_usage,
            cost=0.0,
            elapsed=elapsed,
        )


async def fetch_models(
    base_url: str,
    api_key: str | None = None,
) -> list[str]:
    """
    Fetch available models from local LLM server.

    Supports:
    - Ollama (/api/tags)
    - OpenAI-compatible (/models)

    Args:
        base_url: Base URL for the local server
        api_key: API key (optional)

    Returns:
        List of available model names
    """
    base_url = base_url.rstrip("/")

    # Build headers using unified utility
    headers = build_auth_headers(api_key)
    # Remove Content-Type for GET request
    headers.pop("Content-Type", None)

    timeout = aiohttp.ClientTimeout(total=30)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        # Try Ollama /api/tags first
        is_ollama = ":11434" in base_url or "ollama" in base_url.lower()
        if is_ollama:
            try:
                ollama_url = base_url.replace("/v1", "") + "/api/tags"
                async with session.get(ollama_url, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if "models" in data:
                            return collect_model_names(data["models"])
            except Exception as exc:
                logger.debug(
                    "Failed to fetch Ollama models from %s: %s",
                    base_url,
                    exc,
                )

        # Try OpenAI-compatible /models
        try:
            models_url = f"{base_url}/models"
            async with session.get(models_url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()

                    # Handle different response formats
                    if "data" in data and isinstance(data["data"], list):
                        return collect_model_names(data["data"])
                    elif "models" in data and isinstance(data["models"], list):
                        return collect_model_names(data["models"])
                    elif isinstance(data, list):
                        return collect_model_names(data)
        except Exception as e:
            logger.error("Error fetching models from %s: %s", base_url, e)

        return []


__all__ = [
    "complete",
    "stream",
    "fetch_models",
]
