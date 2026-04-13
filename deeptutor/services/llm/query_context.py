"""
LLM Query Context
=================

A lightweight ContextVar that carries agent/capability/stage metadata
from BaseAgent (or capability code) down into the executor layer,
without adding parameters to every function on the call stack.

Usage (in BaseAgent):

    from deeptutor.services.llm.query_context import set_query_context

    token = set_query_context(agent="concept_analysis_agent", stage="concept_analysis")
    try:
        response = await llm_complete(...)
    finally:
        reset_query_context(token)

Usage (in executors.py):

    from deeptutor.services.llm.query_context import get_query_context

    ctx = get_query_context()
    # ctx.agent, ctx.stage, ctx.capability, ctx.call_id
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
import uuid


@dataclass(frozen=True)
class QueryContext:
    """Metadata about who is making an LLM call."""

    agent: str = ""
    stage: str = ""
    capability: str = ""
    call_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])


_QUERY_CTX: ContextVar[QueryContext] = ContextVar(
    "deeptutor_query_ctx",
    default=QueryContext(),
)


def set_query_context(
    *,
    agent: str = "",
    stage: str = "",
    capability: str = "",
) -> object:
    """
    Set the current query context and return a reset token.

    The token must be passed to reset_query_context() to restore
    the previous context (important in nested calls).

    Returns:
        A token that can be passed to reset_query_context().
    """
    ctx = QueryContext(agent=agent, stage=stage, capability=capability)
    return _QUERY_CTX.set(ctx)


def reset_query_context(token: object) -> None:
    """Restore the previous query context using the token from set_query_context()."""
    _QUERY_CTX.reset(token)  # type: ignore[arg-type]


def get_query_context() -> QueryContext:
    """Return the current query context (empty defaults when not set)."""
    return _QUERY_CTX.get()
