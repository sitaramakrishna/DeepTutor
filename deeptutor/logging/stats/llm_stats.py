"""
LLM Stats Tracker
=================

Tracks LLM token usage, costs, and latency across all modules.
Outputs per-module and global summaries via the unified logging system.

Usage:
    from deeptutor.logging import LLMStats

    stats = LLMStats("Solver")

    # After each LLM call:
    stats.add_call(
        model="gpt-4o-mini",
        prompt_tokens=100,
        completion_tokens=50,
        latency_seconds=1.23,
    )

    # At the end:
    stats.log_summary()  # Uses logging system

Global session stats (updated automatically by telemetry.py):
    from deeptutor.logging.stats.llm_stats import get_global_stats
    get_global_stats().log_summary()
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from ..logger import Logger

# ---------------------------------------------------------------------------
# Model pricing per 1K tokens (USD)
# ---------------------------------------------------------------------------
MODEL_PRICING: dict[str, dict[str, float]] = {
    # OpenAI
    "gpt-4o": {"input": 0.0025, "output": 0.010},
    "gpt-4o-mini": {"input": 0.00015, "output": 0.0006},
    "gpt-4-turbo": {"input": 0.01, "output": 0.03},
    "gpt-4": {"input": 0.03, "output": 0.06},
    "gpt-3.5-turbo": {"input": 0.0005, "output": 0.0015},
    "o4-mini": {"input": 0.00110, "output": 0.00440},
    "o3": {"input": 0.01000, "output": 0.04000},
    "o3-mini": {"input": 0.00110, "output": 0.00440},
    # Anthropic
    "claude-3-5-sonnet": {"input": 0.003, "output": 0.015},
    "claude-3-5-haiku": {"input": 0.0008, "output": 0.004},
    "claude-3-opus": {"input": 0.015, "output": 0.075},
    "claude-3-haiku": {"input": 0.00025, "output": 0.00125},
    "claude-sonnet-4": {"input": 0.003, "output": 0.015},
    "claude-opus-4": {"input": 0.015, "output": 0.075},
    # DeepSeek
    "deepseek-chat": {"input": 0.00014, "output": 0.00028},
    "deepseek-reasoner": {"input": 0.00055, "output": 0.00219},
    # Gemini
    "gemini-1.5-pro": {"input": 0.00125, "output": 0.005},
    "gemini-1.5-flash": {"input": 0.000075, "output": 0.0003},
    "gemini-2.0-flash": {"input": 0.0001, "output": 0.0004},
    # Qwen / DashScope
    "qwen-max": {"input": 0.0024, "output": 0.0096},
    "qwen-plus": {"input": 0.0004, "output": 0.0012},
    "qwen-turbo": {"input": 0.00005, "output": 0.0002},
}


def get_pricing(model: str) -> dict[str, float]:
    """Get pricing for a model (fuzzy match on model name substring)."""
    model_lower = model.lower()
    for key, pricing in MODEL_PRICING.items():
        if key in model_lower or model_lower.startswith(key):
            return pricing
    # Fallback to gpt-4o-mini rates
    return {"input": 0.00015, "output": 0.0006}


def estimate_tokens(text: str) -> int:
    """Rough estimate of tokens (1.3 tokens per word)."""
    return int(len(text.split()) * 1.3)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class LLMCall:
    """Single LLM call record."""

    model: str
    prompt_tokens: int
    completion_tokens: int
    cost: float
    latency_seconds: float = 0.0
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


# ---------------------------------------------------------------------------
# Per-module tracker
# ---------------------------------------------------------------------------

class LLMStats:
    """
    LLM usage statistics tracker.

    Tracks token usage, costs, and latency per module.
    Call log_summary() at the end of a pipeline run to emit totals.
    """

    def __init__(self, module_name: str = "Module") -> None:
        self.module_name = module_name
        self.calls: list[LLMCall] = []
        self.total_prompt_tokens: int = 0
        self.total_completion_tokens: int = 0
        self.total_cost: float = 0.0
        self.total_latency_seconds: float = 0.0
        self.model_used: Optional[str] = None

    def add_call(
        self,
        model: str,
        prompt_tokens: Optional[int] = None,
        completion_tokens: Optional[int] = None,
        latency_seconds: float = 0.0,
        # Optional: estimate from raw text when token counts are unavailable.
        system_prompt: Optional[str] = None,
        user_prompt: Optional[str] = None,
        response: Optional[str] = None,
    ) -> None:
        """
        Record an LLM call.

        Args:
            model:             Model name (e.g. "gpt-4o-mini").
            prompt_tokens:     Actual input token count from API response.
            completion_tokens: Actual output token count from API response.
            latency_seconds:   Wall-clock time for the call in seconds.
            system_prompt:     Used to estimate tokens when counts are absent.
            user_prompt:       Used to estimate tokens when counts are absent.
            response:          Used to estimate completion tokens when absent.
        """
        # Fall back to estimation when actual counts are not provided.
        if prompt_tokens is None and (system_prompt or user_prompt):
            prompt_text = (system_prompt or "") + "\n" + (user_prompt or "")
            prompt_tokens = estimate_tokens(prompt_text)

        if completion_tokens is None and response:
            completion_tokens = estimate_tokens(response)

        prompt_tokens = prompt_tokens or 0
        completion_tokens = completion_tokens or 0

        pricing = get_pricing(model)
        cost = (
            (prompt_tokens / 1000.0) * pricing["input"]
            + (completion_tokens / 1000.0) * pricing["output"]
        )

        call = LLMCall(
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost=cost,
            latency_seconds=latency_seconds,
        )
        self.calls.append(call)

        self.total_prompt_tokens += prompt_tokens
        self.total_completion_tokens += completion_tokens
        self.total_cost += cost
        self.total_latency_seconds += latency_seconds

        if self.model_used is None:
            self.model_used = model

    def get_summary(self) -> dict[str, Any]:
        """Return summary as a plain dictionary."""
        n = len(self.calls)
        avg_latency = (self.total_latency_seconds / n) if n > 0 else 0.0
        return {
            "module": self.module_name,
            "model": self.model_used or "unknown",
            "calls": n,
            "prompt_tokens": self.total_prompt_tokens,
            "completion_tokens": self.total_completion_tokens,
            "total_tokens": self.total_prompt_tokens + self.total_completion_tokens,
            "cost_usd": self.total_cost,
            "total_latency_seconds": round(self.total_latency_seconds, 3),
            "avg_latency_seconds": round(avg_latency, 3),
        }

    def log_summary(self, logger: Optional["Logger"] = None) -> None:
        """
        Emit usage summary through the unified logging system.

        Args:
            logger: Optional Logger instance; creates one from module_name if absent.
        """
        if not self.calls:
            return

        from ..logger import get_logger  # noqa: PLC0415

        if logger is None:
            logger = get_logger(self.module_name)

        n = len(self.calls)
        total_tokens = self.total_prompt_tokens + self.total_completion_tokens
        avg_latency = (self.total_latency_seconds / n) if n > 0 else 0.0

        logger.info("=" * 60)
        logger.info(f"LLM Usage Summary — {self.module_name}")
        logger.info("=" * 60)
        logger.info(f"Model        : {self.model_used or 'unknown'}")
        logger.info(f"API Calls    : {n}")
        logger.info(
            f"Tokens       : {total_tokens:,}  "
            f"(in={self.total_prompt_tokens:,}  out={self.total_completion_tokens:,})"
        )
        logger.info(f"Cost         : ${self.total_cost:.6f} USD")
        logger.info(
            f"Latency      : {self.total_latency_seconds:.3f}s total  "
            f"/ {avg_latency:.3f}s avg per call"
        )
        logger.info("=" * 60)

    # Deprecated alias kept for back-compat.
    def print_summary(self) -> None:
        self.log_summary()

    def reset(self) -> None:
        """Reset all statistics."""
        self.calls.clear()
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.total_cost = 0.0
        self.total_latency_seconds = 0.0
        self.model_used = None


# ---------------------------------------------------------------------------
# Global session-level accumulator (updated by telemetry.py)
# ---------------------------------------------------------------------------

_global_stats: Optional[LLMStats] = None


def get_global_stats() -> LLMStats:
    """Return the process-wide LLMStats instance, creating it on first call."""
    global _global_stats
    if _global_stats is None:
        _global_stats = LLMStats("session")
    return _global_stats


def reset_global_stats() -> None:
    """Reset global stats (e.g. between test runs or sessions)."""
    global _global_stats
    _global_stats = None
