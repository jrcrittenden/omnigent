"""Tests for the finish command's readiness decision and registry update."""

from __future__ import annotations

import json
from pathlib import Path

from omnigent_review import ledger
from omnigent_review.finish import finish, main

SHA = "abc123"


def _seed(path: Path, *, approving_families: list[str]) -> None:
    def mutate(data: dict) -> None:
        data["dispatches"]["auth"] = {
            "agent": "claude_code",
            "family": "anthropic",
            "purpose": "implement",
        }
        for family in approving_families:
            data["verdicts"].append(
                {
                    "task": "auth",
                    "title": f"review-auth@{family}",
                    "agent": family,
                    "family": family,
                    "conversation_id": "c",
                    "parsed": {
                        "verdict": "approve",
                        "head_sha": SHA,
                        "findings": [],
                        "valid": True,
                    },
                }
            )

    ledger.update(path, mutate)


def test_finish_refuses_without_enough_families_and_leaves_registry(tmp_path: Path) -> None:
    """One approving family cannot satisfy a 2-family gate, and nothing is written."""
    ledger_path = tmp_path / "l.json"
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"tasks": {"auth": {"state": "running"}}}))
    _seed(ledger_path, approving_families=["openai"])
    code, messages = finish(
        "auth", SHA, ledger_path=str(ledger_path), registry_path=str(registry), min_families=2
    )
    assert code == 1 and "need 2" in messages[-1]
    assert json.loads(registry.read_text())["tasks"]["auth"]["state"] == "running"


def test_finish_marks_dict_and_list_registries_ready(tmp_path: Path) -> None:
    """Both registry shapes (dict by task, list of entries) are updated on success."""
    ledger_path = tmp_path / "l.json"
    _seed(ledger_path, approving_families=["openai", "google"])
    for shape in (
        {"tasks": {"auth": {"state": "running"}}},
        {"tasks": [{"id": "auth", "state": "running"}]},
    ):
        registry = tmp_path / "registry.json"
        registry.write_text(json.dumps(shape))
        code, _ = finish(
            "auth", SHA, ledger_path=str(ledger_path), registry_path=str(registry), min_families=2
        )
        tasks = json.loads(registry.read_text())["tasks"]
        entry = tasks["auth"] if isinstance(tasks, dict) else tasks[0]
        assert code == 0 and entry["state"] == "ready" and entry["review_head_sha"] == SHA


def test_finish_reports_missing_registry_entry_without_writing(tmp_path: Path) -> None:
    """A ready task absent from the registry returns 3 and leaves the file unchanged."""
    ledger_path = tmp_path / "l.json"
    registry = tmp_path / "registry.json"
    original = json.dumps({"tasks": {"other": {}}})
    registry.write_text(original)
    _seed(ledger_path, approving_families=["openai"])
    code, _ = finish("auth", SHA, ledger_path=str(ledger_path), registry_path=str(registry))
    assert code == 3 and registry.read_text() == original


def test_cli_exit_codes(tmp_path: Path) -> None:
    """The CLI returns 1 for an unreviewed task and 0 once an independent family approved."""
    ledger_path = tmp_path / "l.json"
    argv = ["finish", "--task", "auth", "--head-sha", SHA, "--ledger", str(ledger_path)]
    assert main(argv) == 1
    _seed(ledger_path, approving_families=["openai"])
    assert main(argv) == 0


def test_finish_output_is_labelled_advisory(tmp_path: Path) -> None:
    """A passing finish says the result is advisory, and the registry records its authority."""
    ledger_path = tmp_path / "l.json"
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"tasks": {"auth": {"state": "running"}}}))
    _seed(ledger_path, approving_families=["openai"])
    code, messages = finish("auth", SHA, ledger_path=str(ledger_path), registry_path=str(registry))
    assert code == 0 and "ADVISORY" in messages[-1]
    assert json.loads(registry.read_text())["tasks"]["auth"]["review_authority"].startswith(
        "advisory"
    )
