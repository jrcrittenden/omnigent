"""Unit tests for cross-family review enforcement.

Policies run through the real ``FunctionPolicy`` adapter, so the event shape
the callable sees is the one the engine builds, not a hand-written dict.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnigent.policies.function import resolve_function_policy
from omnigent.policies.types import EvaluationContext, PolicyAction
from omnigent.spec.types import FunctionPolicySpec, FunctionRef, Phase
from omnigent_review.families import family_of_model, resolve_family
from omnigent_review.policies import parse_verdict, ready_check

SHA = "abc123"


def _policy(handler: str, ledger_path: Path, **extra: Any) -> Any:
    spec = FunctionPolicySpec(
        name=handler,
        on=None,
        function=FunctionRef(
            path=f"omnigent_review.policies.{handler}",
            arguments={"ledger_path": str(ledger_path), **extra},
        ),
    )
    return resolve_function_policy(spec)


async def _dispatch(
    guard: Any, *, agent: str, title: str, purpose: str, model: str | None = None
) -> PolicyAction:
    child_args: dict[str, Any] = {"purpose": purpose, "input": "do it"}
    if model:
        child_args["model"] = model
    ctx = EvaluationContext(
        phase=Phase.TOOL_CALL,
        content={
            "name": "sys_session_send",
            "arguments": {"agent": agent, "title": title, "args": child_args},
        },
        tool_name="sys_session_send",
    )
    return (await guard.evaluate(ctx, {})).action


def _verdict_block(verdict: str, findings: list[dict[str, Any]] | None = None) -> str:
    body = {"verdict": verdict, "head_sha": SHA, "findings": findings or []}
    return f"Review done.\n```json\n{json.dumps(body)}\n```"


async def _child_result(recorder: Any, *, title: str, output: str, conv: str) -> None:
    ctx = EvaluationContext(
        phase=Phase.TOOL_RESULT,
        content={"result": output},
        tool_name="sys_session_send",
        request_data={
            "name": "sys_session_send",
            "tool": "sys_session_send",
            "args": {"agent": "x", "title": title, "conversation_id": conv},
        },
    )
    await recorder.evaluate(ctx, {})


@pytest.mark.parametrize(
    ("agent", "model", "expected"),
    [
        ("claude_code", None, "anthropic"),
        ("codex", None, "openai"),
        ("agy", None, "google"),
        ("opencode", None, None),  # multi-model harness, no model: unproven
        ("hermes", "deepseek-v4", "deepseek"),
        ("cursor", "gpt-5", "openai"),
        ("opencode", "mystery-model", None),  # unrecognized: unproven
        ("codex", "claude-opus-4-8", None),  # contradicts single-family harness
        ("pi", "openai/gpt-5.1", "openai"),
    ],
)
def test_resolve_family_fails_closed(agent: str, model: str | None, expected: str | None) -> None:
    """A family is returned only when provable; contradictions and unknowns give None."""
    assert resolve_family(agent, model) == expected


def test_family_of_model_strips_provider_prefix() -> None:
    """Provider-qualified ids resolve by their model name."""
    assert family_of_model("anthropic/claude-sonnet-5-5") == "anthropic"


async def test_same_family_review_is_denied_and_cross_family_allowed(tmp_path: Path) -> None:
    """A claude_code diff cannot be reviewed by cursor-on-Claude, but codex may review it."""
    guard = _policy("family_guard", tmp_path / "l.json")
    assert await _dispatch(guard, agent="claude_code", title="auth", purpose="implement") == (
        PolicyAction.ALLOW
    )
    same = await _dispatch(
        guard, agent="cursor", title="review-auth", purpose="review", model="claude-opus-4-8"
    )
    assert same == PolicyAction.DENY
    assert await _dispatch(guard, agent="codex", title="review-auth", purpose="review") == (
        PolicyAction.ALLOW
    )


async def test_unproven_reviewer_family_is_denied(tmp_path: Path) -> None:
    """opencode with no explicit model cannot prove independence, so it is refused."""
    guard = _policy("family_guard", tmp_path / "l.json")
    await _dispatch(guard, agent="codex", title="t", purpose="implement")
    assert await _dispatch(guard, agent="opencode", title="review-t", purpose="review") == (
        PolicyAction.DENY
    )


async def test_review_without_recorded_implement_is_denied(tmp_path: Path) -> None:
    """A reviewer cannot be dispatched for a task the ledger never saw implemented."""
    guard = _policy("family_guard", tmp_path / "l.json")
    assert await _dispatch(guard, agent="codex", title="review-ghost", purpose="review") == (
        PolicyAction.DENY
    )


async def test_two_independent_families_make_task_ready(tmp_path: Path) -> None:
    """Approvals from two distinct non-producer families satisfy a 2-family gate; one does not."""
    ledger_path = tmp_path / "l.json"
    guard = _policy("family_guard", ledger_path)
    recorder = _policy("verdict_recorder", ledger_path)
    await _dispatch(guard, agent="claude_code", title="auth", purpose="implement")
    assert await _dispatch(guard, agent="codex", title="review-auth@codex", purpose="review") == (
        PolicyAction.ALLOW
    )
    assert await _dispatch(guard, agent="agy", title="review-auth@agy", purpose="review") == (
        PolicyAction.ALLOW
    )
    ready, _ = ready_check("auth", SHA, ledger_path=str(ledger_path), min_families=1)
    assert not ready  # no verdicts yet

    await _child_result(
        recorder, title="review-auth@codex", output=_verdict_block("approve"), conv="c1"
    )
    assert ready_check("auth", SHA, ledger_path=str(ledger_path), min_families=1)[0]
    ready, reasons = ready_check("auth", SHA, ledger_path=str(ledger_path), min_families=2)
    assert not ready and "need 2" in reasons[-1]

    await _child_result(
        recorder, title="review-auth@agy", output=_verdict_block("approve"), conv="c2"
    )
    assert ready_check("auth", SHA, ledger_path=str(ledger_path), min_families=2)[0]


async def test_blocking_verdict_without_repro_is_invalid(tmp_path: Path) -> None:
    """A block with no file/line/evidence is recorded as invalid and never counts."""
    ledger_path = tmp_path / "l.json"
    guard = _policy("family_guard", ledger_path)
    recorder = _policy("verdict_recorder", ledger_path)
    await _dispatch(guard, agent="claude_code", title="auth", purpose="implement")
    await _dispatch(guard, agent="codex", title="review-auth", purpose="review")
    vague = _verdict_block("block", [{"severity": "blocking", "file": "a.py"}])
    await _child_result(recorder, title="review-auth", output=vague, conv="c1")
    assert parse_verdict(vague)["valid"] is False  # type: ignore[index]
    ready, _ = ready_check("auth", SHA, ledger_path=str(ledger_path))
    assert not ready


async def test_valid_block_vetoes_even_with_other_approval(tmp_path: Path) -> None:
    """One family's evidenced block fails the gate despite another family approving."""
    ledger_path = tmp_path / "l.json"
    guard = _policy("family_guard", ledger_path)
    recorder = _policy("verdict_recorder", ledger_path)
    await _dispatch(guard, agent="claude_code", title="auth", purpose="implement")
    await _dispatch(guard, agent="codex", title="review-auth@codex", purpose="review")
    await _dispatch(guard, agent="agy", title="review-auth@agy", purpose="review")
    await _child_result(
        recorder, title="review-auth@codex", output=_verdict_block("approve"), conv="c1"
    )
    finding = {"severity": "blocking", "file": "a.py", "line": 7, "repro": "pytest -k x fails"}
    await _child_result(
        recorder, title="review-auth@agy", output=_verdict_block("block", [finding]), conv="c2"
    )
    ready, reasons = ready_check("auth", SHA, ledger_path=str(ledger_path))
    assert not ready and any("blocks" in r for r in reasons)


async def test_verdict_for_stale_head_does_not_count(tmp_path: Path) -> None:
    """An approval at an older commit does not satisfy the gate at a newer head."""
    ledger_path = tmp_path / "l.json"
    guard = _policy("family_guard", ledger_path)
    recorder = _policy("verdict_recorder", ledger_path)
    await _dispatch(guard, agent="claude_code", title="auth", purpose="implement")
    await _dispatch(guard, agent="codex", title="review-auth", purpose="review")
    await _child_result(recorder, title="review-auth", output=_verdict_block("approve"), conv="c1")
    ready, _ = ready_check("auth", "newer-sha", ledger_path=str(ledger_path))
    assert not ready


async def test_recorder_ignores_non_review_children(tmp_path: Path) -> None:
    """Output from an implement child never lands in the verdict ledger."""
    ledger_path = tmp_path / "l.json"
    guard = _policy("family_guard", ledger_path)
    recorder = _policy("verdict_recorder", ledger_path)
    await _dispatch(guard, agent="claude_code", title="auth", purpose="implement")
    await _child_result(recorder, title="auth", output=_verdict_block("approve"), conv="c0")
    assert json.loads(ledger_path.read_text())["verdicts"] == []


def test_inconsistent_verdicts_are_invalid() -> None:
    """Approve-with-blocking-finding and block-without-blocking-finding are both rejected."""
    blocking = {"severity": "blocking", "file": "a.py", "line": 3, "repro": "x"}
    assert parse_verdict(_verdict_block("approve", [blocking]))["valid"] is False  # type: ignore[index]
    assert parse_verdict(_verdict_block("block", []))["valid"] is False  # type: ignore[index]
    assert parse_verdict(_verdict_block("approve"))["valid"] is True  # type: ignore[index]
    assert parse_verdict("no json here") is None
