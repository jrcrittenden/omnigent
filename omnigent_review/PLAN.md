# omnigent_review: plan v2 (revised after outside review)

Branch `spike/review-gate` on `jrcrittenden/omnigent`. Nothing past section 1 is built.
v1 of this plan was reviewed externally; that review's six core findings are accepted
and drive the structure below. Section 9 records each decision, including two places
where code reading qualifies the reviewer's recommendation.

## 0. Claim levels and the acceptance test

Until the acceptance test passes, the system reports **"workflow checks completed,
with these limits"**, never "enforced readiness".

| Level | Where verdict/gate state lives | May claim |
|-------|-------------------------------|-----------|
| L0 advisory | Local JSON ledger, orchestrator-writable (today) | "checks recorded locally" |
| L1 server state | Server `session_state`, written only by policies from attested events | "checks recorded by the server" |
| L2 enforced | L1, plus the orchestrator provably cannot write the state store or reach reviewer credentials (remote/managed server, or sandboxed orchestrator) | "readiness enforced" |

**Acceptance test.** Can an orchestrator obtain "ready" without the specified review
and test evidence existing for the exact artifact submitted? Operationalised as a
red-team suite where a scripted adversarial orchestrator tries each of: forge the
ledger; write "ready" by shell/edit/python; replay a prior verdict on a new head;
substitute a reviewer or reuse a conversation; approve-after-block shopping; edit
after approval; submit with a dirty tree; poison test config; downgrade the tier;
claim a family it did not use. Each attempt must fail at the claimed level. The
suite is the exit gate for each phase and is built first (section 3).

## 1. What exists (Built, L0 advisory)

`families.py` (fail-closed family resolution), `ledger.py` (locked JSON file),
`policies.family_guard`, `verdict_recorder`, `ready_write_guard`, `ready_check`,
`finish` CLI, `skills/cross-review-gated`, `examples/guardrails.yaml`; 36 tests.

Known weaknesses of what is built (each is fixed in a phase below):
latest-verdict-per-family erases an unresolved block (approval shopping); the head
SHA does not bind the tested tree, command, config or environment; the ledger is
orchestrator-writable; family is inferred from requested args, not observed;
file:line is required for every blocking finding. `finish` now labels its output
advisory (this commit).

## 2. Order

| Phase | Work | Needs live host |
|-------|------|-----------------|
| A | Live dispatch and containment checks | yes |
| B | Trusted evidence bindings and state | partly (design checks from A) |
| C | Test-gate semantics (pass / fail / inconclusive) | no |
| D | Isolated review | verify live |
| E | Plan review | no |
| F | Canaries | yes; deferred until B-D produce evidence |

## 3. Phase A: live checks (each is a pass/fail observation with evidence)

Dispatch and wiring:
1. Same-family review dispatch is denied on the real path; what the brain sees.
2. `verdict_recorder` fires for real child output; `request_data` shape matches.
3. Which engine runs the brain's guardrails: server, runner-local gate, or both.
   The runner-local gate has no `session_state`; `context.conversation_id` is None
   without an engine and is the marker to distinguish them.
4. Reviewers' verdict-block compliance rate.
5. Polly's real `registry.json` shape.
6. Does a denied brain reword to evade rather than pick another family?

Authority and containment (new):
7. Can the orchestrator's shell write the server state store? Local mode: where is
   the DB (code suggests a file in the data dir) and is it reachable by the same OS
   user. Is there any REST route reachable with host credentials that sets
   `session_state`? (Code search found none, not exhaustive.)
8. Can an agent `type: function` tool (deterministic Python run on the runner) have
   its `tool_result` recorded by a server-engine policy into `session_state`? This
   is the proposed attestation channel (section 4).
9. Can a tool_call policy inject or transform dispatch input (e.g. a review ID)?
10. Is there evidence of the model a child actually ran (child conversation record,
    `llm_response` events, usage rows), beyond the requested `args.model`?
11. Reviewer reach: from a codex/claude/agy native reviewer, attempt to read
    credentials (`~/.omnigent`, `~/.claude`, env), other worktrees, the ledger, the
    network; attempt writes. Native codex harness code configures Codex's own
    sandbox (`writable_roots`, `bypass_sandbox`), so containment for native workers
    is probably the vendor sandbox mode, not Omnigent's; confirm per harness.

Exit: a findings list; section 4-6 designs are adjusted to it. If checks 7-8 fail,
the maximum claim level is L0.

## 4. Phase B: trusted evidence and state

**Evidence record** (canonical JSON, hashed, immutable once attested):
repository identity (remote URL + root commit), `base_sha`, `head_sha`,
`tree_sha` of the committed tree, working-tree-clean assertion, test argv and the
hashes of config files that govern it (pytest.ini/conftest.py/pyproject test
sections/package.json scripts/CI workflow/lockfiles), environment identity (OS,
runtime versions, lockfile hash, image id if any), result artifact paths with
SHA-256, tier classification, and tool versions.

**Clean tree is required before testing and before review, and re-verified at
finish.** Dirty or untracked-nonignored state aborts the step. This is done by one
deterministic snapshot tool, not by the orchestrator: it verifies cleanliness,
exports an immutable snapshot, and emits the evidence record.

**Attestation channel.** Deterministic evidence tools (`snapshot`, `gate`, `finish`)
are agent `function` tools, not shell commands. The orchestrator controls only their
arguments; their results arrive as `tool_result` events, and a server-engine policy
records them into `session_state` under a non-reserved key (`_policy_*` keys require
an approved ASK). The orchestrator cannot construct a `tool_result` event. If runtime
check 8 fails, fall back to L0 and say so.

**Review identity.** Each review is an assignment record created at dispatch:
`review_id`, task, artifact (`tree_sha`, `head_sha`), requested agent and model,
resolved family, title. Titles must be unique per review (reuse for another review is
denied). A conversation ID binds to one review on first sight and cannot satisfy a
second. A verdict is accepted only from that dispatch's completion channel, must echo
`review_id`/`tree_sha`/`head_sha`, and is single-use (replay denied). If the
orchestrator omits the review ID from the reviewer's input, the verdict is invalid:
the failure mode is lost approval, not forged approval. If check 9 shows policies can
inject input, the policy supplies the ID instead.
Family claims are labelled `requested` until check 10 yields observed-model evidence;
then `observed`, and mismatch denies.

**Findings lifecycle.** Findings get stable IDs (hash of review, location, category).
States: `open`, `resolved`, `superseded`, `waived`. A later approval does not erase
an open blocking finding; it must list `resolves: [ids]` with verification evidence,
or a human waives it (ASK). `finish` requires zero open blocking findings across all
reviews of the task's lineage. Replaces "latest verdict per family". Scope field:
`file` (needs file and line), `repo`, `omission`, `architecture` (need evidence and
affected area, no line). Interim fix in this commit: a valid block at a given head is
never erased by a later approval at the same head.

**Tier classification** is computed at plan time, frozen in attested state, and
escalated automatically (never downgraded by the orchestrator or implementer) when the
actual diff touches security-sensitive paths, migrations, API/schema files, protected
config, or exceeds size/module thresholds; irreversible changes escalate regardless of
size. Downgrade needs a human ASK.

**Readiness output.** `finish` emits an attestation bundle (evidence record hash,
review IDs, finding states, waivers, claim level). The human merge step reads this
bundle from the server, not the orchestrator's registry file, which becomes a
non-authoritative convenience. A human merge is a backstop only if that bundle
reaches them independently.

## 5. Phase C: test-gate semantics

Three outcomes: `pass`, `fail`, `inconclusive`. Only `pass` is ready; `fail` and
`inconclusive` require resolution or an explicit scoped waiver (human ASK, naming
test IDs, reason, and expiry).

- New failure at head, absent at base: `fail`.
- Newly flaky (differs across reruns): `inconclusive`, still a new failure.
- Error text (ModuleNotFound, connection, timeout, ENOSPC) is never grounds to
  exonerate. A new failure matching those patterns is `inconclusive` with the pattern
  recorded as a hint, since a missing import can be a regression.
- Fails at base and head with the same signature: `preexisting`, listed in the
  attestation (visible, non-blocking). Different signature: `inconclusive` (masked
  regression). Baseline comparison shows a failure was seen before, not that it is
  acceptable, so preexisting failures in code the diff touches need a waiver.
- Coarse mode (no JUnit): a successful command satisfies a command-level check; a
  failing command cannot be baseline-exonerated and is `fail`.
- The test command and governing config hashes are frozen at plan time. A diff that
  changes test config, conftest, CI, lockfiles, or coverage thresholds makes the gate
  `inconclusive` pending waiver; optionally also run head tests under base config.
- Gate and baseline runs record the full evidence record (section 4).

Fake-completion scan: a **finding generator**, not a verdict. It emits findings
(added skip/xfail/only, removed tests or assertions, `pass`/`NotImplementedError`
bodies, lowered thresholds, `--no-verify`, TODO/FIXME, lint suppressions) with
file:line into the review as evidence for reviewers. An inline `review-gate: allow`
comment is recorded as the author's explanation and never as a waiver.

## 6. Phase D: isolated review

Described as "operating on disposable copies, containment unverified" until Phase A
check 11 proves a boundary per harness.

1. `*_reviewer` agent entries (purpose is dispatch metadata the child never sees).
2. Reviewer reads only an immutable snapshot from the snapshot tool.
3. Least privilege per harness: vendor sandbox read-only mode for native codex/claude,
   `read_only_os` for tool aliases, no credentials in the environment, network off
   where supported. Shell-deny patterns are retained as diagnostics (they log an
   attempted write), not as containment.
4. Assignment and verdict transport live outside repository content. The reviewer
   prompt treats the diff and repository instructions (CLAUDE.md, AGENTS.md, comments)
   as untrusted data; susceptibility is measured by canaries, enforcement comes from
   privilege minimisation and the out-of-band verdict channel.
5. `family_guard` gains `agent_aliases` and denies review dispatch to non-reviewer
   agents.

## 7. Phase E: plan review

`plan_lint` (deterministic) checks task schema and, beyond file overlap, shared
interfaces, schemas, generated artifacts and runtime resources (ports, DB, caches),
plus a post-hoc comparison of each task's declared file set against its actual diff
at gate time (undeclared files escalate). Reviewers of the plan come from a family
different from the brain's (brain family from `context.model` on the server path, a
fail-closed parameter otherwise). Plan approval is keyed by plan hash; skipping is a
human ASK.

## 8. Phase F: canaries (deferred)

Start after B-D produce attested evidence. Inject realistic defects, run reviewers
via the normal path, and score **defect identification** (is the finding the injected
defect, or its downstream consequence, or a different problem) in addition to
localisation. Include clean diffs (false positives) and diff-embedded injection
attempts. Keep a held-out, rotated bug set. Results inform routing; they do not
establish independence.

## 9. Decisions on the external review

| Question / finding | Decision |
|--------------------|----------|
| Gate fails open on ambiguous failures | Accepted: three outcomes, no pattern exoneration (5) |
| SHA does not bind evidence | Accepted: evidence record, clean tree before and after (4) |
| Orchestrator manufactures authority | Accepted: attestation channel and claim levels. Qualification: server `session_state` is writable only by policies as far as code search shows, but in local mode the DB likely sits where the unsandboxed orchestrator's user can write it, so L2 needs a remote server or sandboxed orchestrator |
| Snapshot is not isolation | Accepted: reworded; containment checks first (3, 6) |
| Review identity underbound | Accepted: assignment records, single-use verdicts, requested vs observed model (4) |
| Latest-per-family erases blocks | Accepted: finding lifecycle; interim same-head fix (4) |
| Writable ledger | Advisory prototype only (L0); leave L0 by Phase B |
| Family granularity | Routing heuristic only; store exact model and provider |
| Tiers | Frozen at plan time, auto-escalate, human-only downgrade (4) |
| Coarse mode | Ship with stated limits (5) |
| Snapshot + shell deny | Not containment (6) |
| Diff injection | Out-of-band transport and least privilege; canaries measure only (6, 8) |
| Missing threats | Added to section 0 red-team suite: stale evidence, replay, concurrent task contamination, reviewer substitution, approval shopping, poisoned test/config |
| Cut | Canaries deferred (8) |

## 10. Questions for the next review round

1. Is the function-tool attestation channel sound, or is there a bypass where an
   orchestrator can still cause a forged `tool_result` or choose arguments that
   produce a misleading but genuine evidence record (e.g. testing a different
   worktree)?
2. Should the evidence tools themselves run in a separate sandboxed process from the
   orchestrator?
3. Is the finding-resolution rule (explicit `resolves` IDs with evidence, human
   waiver otherwise) strict enough, or does it create deadlock for reviewers who
   disagree on a finding?
4. Is single-use verdict binding by title + conversation ID adequate if the
   orchestrator can choose titles?
5. Which acceptance-suite attack is missing?
