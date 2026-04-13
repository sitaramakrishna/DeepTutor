"""
LLM Telemetry
=============

Telemetry tracking for LLM calls.

Every provider complete() call is timed and logged at INFO level with:
  model, provider, mode, tokens_in, tokens_out, total_tokens, cost, latency, finish_reason

Streaming calls log at stream-end via log_stream_call() which providers invoke
from their _stream() generators.

Log format (single line, grep-friendly):
  LLM | provider=openai model=gpt-4o mode=complete tokens_in=512 tokens_out=128 total=640 cost=$0.001600 latency=1.243s finish=stop
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import functools
import time
from typing import TypeVar

from deeptutor.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Decorator applied to provider complete() methods
# ---------------------------------------------------------------------------

def track_llm_call(
    provider_name: str,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """
    Decorator that logs LLM call metrics after every complete() invocation.

    Captures: model, provider, tokens in/out/total, cost, latency, finish reason.
    On error: logs provider, function name, elapsed, exception type and message.
    """

    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(*args: object, **kwargs: object) -> T:
            t0 = time.perf_counter()
            try:
                result = await func(*args, **kwargs)
                elapsed = time.perf_counter() - t0

                # Import locally to avoid circular imports at module load time.
                from ..types import TutorResponse  # noqa: PLC0415

                if isinstance(result, TutorResponse):
                    _log_complete_call(
                        provider=result.provider or provider_name,
                        model=result.model or "",
                        usage=result.usage,
                        cost=result.cost_estimate,
                        finish_reason=result.finish_reason,
                        elapsed=elapsed,
                        is_stream=False,
                    )
                else:
                    # Streaming methods return a generator — log at basic level.
                    logger.debug(
                        "LLM | provider=%s fn=%s elapsed=%.3fs (generator)",
                        provider_name,
                        func.__name__,
                        elapsed,
                    )
                return result  # type: ignore[return-value]

            except Exception as exc:
                elapsed = time.perf_counter() - t0
                logger.warning(
                    "LLM ERROR | provider=%s fn=%s elapsed=%.3fs error=%s: %s",
                    provider_name,
                    func.__name__,
                    elapsed,
                    type(exc).__name__,
                    exc,
                )
                raise

        return wrapper  # type: ignore[return-value]

    return decorator  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Called by provider _stream() generators when streaming is complete
# ---------------------------------------------------------------------------

def log_stream_call(
    *,
    provider: str,
    model: str,
    usage: dict[str, int],
    cost: float,
    elapsed: float,
) -> None:
    """
    Log metrics for a completed streaming call.

    Providers call this from their _stream() generator once the final chunk
    is yielded and the stream is fully consumed.

    Args:
        provider:  Provider label (e.g. "openai", "anthropic").
        model:     Model name used for the call.
        usage:     Token usage dict. Keys vary by provider:
                     OpenAI:    prompt_tokens, completion_tokens, total_tokens
                     Anthropic: input_tokens, output_tokens
                   Pass {} when tokens are unavailable (e.g. OpenAI default stream).
        cost:      Cost estimate in USD (0.0 when not calculated).
        elapsed:   Wall-clock seconds from stream start to last chunk.
    """
    _log_complete_call(
        provider=provider,
        model=model,
        usage=usage,
        cost=cost,
        finish_reason="stop",
        elapsed=elapsed,
        is_stream=True,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _log_complete_call(
    *,
    provider: str,
    model: str,
    usage: dict[str, int],
    cost: float,
    finish_reason: str | None,
    elapsed: float,
    is_stream: bool,
) -> None:
    """Emit a single structured INFO log line with all LLM call metrics."""
    # Normalize token field names — OpenAI and Anthropic use different keys.
    tokens_in = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
    tokens_out = usage.get("completion_tokens") or usage.get("output_tokens") or 0
    tokens_total = usage.get("total_tokens") or (tokens_in + tokens_out)

    # Calculate cost from token counts when the provider returns 0.
    if cost == 0.0 and (tokens_in or tokens_out):
        cost = _estimate_cost(model, tokens_in, tokens_out)

    mode = "stream" if is_stream else "complete"
    logger.info(
        "LLM | provider=%s model=%s mode=%s "
        "tokens_in=%d tokens_out=%d total=%d "
        "cost=$%.6f latency=%.3fs finish=%s",
        provider,
        model,
        mode,
        tokens_in,
        tokens_out,
        tokens_total,
        cost,
        elapsed,
        finish_reason or "—",
    )

    # Push into session-level accumulator (never raises).
    _record_to_stats(
        model=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost=cost,
        elapsed=elapsed,
    )


def _estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    """Estimate cost using the pricing table in llm_stats."""
    try:
        from deeptutor.logging.stats.llm_stats import get_pricing  # noqa: PLC0415
        pricing = get_pricing(model)
        return (tokens_in / 1000.0) * pricing["input"] + (tokens_out / 1000.0) * pricing["output"]
    except Exception:
        return 0.0


def _record_to_stats(
    *,
    model: str,
    tokens_in: int,
    tokens_out: int,
    cost: float,
    elapsed: float,
) -> None:
    """Feed call data into the global session LLMStats tracker."""
    try:
        from deeptutor.logging.stats.llm_stats import get_global_stats  # noqa: PLC0415
        get_global_stats().add_call(
            model=model,
            prompt_tokens=tokens_in,
            completion_tokens=tokens_out,
            latency_seconds=elapsed,
        )
    except Exception:
        pass  # Stats tracking must never break a call path.
