"""Policies that make cross-family review a mechanism instead of a prompt.

``family_guard`` (tool_call) records implement dispatches and denies a review
dispatch whose family is unproven or equals the producer's. ``verdict_recorder``
(tool_result) records reviewer verdicts from the delayed child output the server
delivers, so a verdict can only come from a real child session.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from omnigent_review import ledger
from omnigent_review.families import MODEL_PREFIX_FAMILIES, resolve_family

_Json = dict[str, Any]
_ALLOW: _Json = {"result": "ALLOW"}
_VERDICT_BLOCK = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def parse_verdict(output: str) -> _Json | None:
    """
    Extract and validate the last fenced JSON verdict in reviewer output.

    A verdict is valid when it names ``approve`` or ``block``, carries the
    ``head_sha`` it reviewed, and every blocking finding has a file, an integer
    line and a repro or evidence string. An approval carrying a blocking
    finding is inconsistent and invalid.

    :param output: Reviewer's final output text.
    :returns: ``{"verdict", "head_sha", "findings", "valid"}``, or ``None`` when
        no parseable JSON block exists.
    """
    blocks = _VERDICT_BLOCK.findall(output)
    if not blocks:
        return None
    try:
        raw = json.loads(blocks[-1])
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    findings = raw.get("findings")
    findings = findings if isinstance(findings, list) else []
    blocking = [f for f in findings if isinstance(f, dict) and f.get("severity") == "blocking"]
    findings_ok = all(
        isinstance(f.get("file"), str)
        and isinstance(f.get("line"), int)
        and (f.get("repro") or f.get("evidence"))
        for f in blocking
    )
    verdict = raw.get("verdict")
    head_sha = raw.get("head_sha")
    valid = (
        verdict in ("approve", "block")
        and isinstance(head_sha, str)
        and bool(head_sha)
        and findings_ok
        and not (verdict == "approve" and blocking)
        and not (verdict == "block" and not blocking)
    )
    return {"verdict": verdict, "head_sha": head_sha, "findings": findings, "valid": valid}


def _deny(reason: str) -> _Json:
    return {"result": "DENY", "reason": reason}


def family_guard(
    *,
    ledger_path: str,
    review_prefix: str = "review-",
    model_prefixes: Mapping[str, str] = MODEL_PREFIX_FAMILIES,
) -> Callable[[_Json], _Json]:
    """
    Factory: enforce reviewer-family independence at dispatch time.

    :param ledger_path: Shared ledger file path, e.g. ``".polly/review_ledger.json"``.
    :param review_prefix: Reviewer titles are ``<review_prefix><implementer title>``,
        with an optional ``@<suffix>`` so several reviewers of one task stay distinct.
    :param model_prefixes: Lowercase model-name prefix to family.
    :returns: Evaluator ``fn(event)`` returning ALLOW or DENY.
    """
    path = Path(ledger_path)

    def _evaluate(event: _Json) -> _Json:
        data = event.get("data")
        if event.get("type") != "tool_call" or not isinstance(data, dict):
            return _ALLOW
        if data.get("name") != "sys_session_send":
            return _ALLOW
        arguments = data.get("arguments") or {}
        child_args = arguments.get("args") or {}
        purpose = child_args.get("purpose")
        title = arguments.get("title") or ""
        agent = arguments.get("agent") or ""
        family = resolve_family(agent, child_args.get("model"), model_prefixes)

        if purpose == "implement":
            ledger.update(
                path,
                lambda led: led["dispatches"].__setitem__(
                    title, {"agent": agent, "family": family, "purpose": "implement"}
                ),
            )
            return _ALLOW
        if purpose != "review":
            return _ALLOW

        if not title.startswith(review_prefix):
            return _deny(f"Review dispatch titles must start with {review_prefix!r}.")
        task = title[len(review_prefix) :].split("@", 1)[0]
        producer = ledger.load(path)["dispatches"].get(task)
        if producer is None or producer.get("purpose") != "implement":
            return _deny(f"No recorded implement dispatch named {task!r} to review.")
        if producer["family"] is None:
            return _deny(
                f"Producer family of {task!r} is unproven; re-dispatch with an explicit model."
            )
        if family is None:
            return _deny(
                f"Reviewer family for agent {agent!r} is unproven; set args.model to a "
                "recognized model so independence can be checked."
            )
        if family == producer["family"]:
            return _deny(
                f"Reviewer family {family!r} equals producer family for {task!r}; "
                "choose a reviewer from a different model family."
            )
        ledger.update(
            path,
            lambda led: led["dispatches"].__setitem__(
                title,
                {"agent": agent, "family": family, "purpose": "review", "task": task},
            ),
        )
        return _ALLOW

    return _evaluate


def verdict_recorder(*, ledger_path: str) -> Callable[[_Json], _Json]:
    """
    Factory: record reviewer verdicts from delayed child output.

    :param ledger_path: Shared ledger file path.
    :returns: Evaluator ``fn(event)``; always ALLOW, recording as a side effect.
    """
    path = Path(ledger_path)

    def _evaluate(event: _Json) -> _Json:
        if event.get("type") != "tool_result":
            return _ALLOW
        request = event.get("request_data") or {}
        if request.get("name") != "sys_session_send":
            return _ALLOW
        # Child-output path sends ``args``; the native hook path sends ``arguments``.
        call_args = request.get("args") or request.get("arguments") or {}
        title = call_args.get("title") or ""
        data = event.get("data")
        output = data.get("result") if isinstance(data, dict) else data
        if not isinstance(output, str):
            return _ALLOW
        dispatch = ledger.load(path)["dispatches"].get(title)
        if dispatch is None or dispatch.get("purpose") != "review":
            return _ALLOW
        parsed = parse_verdict(output)
        record = {
            "task": dispatch["task"],
            "title": title,
            "agent": dispatch["agent"],
            "family": dispatch["family"],
            "conversation_id": call_args.get("conversation_id"),
            "parsed": parsed,
        }
        ledger.update(path, lambda led: led["verdicts"].append(record))
        return _ALLOW

    return _evaluate


def ready_check(
    task: str,
    head_sha: str,
    *,
    ledger_path: str,
    min_families: int = 1,
) -> tuple[bool, list[str]]:
    """
    Decide whether *task* has enough independent approvals at *head_sha*.

    The latest valid verdict per reviewer family at *head_sha* counts; any
    family whose latest verdict is ``block`` fails the check.

    :param task: Implementer dispatch title.
    :param head_sha: Commit the deliverable is at.
    :param ledger_path: Shared ledger file path.
    :param min_families: Distinct approving reviewer families required.
    :returns: ``(ready, reasons)``; *reasons* explains every failure.
    """
    led = ledger.load(Path(ledger_path))
    producer = led["dispatches"].get(task)
    if producer is None or producer["family"] is None:
        return False, [f"no proven producer family recorded for {task!r}"]
    latest: dict[str, _Json] = {}
    for record in led["verdicts"]:
        parsed = record["parsed"]
        if record["task"] != task or not parsed or not parsed["valid"]:
            continue
        if parsed["head_sha"] != head_sha or record["family"] in (None, producer["family"]):
            continue
        latest[record["family"]] = parsed
    reasons = [f"family {fam!r} blocks" for fam, p in latest.items() if p["verdict"] == "block"]
    approving = [fam for fam, p in latest.items() if p["verdict"] == "approve"]
    if len(approving) < min_families:
        reasons.append(
            f"{len(approving)} independent approving families at {head_sha}; need {min_families}"
        )
    return not reasons, reasons
