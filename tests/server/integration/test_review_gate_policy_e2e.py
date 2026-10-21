"""Server-path check that cross-family review policies see what they need.

Drives ``POST /v1/sessions/{id}/policies/evaluate`` with the exact request
shapes the runner sends for a ``sys_session_send`` dispatch and for delayed
child output, against real stores. Proves a policy can (1) DENY a same-family
review dispatch and (2) attribute a reviewer's output to a dispatch by title.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.runtime import get_caps
from omnigent.runtime.caps import RuntimeCaps
from omnigent.spec.types import FunctionPolicySpec, FunctionRef
from omnigent_review.policies import ready_check
from tests.server.helpers import create_session_for_agent as _create_session
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio

SHA = "deadbeef"


def _dispatch_request(agent: str, title: str, purpose: str, model: str | None) -> dict[str, Any]:
    child_args: dict[str, Any] = {"purpose": purpose, "input": "x"}
    if model:
        child_args["model"] = model
    return {
        "event": {
            "type": "PHASE_TOOL_CALL",
            "target": "",
            "data": {
                "name": "sys_session_send",
                "arguments": {"agent": agent, "title": title, "args": child_args},
            },
            "context": {},
        }
    }


def _child_output_request(title: str, output: str, conversation_id: str) -> dict[str, Any]:
    """Mirror of ``_subagent_tool_result_policy_request`` in the runner."""
    return {
        "event": {
            "type": "PHASE_TOOL_RESULT",
            "data": {"result": output},
            "request_data": {
                "name": "sys_session_send",
                "tool": "sys_session_send",
                "args": {"agent": "codex", "title": title, "conversation_id": conversation_id},
            },
        }
    }


async def test_review_policies_enforce_via_server_engine(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Same-family review is denied; a delayed child verdict is recorded and gates readiness."""
    ledger_path = str(tmp_path / "ledger.json")
    specs = [
        FunctionPolicySpec(
            name=f"review__{handler}",
            on=None,
            function=FunctionRef(
                path=f"omnigent_review.policies.{handler}",
                arguments={"ledger_path": ledger_path},
            ),
        )
        for handler in ("family_guard", "verdict_recorder")
    ]
    original = get_caps()
    patched = RuntimeCaps(execution_timeout=original.execution_timeout, default_policies=specs)
    monkeypatch.setattr("omnigent.server.routes.sessions.get_caps", lambda: patched)

    agent = await create_test_agent(client)
    session_id = await _create_session(client, agent["id"])
    url = f"/v1/sessions/{session_id}/policies/evaluate"

    async def evaluate(body: dict[str, Any]) -> dict[str, Any]:
        resp = await client.post(url, json=body)
        assert resp.status_code == 200, resp.text
        return resp.json()

    impl = await evaluate(_dispatch_request("claude_code", "auth", "implement", None))
    assert impl["result"] == "POLICY_ACTION_ALLOW"

    same = await evaluate(
        _dispatch_request("cursor", "review-auth@c", "review", "claude-opus-4-8")
    )
    assert same["result"] == "POLICY_ACTION_DENY", same

    cross = await evaluate(_dispatch_request("codex", "review-auth@codex", "review", None))
    assert cross["result"] == "POLICY_ACTION_ALLOW", cross

    verdict = json.dumps({"verdict": "approve", "head_sha": SHA, "findings": []})
    await evaluate(
        _child_output_request("review-auth@codex", f"ok\n```json\n{verdict}\n```", "c1")
    )

    ready, reasons = ready_check("auth", SHA, ledger_path=ledger_path)
    assert ready, reasons
