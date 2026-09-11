"""Basic input guardrails: reject empty/oversized/blocklisted/malformed input
and redact obvious PII before it reaches the model. Everything here is
in-process — no external guardrails service.
"""

from __future__ import annotations

import re

from gateway.errors import GuardrailRejected
from gateway.models import Message

# Roles this basic gateway forwards, including the tool-calling roles: an
# assistant turn may carry tool_calls, and a "tool" turn carries a tool result
# (with its tool_call_id). An unknown role is rejected here (a clean
# INVALID_ARGUMENT) rather than passed through to fail deep in the provider.
_ALLOWED_ROLES = frozenset({"system", "user", "assistant", "developer", "tool"})

# Best-effort, deliberately broad PII redaction: it favours over-redacting
# obvious identifiers over letting them reach the model, so it will catch
# non-PII digit runs (e.g. order IDs shaped like a phone number) and misses
# unformatted variants (e.g. separator-less SSNs). Not a substitute for a real
# DLP pass.
_PII_PATTERNS = [
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[EMAIL]"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[SSN]"),
    (re.compile(r"\b\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b"), "[CARD]"),
    (re.compile(r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b"), "[PHONE]"),
]


def screen_and_sanitize(
    messages: list[Message],
    *,
    max_input_chars: int,
    blocklist: list[str],
) -> list[Message]:
    """Run input guardrails and return PII-redacted messages.

    Raises GuardrailRejected if the input is empty, too long, carries an
    unsupported role, or contains a blocklisted term. Blocklist matching is
    scoped to individual user and tool messages -- the two roles carrying
    caller/externally-supplied content -- so the model's own prior turns
    (assistant/system content) can't trip it and a term can't falsely match
    across a message boundary. Tool messages carry a tool's *result* (e.g. an
    HTTP response body or database row fetched by mcp_svc), which is exactly
    the kind of externally-sourced content a prompt-injection payload could
    ride in on, so it gets the same screening as direct user input.
    """
    for m in messages:
        if m.role not in _ALLOWED_ROLES:
            raise GuardrailRejected(f"unsupported role: {m.role!r}")

    combined = " ".join(m.content for m in messages).strip()
    if not combined:
        raise GuardrailRejected("input is empty")
    if len(combined) > max_input_chars:
        raise GuardrailRejected(
            f"input exceeds max_input_chars ({len(combined)} > {max_input_chars})"
        )

    for m in messages:
        if m.role not in ("user", "tool"):
            continue
        lowered = m.content.lower()
        for term in blocklist:
            if term.lower() in lowered:
                raise GuardrailRejected(f"blocked term detected: {term!r}")

    # Preserve tool-calling fields (tool_calls / tool_call_id) — only the
    # free-text content is redacted.
    return [
        Message(
            role=m.role,
            content=_redact_pii(m.content),
            tool_calls=m.tool_calls,
            tool_call_id=m.tool_call_id,
        )
        for m in messages
    ]


def _redact_pii(text: str) -> str:
    for pattern, replacement in _PII_PATTERNS:
        text = pattern.sub(replacement, text)
    return text
