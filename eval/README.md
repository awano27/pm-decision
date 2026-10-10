# Question-quality eval

`fixtures.jsonl` holds synthetic, labeled events for every shipped graph (99 events).
Each fixture labels some judge / plan nodes; all labeled questions for one event go to Jev in one request.

```bash
python eval/run_eval.py live --out answers.jsonl      # needs TYPESAFE_API_KEY
python eval/run_eval.py dump > requests.jsonl         # or run requests elsewhere (e.g. an MCP client)
python eval/run_eval.py score answers.jsonl
```

The report lists per-node `ok/n` and how many routed to `unsure` (a safe miss: the event goes to a human).

**Do not publish Jev results.** TypeSafe's terms forbid publishing Jev benchmark/performance data;
keep `answers*.jsonl` and scores out of this repository, issues and public CI logs.

`fixtures_preview_ja.jsonl` (58 events) holds what a PM sees in the Teams chat list (one-line Japanese previews, some cut
with …) and new ADO items, each labeled `want: auto | pm` (and `severe`). It measures how much reaches the PM, not node
accuracy: thanks / OK / FYI should not, decisions, requests, incidents must. Kev-4B (CPU fp32, 2026-10-10): 45 of 58 went
to the PM, 20 of the 33 `auto` ones (Teams `intent` picked `fyi` at 0.2-0.5 confidence, under 0.6); with the `ack_only`
rule 31 and 6; 0 `pm` events automatic and 0 severe misses both times. `tests/test_preview_prefilter.py` checks the rule on it.

## Per-backend thresholds

Node thresholds were tuned on Jev. A backend with a different confidence scale gets a profile in
`kimeru/profiles.py` (two global knobs: `conf_scale`, `noul_scale`). Tune on recorded answers — no
new model calls:

```bash
python eval/run_eval.py live --backend kev --out answers_kev.jsonl
python eval/run_eval.py tune answers_kev.jsonl           # grid search, never adds confidently-wrong decisions
python eval/run_eval.py score answers_kev.jsonl --profile kev
```

Kev-4B (local; publishing Kev numbers is fine). First run (2026-09-26, 197 model questions before the
rule-first nodes): 124 decided correctly, 70 sent to a human, 3 confidently wrong with Jev's thresholds;
126 / 68 / 3 with the `kev` profile (1.0 / 0.95). Current fixtures ask the model 175 labeled questions
(the rest are decided by match nodes): 121 correct / 51 to a human / 3 confidently wrong with the `kev`
profile, top-1 89%, median 3.9 s per request (CPU, 2026-09-27).
Lesson: a profile tuned on the first 39 events (0.8 / 0.75) looked better there but added confidently-wrong
decisions on 60 held-out events (3 -> 7). Always check a tuned profile on events it was not tuned on.

Current scoring (2026-10-06, commit cb13df1, Kev-4B, CPU bf16): 65 correct / 22 human / 10 fallback / 2 wrong / 0 severe on
the 99 events; 37 / 10 / 0 / 1 / 0 on the 48 held-out events. Details: docs/evaluation.md.

Values below were recorded before the C09 scoring change.
After rule-first nodes and sharper criteria (e2e, final action): 62 correct / 25 human / 12 fallback / 0 wrong
on the 99 events, 29 / 6 / 0 / 1 on `fixtures_holdout.jsonl` (36 events never tuned on;
`python eval/e2e.py --backend kev --fixtures fixtures_holdout.jsonl`).
Re-measured 2026-09-27 with stricter e2e scoring (correct = same terminal *and* same actions, including the
playbook; to-human = actually queued for the PM): unchanged. The 9 playbook disagreements all ended at a
different terminal (Kev was unsure and took the safe route), none were counted correct.

12 held-out alert events were added afterwards (alert-c01..c12). Their first run found one severe miss:
a Sev1 "availability test failing in 4 of 5 regions, order page returns errors" was scored as degraded, not
an outage, and got P1 without paging. Fix: a score-node guard (`guards` in graphs/monitor_alert.json) sends
a Sev0/Sev1 alert that the model rates below an outage to a person. With the guard: 62 / 25 / 12 / 0 on the
99 events (unchanged), 35 / 12 / 0 / 1 and 0 severe misses on the 48 held-out events (alerts 6 correct,
6 to a person). The alert part of the held-out set has now been looked at once, so it is no longer untouched.

CLM v0.1-8B (Contrastive-LM, Qwen3-8B Q8_0 embeddings via llama.cpp on CPU, ~1-2 s per question) was tried as
a backend: 26 / 44 / 14 / 15 wrong (2 severe misses) on the 99 events, 9 / 23 / 3 / 1 on the held-out set;
top-1 per question 49% vs Kev 85%. Its reference head is trained on coding-agent trajectories, and on these
Japanese PM questions it collapses score answers to the middle. Not used.

## Question-writing rules learned from this eval

- One condition per `noul`. "A and (B or C)" questions under-fire; split them into chained nodes.
- Ordered answers (priority, severity) are `score` nodes with bands, not `choice`: adjacent-level splits
  are normal and the expected value is still meaningful, so `min_conf` can be low.
- Never reference a state field that does not exist (e.g. `` `board` `` when the state has `text`).
- Do not put your own verdict into the state (e.g. a "（今日）" due label next to an item you ask to rank);
  Jev anchors on it.
- When a label disagrees with Jev, re-read the question literally before "fixing" the question — the label may be wrong.

## 要件の検討の依頼（`fixtures_requirements*.jsonl`）

```bash
python eval/e2e.py --backend kev --fixtures fixtures_requirements.jsonl           # 調整に使った 10 件
python eval/e2e.py --backend kev --fixtures fixtures_requirements_holdout.jsonl   # 調整に使っていない 10 件
```

要件の検討 6 件と、「要件」を含むが別の扱いになる 4 件（判断・進捗・共有・障害）です。

## 文面の質（`draft_quality.py`）

> 注意: writer（GitHub Copilot など）を実際に呼びます。12 イベントで約 15 回です。契約の月間の上限を使うので、回す前に残りを確認してください。

```bash
python eval/draft_quality.py --backend stub --writer copilot --per-kind 3 --show
python eval/draft_quality.py --fixtures fixtures_holdout.jsonl --per-kind 2
```

下書きごとに、日本語・英語の混入・定型文の複製・不自然な言い回し・材料にない権限者や完了・具体性・材料にない日付や数値を数えます（判定は LLM ではなく決まった規則なので、プロンプトの変更の前後で比べられます）。`--show` で、定型文と下書きを並べて読めます。最終的な確認は、読んで行ってください。
