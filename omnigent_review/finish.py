"""Readiness command: the only sanctioned way to mark a reviewed task ready."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from omnigent_review.policies import ready_check

_TASK_KEYS = ("id", "task_id", "title", "name")


def _find_task(registry: dict[str, Any], task: str) -> dict[str, Any] | None:
    tasks = registry.get("tasks")
    if isinstance(tasks, dict):
        entry = tasks.get(task)
        return entry if isinstance(entry, dict) else None
    if isinstance(tasks, list):
        for entry in tasks:
            if isinstance(entry, dict) and any(entry.get(key) == task for key in _TASK_KEYS):
                return entry
    return None


def finish(
    task: str,
    head_sha: str,
    *,
    ledger_path: str,
    registry_path: str | None = None,
    min_families: int = 1,
) -> tuple[int, list[str]]:
    """
    Check independent review and, when satisfied, mark the registry entry ready.

    :param task: Implementer dispatch title, e.g. ``"auth-refactor"``.
    :param head_sha: Commit the deliverable is at.
    :param ledger_path: Review ledger file path.
    :param registry_path: Optional orchestrator registry to update on success.
    :param min_families: Distinct approving reviewer families required.
    :returns: ``(exit_code, messages)``: 0 ready, 1 not ready, 3 task missing from
        the registry (nothing is written).
    """
    ready, reasons = ready_check(
        task, head_sha, ledger_path=ledger_path, min_families=min_families
    )
    if not ready:
        return 1, reasons
    if registry_path is None:
        return 0, [f"{task} is ready at {head_sha}"]
    path = Path(registry_path)
    registry = json.loads(path.read_text())
    entry = _find_task(registry, task)
    if entry is None:
        return 3, [f"{task!r} not found in {registry_path}; registry unchanged"]
    entry["state"] = "ready"
    entry["review_head_sha"] = head_sha
    path.write_text(json.dumps(registry, indent=2, sort_keys=True))
    return 0, [f"{task} marked ready at {head_sha}"]


def main(argv: list[str] | None = None) -> int:
    """
    CLI entry point: ``python -m omnigent_review finish --task T --head-sha S ...``.

    :param argv: Argument list; defaults to ``sys.argv[1:]``.
    :returns: Process exit code from :func:`finish`.
    """
    parser = argparse.ArgumentParser(prog="omnigent_review")
    sub = parser.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser("finish", help="mark a reviewed task ready")
    cmd.add_argument("--task", required=True)
    cmd.add_argument("--head-sha", required=True)
    cmd.add_argument("--ledger", default=".polly/review_ledger.json")
    cmd.add_argument("--registry", default=None)
    cmd.add_argument("--min-families", type=int, default=1)
    args = parser.parse_args(argv)
    code, messages = finish(
        args.task,
        args.head_sha,
        ledger_path=args.ledger,
        registry_path=args.registry,
        min_families=args.min_families,
    )
    print("\n".join(messages), file=sys.stderr if code else sys.stdout)
    return code
