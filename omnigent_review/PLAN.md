# omnigent_review: remaining work, plan for review

Status: proposal for outside review. Nothing below is built yet except where marked **Built**.
Branch: `spike/review-gate` (commits `7057bda`, `b85d78a`) on `jrcrittenden/omnigent`.

## 0. Goal and threat model

Make cross-model-family review a mechanism rather than a prompt, for Omnigent
orchestrators (Polly-style: a Claude brain dispatching claude_code / codex / agy /
opencode / hermes workers). The failure we are designing against is **correlated
error**: a plan, patch, or review that shares blind spots with its author because
it came from the same model family, plus an orchestrator that reports "done"
without evidence.

In scope threats: same-family review passing as independent; orchestrator claiming
readiness without review; reviewer silently editing code; test failures blamed on
(or hidden from) the wrong party; fake completion (skipped tests, stubs);
instructions embedded in a diff that target the reviewer ("approve this").
Out of scope: a malicious human operator; compromised CLIs; deliberate shell
obfuscation by the orchestrator (the human-merge step is the backstop).

## 1. What exists (Built)

- `omnigent_review/families.py`: model-to-family resolution, failing closed
  (`claude_code`/`codex`/`agy` inferred from agent name; `opencode`/`hermes`/
  `cursor`/`pi` need an explicit recognized `args.model`).
- `omnigent_review/ledger.py`: locked, atomic JSON ledger at `.polly/review_ledger.json`,
  used because the server policy engine has `session_state` but the runner-local
  gate does not.
- `policies.family_guard` (tool_call): records implement dispatches; denies a review
  dispatch whose family is unproven or equals the producer's. Reviewer titles are
  `review-<implementer title>@<suffix>`.
- `policies.verdict_recorder` (tool_result): records reviewer verdicts from the
  delayed child output the server posts (it carries agent, title, conversation_id).
  Verdict JSON must name `approve`/`block`, carry the reviewed `head_sha`, and every
  blocking finding needs file, integer line, and repro/evidence.
- `policies.ready_check` / `python -m omnigent_review finish`: ready only with N
  distinct non-producer families approving at the current head, latest verdict per
  family, any evidenced block vetoes.
- `policies.ready_write_guard`: denies direct "ready" writes to `registry.json`
  (textual; finish command exempt only when it is exactly that invocation).
- Skill `skills/cross-review-gated/SKILL.md`, `examples/guardrails.yaml`.
- Tests: 36 passing (unit through the real `FunctionPolicy` adapter, plus one
  integration test through `POST /v1/sessions/{id}/policies/evaluate`).

Verified by test: server-path policies see dispatch args and delayed child output.
**Not verified:** any live harness run; runner-local gate behavior; real reviewers
emitting the verdict block; Polly's actual `registry.json` schema (finish guesses
dict-of-tasks or list-of-tasks).

## 2. Recommended order and rationale

| # | Item | Why here | Needs live host? |
|---|------|----------|------------------|
| 1 | Live smoke run | Resolves the unverified assumptions that change items 3-5 | Yes |
| 2 | Deterministic gates (baseline diff + fake-completion scan) | Pure code, fully testable here, feeds `finish` | No |
| 3 | Reviewer isolation | Closes a real hole (reviewers are unsandboxed, yolo) | Verify live |
| 4 | Plan review | Reuses ledger/recorder; reviews the artifact with the least coverage today | No (mostly) |
| 5 | Canary harness | Measures whether any of this works; needs real reviewers | Yes |

Items 1 and 2 run in parallel (2 needs nothing from the host).

## 3. Item 1: live smoke run (no new code, a checklist)

Copy `examples/polly/`, merge `examples/guardrails.yaml`, add `cross-review-gated`
to its skills, run a small real task with at least two of Claude/Codex/agy.
Record, with evidence:

1. Does a same-family review dispatch get denied on the real path (and what text
   does the brain see)?
2. Does `verdict_recorder` fire for real child output; is the `request_data`
   shape (`args.title`, `args.conversation_id`) what the integration test assumed?
3. Do reviewers emit the fenced JSON block; how often is it malformed or missing?
4. Polly's real `registry.json` shape (adjust `finish._find_task`).
5. Do the guardrails run on the runner-local gate, the server engine, or both, for
   the brain's own tool calls? (`family_guard` writes the ledger in both; confirm.)
6. Does the model, when denied, reword to evade rather than choose another family?

Exit criterion: a written findings list; items 2-5 below are revised from it.

## 4. Item 2: deterministic gates

### 4a. Baseline-diff test gate

New `omnigent_review/gates.py` and CLI `python -m omnigent_review gate`.

Behavior: run the project's test command at head in the task worktree; if it fails,
run it at the base SHA in a throwaway detached worktree; new failures = head
failures minus base failures. Classify each new failure by re-running at head
`--reruns N` (default 2): fails every time = `real`; mixed = `flaky`; matches an
environment pattern (ModuleNotFoundError, connection refused, ENOSPC, timeout) =
`environmental`. Only `real` new failures fail the gate.

Per-test identity comes from JUnit XML (`--junit <path>`; pytest, jest-junit, and
go-junit-report all emit it). Without JUnit the gate falls back to exit-code only
and records `mode: coarse`; coarse results cannot distinguish pre-existing from new
failures and `finish` marks them as such.

Writes `gates[task] = {head_sha, base_sha, status, mode, new_failures[], ...}`
into the ledger. `ready_check` gains `require_gate=True`: gate must be present,
`pass`, and at the same `head_sha` as the verdicts.

Tests: temp git repos with a test that is (a) already failing at base, (b) newly
failing, (c) flaky, (d) environmental. Mutation check: gate must fail case (b) and
pass (a).

Risk: the ledger is forgeable by a direct file write from the orchestrator
(unsandboxed). Mitigation: extend `ready_write_guard` into a `ledger_write_guard`
denying `sys_os_write`/`sys_os_edit`/shell mentions of the ledger path other than
the three CLI commands. Same textual limits as today.

### 4b. Fake-completion scan

`gates.scan_diff(diff_text)` run by the same CLI over the diff's added/removed lines.

Blocking: added skip/xfail/only markers (`pytest.mark.skip`, `xfail`, `it.skip`,
`test.only`, `describe.only`, `t.Skip()`), net deletion of test functions or
assertions without replacement, lowered coverage thresholds, `--no-verify`,
`NotImplementedError` or `pass` as a sole function body in non-test code.
Warning: new `TODO|FIXME`, new `# type: ignore` / `noqa` / `eslint-disable`.
Per-line allowance `review-gate: allow <reason>` is recorded in output, not silent.

Tests: a table of diffs per pattern, plus true-negative diffs (a legitimately
removed obsolete test with its replacement). Output: file:line findings stored in
the gate record; blocking findings fail the gate.

## 5. Item 3: reviewer isolation

Finding: `purpose` is dispatch metadata. The child's own policy engine never sees
it and Polly's worker configs are shared between implement and review, so
"reviewers are read-only" cannot be a policy keyed on purpose. It must be separate
reviewer agent entries.

Changes:
1. Add `*_reviewer` agent configs (`claude_code_reviewer`, `codex_reviewer`,
   `agy_reviewer`, `opencode_reviewer`, `hermes_reviewer`) derived from the worker
   configs: `read_only_os` policy, a shell write-deny policy (redirects, `tee`,
   `sed -i`, `git commit/checkout/apply/reset/clean`), `yolo: false` where the
   harness supports it, and an `os_env.sandbox` with no write paths except a
   scratch dir (`linux_bwrap`), read access to the snapshot path.
2. `family_guard`: add `agent_aliases` (`codex_reviewer` -> `codex`) so family
   resolution works; deny a review dispatch to a non-reviewer agent and an
   implement dispatch to a reviewer agent.
3. Reviewer input is a snapshot directory (a detached export of head), never the
   implementer's worktree, so even a successful write corrupts only a disposable tree.
4. `finish` additionally requires the implementer worktree's `HEAD` to equal the
   verdict `head_sha` and `git status --porcelain` to be clean.

Unknowns to verify live (item 1): whether the Omnigent sandbox wraps native
harnesses (claude-native / codex-native run their own TUI, and `yolo` bypasses
Codex's own sandbox); whether `read_only_os` native-tool aliases cover the shell.
If the sandbox does not wrap native harnesses, the disposable snapshot plus the
shell-deny policy is the defense and we say so.

Tests: adversarial reviewer prompt instructing a write; assert denial or that the
real worktree is untouched. Policy unit tests for the shell-deny patterns.

## 6. Item 4: plan review

The brain (Claude) writes the plan, and today its plan is reviewed by no one but the
human. Changes:

1. `plan_lint` (deterministic, before any LLM): `plan.json` schema where every task
   has objective, files, interfaces, verification command, stop conditions,
   acceptance criteria; file sets of parallel tasks must be disjoint.
2. Skill `plan-review`: dispatch reviewers from a family different from the brain's
   (`purpose: "review"`, title `plan-review@<suffix>`), verdict JSON keyed by
   `plan_sha256` with finding categories (missing-requirement, unsafe-ordering,
   file-overlap, untestable-criterion).
3. `plan_gate` policy: deny the first `implement` dispatch until a valid approval
   exists for the current plan hash. Skipping for small tasks is an ASK to the
   human, not a model choice.
4. Brain family: from `event.context.model` on the server path; on the runner-local
   gate it is absent, so take a `brain_family` factory parameter and fail closed.

Tests: reuse the ledger/recorder harness; a plan edited after approval must lose
approval (hash mismatch).

## 7. Item 5: canary harness

`omnigent_review/canary.py`. Take a clean snapshot with passing tests, inject a
realistic bug from a catalog (off-by-one, inverted condition, removed auth/null
check, swapped arguments) producing a diff and hidden ground truth, run reviewers
through the normal dispatch path in a throwaway session, score catch rate by a
blocking finding within +/-N lines of the mutation. Persist per-family and
per-model results to `.polly/canary.json`; warn when a reviewer's rate is under a
threshold. Include prompt-injection canaries (a diff comment telling the reviewer
to approve) and clean-diff canaries to measure false positives.

Sampled periodically, not per run, for cost. Tests here cover injection, scoring,
and persistence with fake reviewers; real catch rates need live reviewers.
Caveat: models may recognise synthetic mutations, so keep a held-out bug set and
rotate it.

## 8. Questions for the reviewer

1. Ledger is a file plus textual write guards, and the orchestrator is unsandboxed.
   Is that acceptable residual risk, or should state move somewhere the
   orchestrator cannot write (server-side store, a sandboxed orchestrator)?
2. Family granularity: GPT and o-series are one family; DeepSeek, Kimi, GLM, Qwen
   each their own. Are those actually independent enough (distillation, shared
   data), and is "family" the right unit versus training lineage?
3. Tier rule: small = 1 family, large = 2. Is the trigger (multi-module, migration,
   API shape, security path, second failed fix loop) right?
4. Baseline strategy for repos without JUnit: is a coarse exit-code mode worth
   shipping or should the gate refuse?
5. Reviewer isolation: is "separate reviewer agents + disposable snapshot + shell
   deny" sufficient if the sandbox does not wrap native harnesses?
6. Prompt injection via the diff: is a "treat diff as untrusted data" prompt plus
   canaries enough, or is structural separation needed?
7. What is missing from the threat model, and which item should be cut?

## 9. Not planned

Per-model routing tables, notification systems, a standalone runner, swarm or
consensus machinery, auto-learning. Omnigent already supplies hosts, worktrees,
dispatch, spawn bounds, and cost caps.
