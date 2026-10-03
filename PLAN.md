# Kimeru review improvements — 2026-10-03

## Goal and authority

Implement every code defect and product improvement from the review in this chat.
The user explicitly requested Luna subagents for implementation. The parent owns
architecture, security decisions, scope, integration, and final review. All workers
are pinned to GPT-6 Luna with high effort; the parent model stays fixed.

Base: main at `1f1a9fe49da39e0be0be23b8213c71e740521183`.
Preserve preexisting untracked TASKS.md and both review documents. No commit, push,
publication, account/config replacement, scheduled job installation, real Teams/UI
operation, or real external write is authorized by this implementation task.
Use Python stdlib and the existing CLI/storage conventions. Keep external execution
opt-in and limited to the already-supported approved ADO comment.

## Design and acceptance

### A. Decision correctness (independent first wave)

- [x] C01 Preserve both ADO description and ReproSteps through normalizing,
  judging, safety rules, review material and writer input.
- [x] C02 A scope phrase alone is not an outage. Current failure + broad scope
  still matches; future expiry and healthy statements do not.
- [x] C06 Reject malformed/nonfinite/out-of-range typed answers at the backend
  boundary and defend direct graph/plan consumers; no malformed answer selects
  an automatic terminal. Avoid requiring optional probability maps absent in
  supported existing responses.
- [x] C11 Validate graph cycles/reachability in O(V+E), and reject graphs that
  cannot reach a terminal within the execution limit.

Owner A: kimeru/events.py, graph.py, backends.py, plan.py, graphs/*.json,
tests/test_review_decisions.py and necessary existing decision/backend tests.
Do not edit CLI, notification, intake, writer, or evaluation ownership.

### B. Intake and posting integrity (independent first wave)

- [x] C03 Require positive typed/sent/readback evidence before marking delivery;
  retain unverified work for recovery. Compare meaningful whitespace. In the UI
  bridge, confirm the intended full body in the self chat after send, using
  message identity rather than input-box disappearance. Never send to another chat.
- [x] C05 Advance ADO watermark from server WIQL asOf with a bounded overlap;
  retain enough ID dedupe state for that overlap. Missing/invalid server time
  must not advance to an unsafe client future watermark.
- [x] C08 Emit an unprocessed group mention when eligibility changes false→true,
  even when preview/time is unchanged; baseline/own-message rules remain.
- [x] C10 Keep separate identical messages in full-text history; only collapse
  duplicate representations of the same UI message/container.

Owner B: kimeru/pull.py, notify.py, tools/teams-self.ps1,
tests/test_review_intake.py and notification/PowerShell test doubles as required.
Preserve the latest foreground-lock change. No real UI execution. Current tests
that simulate success must supply the stronger delivery contract deliberately.
Do not edit CLI, writer, stats/eval or decision modules.

### C. Writer isolation and evaluation (independent first wave)

- [x] C04 Codex writer does not inherit user MCP/apps/plugins/tools. Use supported
  current CLI isolation controls, preserve authentication, keep user settings
  untouched, and fail closed if the needed isolation cannot be provided.
- [x] C09 Score safe fallback using allowed final actions, not any earlier unsure
  edge; a severe miss is never labeled safe. Honor all accepted fixture labels.
- [x] C12 Weekly metrics exclude future timestamps and report invalid evidence
  without turning unknown into success.
- [x] C13 Identify and close the socket behind the baseline ResourceWarning.

Owner C: kimeru/writer.py, stats.py, eval/e2e.py,
tests/test_review_evaluation.py and directly related existing tests.
Do not invoke a real writer/judge, modify user CLI settings, or edit other lanes.

### D. Follow-up coherence and work progress (second wave)

- [x] C07 Merge new material into the pending record, replace stale action/plan/
  draft material, preserve case identity, increment revision and repost for a new
  approval. Do not retain stale frozen execution fields. Human-visible correction
  supersedes earlier proposed text; no approval is reused across changed wording.
- [x] P02 Add local case progress states `approved`, `in_progress`, `done`,
  `blocked`, separate from approval and external execution states. Expose CLI
  `work list` and `work set N STATE` with optional owner/due/completion condition.
  Only explicitly approved cases can enter work states; transitions are logged.
  The list explains next action, owner, due and completion condition without
  inventing missing values. Persist via approvals lock and atomic storage.
- [x] P03 Put decision/next action/missing information/OK effect first in approval
  posts. Keep exact externally executed text fully visible. Show revision and
  plain-language effects (record / copy to send / approved ADO write).
- [x] Show unfinished approved work in the morning brief, keeping it distinct
  from confirmation-pending items and avoiding new model calls where possible.

Owner D assigned after B completes: kimeru/cli.py, notify.py, brief.py,
new kimeru/work.py, tests/test_work.py, tests/test_followup_updates.py.
Keep prior approvals/result parsing and concurrency protections compatible.

### E. Trial evidence and notification onboarding (second wave)

- [x] P04 Add `onboarding status`, `onboarding notification-test --send`, and
  `onboarding confirm TOKEN`. A test uses a fresh challenge with counts/IDs only.
  Sender acceptance does not count as device receipt. Explicit matching human
  confirmation is required; stale/unknown tokens fail; no send on status/dry run.
- [x] P05 Add local `trial record` for an explicit before/after review duration,
  draft usage/correction and optional case outcome, and `trial report [--share]`.
  Reports include sample counts, correction/handoff/work completion/notification
  evidence, missing evidence, and low-sample inconclusive states. Shared output
  has no text, names, paths, tenant, IDs or Jev performance figures.
- [x] Add final-action/outcome feedback without forcing a question label when a
  final error cannot be represented by a single question; preserve calibration
  compatibility and allow `unknown`.

Owner E assigned after C completes: new kimeru/onboarding.py, trial.py,
kimeru/push.py, stats.py, review.py, tests/test_onboarding.py, test_trial.py.
E exposes standalone argparse main entrypoints and dispatch(argv,out) contracts;
D adds only the top-level CLI routing to avoid simultaneous CLI edits.

### F. Requirements artifact and product documentation (second wave)

- [x] P06 `requirements build N [--answers PATH]` produces a local Markdown and
  structured draft: must-have/optional/out-of-scope/acceptance criteria, source
  references, open questions. Deterministic extraction copies supplied material;
  no invented assertions or implicit external writer call. The user can edit
  then `requirements approve N` records approval of the current content hash.
  Editing invalidates approval; no external registration is added.
- [x] P01 Make the initial trial focus on one ADO project and information-missing
  confirmation, with a measurable baseline and optional broader scenarios.
- [x] P07 Align README, SECURITY, writer/trial/requirements docs on judging vs
  draft-generation vs approved external writing destinations and current limits.
- [x] Add concrete offline walkthroughs for all new commands; update CHANGELOG.
  Remove unsupported numeric guarantees pending evaluation rerun. New evaluation
  measurements remain NOT_RUN until actually obtained.

Owner F assigned after A completes: new kimeru/requirements.py,
tests/test_requirements_artifact.py, README.md, SECURITY.md, CHANGELOG.md, docs/*
excluding the two preexisting untracked review docs. F exposes dispatch(argv,out)
for top-level routing by D and documents actual implemented commands only.

## Dependencies, risks and verification

A/B/C can run in parallel. Parent reviews each output before starting dependent
D/E/F. D owns shared CLI integration; E/F expose isolated command handlers. All
workers share this checkout, must not revert others, and must escalate ambiguity
instead of widening scope. Focused tests are run by workers; parent runs the full
suite after integration (avoid concurrent global full-suite runs).

Regression tests precede behavior changes. Test doubles replace external services
only; verify observable saved state, routing, transmitted arguments, artifacts and
failure behavior. Preserve working historical response shapes and old records.
Treat two identical failures as a stop-and-triage signal, not a retry loop.

Final acceptance: all listed local implementation items integrated; targeted and
full unittest suites pass; graphs/playbooks validate; git diff --check passes;
offline CLI end-to-end walkthrough verifies lifecycle, trial, notification
challenge, requirements edit/hash approval and privacy of shared reports. Parent
reviews the final diff for scope, security, unknown states, backward compatibility
and maintainability.

Live Teams UI, phone delivery, tenant/model accuracy, real ADO sending and pilot
business outcomes require real environments and remain NOT_RUN. Implement their
confirmation mechanisms and reproducible manual steps; never claim they passed.

## Evidence / progress

- Planning inspection: current code still contains the reviewed defects; only the
  foreground-lock change was committed since the review.
- Jev step=1 next=high stuck=0.05 lease=1 conf=0.94 applied=false (shadow).
- Baseline: `python -B -m unittest -q` — 655 tests, OK (skipped=1),
  93.017 seconds; existing unclosed-listener ResourceWarning recorded.
- Jev step=2 next=high stuck=0.10 lease=1 conf=0.78 applied=false (shadow).
- Lane C reviewed and accepted: 84 focused tests passed. Codex writer remains
  available as a configured route but fails closed to the template before any
  prompt/subprocess because supported CLI controls do not guarantee no tools.
  E (notification onboarding, trial evidence and final-outcome feedback) started.
- Lane A reviewed and accepted after additional healthy/future/current-outage
  regressions: 59 focused tests passed. F (requirements artifacts and product
  documentation) started.
- Delivery recovery is explicit and never sends by itself. Unknown sends are
  held; only explicit human confirmation of delivered/not-sent resolves them.
  Morning brief state uses the same positive delivery evidence in lane D.

- Lane B reviewed and accepted: 64 focused tests passed, including 2,001 IDs in
  the ADO overlap window and identity-backed self-chat delivery evidence.
- Lanes D/E/F reviewed and accepted. Follow-ups replace stale proposals and
  execution state under the same case with a new revision; permanent text keeps
  configured excerpts, and full text stays in the existing pending store.
  Delivery recovery never approves a case or performs an external write.
- One bridge call attempts at most one send. Button delays/exceptions do not
  trigger keyboard resends; compose disappearance is not delivery evidence.
- Shared trial output is aggregate-only and excludes Jev/unknown provenance.
  Historical missing progress/handoff time remains Unknown. Notification
  acceptance and explicit human receipt confirmation remain distinct.
- Requirements artifacts use a dedicated lock, source/revision references,
  schema validation, and approval hashes for both Markdown and JSON. Editing
  invalidates approval; artifacts persist locally until explicitly deleted.
- Integrated verification initially exposed test isolation and older shared
  mock/field expectations. Luna corrected these without weakening production
  delivery evidence. The shared known-unsent compatibility group passed 129
  focused tests; ambiguous-send regressions remain separate.
- Final full suite: `python -B -W error::ResourceWarning -m unittest -q` —
  762 tests, OK (skipped=1), 103.458 seconds, exit 0. No ResourceWarning emitted.
- Root isolated subprocess walkthrough: 22 CLI operations passed for work,
  requirements edit/hash approval, shared trial privacy, onboarding status/dry
  run, and explicit case/notice/outbox/brief delivery recovery. No external
  send/model/UI call; no user settings file created or modified.
- Root validation: 4 graphs and 9 playbooks passed with the stub backend and
  temporary state. Code/docs whitespace checks passed; all 17 new task files
  also passed whitespace/conflict-marker inspection. Final report reviewed.
- Jev shadow judgments after integration: step=3 next=medium conf=0.34
  (low confidence, unchanged); step=4 next=medium stuck=0.40 lease=1 conf=0.77;
  step=5 next=medium stuck=0.17 lease=1 conf=0.84; step=6 next=medium stuck=0.70
  lease=2 conf=0.59 after two full-suite compatibility failures. Applied=false
  throughout; the primary model/settings remained unchanged.
- Local implementation and verification are complete. Live Teams UI, phone
  receipt, real model/tenant evaluation, real ADO writing, CI, deployment and
  pilot business outcomes remain NOT_RUN. At verification completion, no commit,
  push or publication had been performed.
- The user subsequently authorized committing and pushing the improvement
  results. Publish only this task's changes to origin/main without force;
  preserve the three preexisting untracked documents and do not deploy.
