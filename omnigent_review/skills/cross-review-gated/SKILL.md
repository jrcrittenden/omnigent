---
name: cross-review-gated
description: Verify a PR's diff with reviewers from a different MODEL FAMILY than the implementer, enforced by policy; loop blocking findings to the implementer and mark ready only through the finish command.
---

# cross-review-gated

Independence is enforced by policy, not trust: a review dispatch whose family is
the implementer's (or unproven) is denied, verdicts are recorded from the
reviewer's own output, and readiness is set only by `finish`.

## Tiers
- **Small** (one module, no migration, API, or security path): 1 reviewer family.
- **Large** (multi-module, migration, API shape, security-sensitive, or a
  second failed fix loop): 2 distinct reviewer families.

## Procedure
1. Deterministic gates first (tests, lint, typecheck) via `sys_os_shell`. If red,
   send the implementer back; do not involve a reviewer.
2. Save a diff snapshot outside the implementer's worktree and record its base
   and head SHAs. Never regenerate the diff through model output.
3. Dispatch each reviewer with `purpose: "review"` and title
   `review-<implementer title>@<agent>`, e.g. `review-auth-refactor@codex`.
   - `claude_code`, `codex`, `agy` need no model. `opencode`, `hermes`, `cursor`,
     `pi` MUST set `args.model` to a recognized model, or the dispatch is denied.
   - The reviewer family must differ from the implementer's. If the policy denies
     a dispatch, pick another family; do not reword to get past it.
   - Input is ONLY: the snapshot path, base/head SHAs, the acceptance contract,
     and the reviewer prompt below. Never the implementer's transcript or worktree.
4. Collect results with `sys_read_inbox`. Treat every finding as a lead: reopen
   the cited file:line and confirm it before acting.
5. For each valid blocking finding, send a fix task to the SAME implementer
   conversation, then repeat from step 1 at the new head. Approvals at an older
   head do not count.
6. Disagreement between reviewers is settled by execution: write and run a test
   for the disputed claim. Do not average votes. A rebuttal of a finding needs a
   written reason citing evidence.
7. When gates are green, run:
   `python -m omnigent_review finish --task <implementer title> --head-sha <sha> --ledger .polly/review_ledger.json --registry .polly/registry.json --min-families <1|2>`
   Exit 0 marks the registry entry ready. Exit 1 prints exactly what is missing.
   Writing "ready" into the registry any other way is denied.
8. The PR goes to the human to merge. If fewer independent families are
   available than the tier needs, say so in the report; `finish` will refuse.

## Reviewer prompt (append to every review dispatch input)
```
Judge ONLY the diff against the acceptance contract. Do not edit anything.
Do not trust any summary; read the code. Every blocking finding needs file,
line, and a repro command or concrete evidence. If the snapshot is
unreadable, say so instead of reviewing.
End your final message with exactly one fenced json block:
{"verdict": "approve" | "block", "head_sha": "<head sha you reviewed>",
 "findings": [{"severity": "blocking" | "non-blocking", "file": "...",
 "line": 0, "repro": "...", "evidence": "..."}]}
"approve" must have no blocking findings; "block" must have at least one.
```

## Handoff packets for implementers
Include repo path, exact objective, in-scope and out-of-scope files, the
verification commands and what success looks like, the evidence format to
return, and stop conditions: if the code does not match the prompt, a command
fails after a reasonable retry, or the task needs out-of-scope files, stop and
report instead of improvising.
