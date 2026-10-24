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

## Where the data was used

"Held-out" below is the name of `fixtures_holdout.jsonl`, not a promise that it was never looked at. All of it has been used
in development: the first 36 events to pick the Teams question wording and the minutes options (2026-09-26), the 12 alert
events to design the Sev0/1 guard (2026-09-27). `fixtures_preview_ja.jsonl` designed the `ack_only` rule and
`fixtures_context.jsonl` decided the `judge_context` gate. Only `fixtures_requirements_holdout.jsonl` is recorded as unused
(measured once, the day it was made; no result file is kept). **No re-evaluation on data unused in development exists yet.**
The same table, in Japanese: docs/evaluation.md ("評価データの来歴").

## Per-backend thresholds

Node thresholds were tuned on Jev. A backend with a different confidence scale gets a profile in
`kimeru/profiles.py` (two global knobs: `conf_scale`, `noul_scale`). Tune on recorded answers — no
new model calls:

```bash
python eval/run_eval.py live --backend kev --out answers_kev.jsonl
python eval/run_eval.py tune answers_kev.jsonl           # grid search, never adds confidently-wrong decisions
python eval/run_eval.py score answers_kev.jsonl --profile kev
```

**The numbers below are historical measurements retained from public main**, including runs before and after C09.
Real-model re-evaluation of this integrated version is **NOT_RUN**, as is evaluation on real (anonymized) data
(see `docs/evaluation.md`). These records are not evidence of a new run on this version.

Kev-4B (local; publishing Kev numbers is fine). First run (2026-09-26, 197 model questions before the
rule-first nodes): 124 decided correctly, 70 sent to a human, 3 confidently wrong with Jev's thresholds;
126 / 68 / 3 with the `kev` profile (1.0 / 0.95). Current fixtures ask the model 175 labeled questions
(the rest are decided by match nodes): 121 correct / 51 to a human / 3 confidently wrong with the `kev`
profile, top-1 89%, median 3.9 s per request (CPU, 2026-09-27).
Lesson: a profile tuned on the first 39 events (0.8 / 0.75) looked better there but added confidently-wrong
decisions on 60 held-out events (3 -> 7; the other 60 of fixtures.jsonl, not fixtures_holdout.jsonl). Always check a tuned
profile on events it was not tuned on. The `kev` profile in use (1.0 / 0.95) was chosen on all 99 events of fixtures.jsonl
(before fixtures_holdout.jsonl existed) and has not been checked on events unused in development (the 48 held-out events
were used for the question wording and the guard).

How e2e.py scores: the expected path of an event answers each labeled judgment with its label and every other question with the
model's own answer, so an unlabeled question is never scored. "Correct" means the labeled judgments did not change the final
action, not that every question was answered right. An event with no labeled judgment on its path is always "correct", and
"to-human" counts only the events that differ from the expected path and went to a person (e2e.py does not count the former yet).

Post-C09 scoring record (2026-10-06, commit cb13df1, Kev-4B, CPU bf16): 65 correct / 22 human / 10 fallback / 2 wrong / 0 severe on
the 99 events; 37 / 10 / 0 / 1 / 0 on the 48 held-out events. Details: docs/evaluation.md.

Same weights on CPU fp32 (2026-10-13, commit f102813, all new roadmap settings at their defaults): 66 correct /
21 human / 10 fallback / 2 wrong / 0 severe on the 99 events (the same two wrong as bf16: teams-5, ado-6); 36 / 9 / 0 / 3 / 0
on the 48 held-out events (wrong: ado-c06, minutes-c07, alert-c11). The bf16 numbers above are unchanged. A control run at
e90e9dd, the commit before the new settings were merged, with the same fp32 Kev gave the same results 3 times out of 3, with
byte-identical judge inputs and answers before and after the merge and no run-to-run variance; so the difference from
37 / 10 / 0 / 1 / 0 is bf16 vs fp32, not a regression. Over the 99 + 48 events: 10.7 s per event on average (11.3 s dev, 9.6 s held-out; max 39 s). Details:
docs/evaluation.md.

Context gate (`fixtures_context.jsonl`, 4 events that carry the chat's earlier messages; Kev-4B, CPU fp32, 2026-10-13):
without the earlier messages 1 correct / 3 human / 0 wrong; with them (`judge_context`) 2 / 2 / 0. Only ctx-4 changed
(`status` confidence 0.53 -> 0.72, `ask_pm` -> `reply_status`); ctx-1 and ctx-3 moved toward the right intent
(0.12 -> 0.29, 0.16 -> 0.28) but stayed at `ask_pm`; about 2-5 s slower per event. The default stays `judge_context=0`.
`e2e.py` passes the context from the fixture's `context` key and does not read the `judge_context` setting: to measure
"without context", run a copy of the fixture file with that key removed.

Values below were recorded before the C09 scoring change.
After rule-first nodes and sharper criteria (e2e, final action): 62 correct / 25 human / 12 fallback / 0 wrong
on the 99 events, 29 / 6 / 0 / 1 on `fixtures_holdout.jsonl` (36 events; not untouched: the Teams wording and the
minutes options were picked on them the day they were added, see "Where the data was used" above;
`python eval/e2e.py --backend kev --fixtures fixtures_holdout.jsonl`).
Re-measured 2026-09-27 with stricter e2e scoring (correct = same terminal *and* same actions, including the
playbook; to-human = actually queued for the PM): unchanged. The 9 playbook disagreements all ended at a
different terminal (Kev was unsure and took the safe route), none were counted correct.

12 held-out alert events were added afterwards (alert-c01..c12). Their first run found one severe miss:
a Sev1 "availability test failing in 4 of 5 regions, order page returns errors" was scored as degraded, not
an outage, and got P1 without paging. Fix: a score-node guard (`guards` in graphs/monitor_alert.json) sends
a Sev0/Sev1 alert that the model rates below an outage to a person. With the guard: 62 / 25 / 12 / 0 on the
99 events (unchanged), 35 / 12 / 0 / 1 and 0 severe misses on the 48 held-out events (alerts 6 correct,
6 to a person). The alert part of the held-out set was used to design the guard, so it is no longer untouched either.

CLM v0.1-8B (Contrastive-LM, Qwen3-8B Q8_0 embeddings via llama.cpp on CPU, ~1-2 s per question) was tried as
a backend: 26 / 44 / 14 / 15 wrong (2 severe misses) on the 99 events, 9 / 23 / 3 / 1 on the held-out set;
top-1 per question 49% vs Kev 85% in that comparison run (the 89% above is the 2026-09-27 run of the
175 labeled questions). Its reference head is trained on coding-agent trajectories, and on these
Japanese PM questions it collapses score answers to the middle. Not used.

## Your own labeled decisions (`dataset_eval.py`, local only)

`kimeru review` writes labeled rows to `<state folder>/fixtures_user.jsonl`. To evaluate on them without putting business
data in the repository:

```bash
python -m kimeru dataset anonymize                       # candidate + review_PRIVATE.txt; the source is not changed
python -m kimeru dataset mark-reviewed DIR --i-reviewed  # after a person read the review list (void if the bytes change)
python -m kimeru dataset split DIR                        # deterministic tune / eval split, checks, data version
python eval/dataset_eval.py run --dataset SPLIT_DIR --backend kev
python eval/dataset_eval.py status --dataset SPLIT_DIR --result SPLIT_DIR/result-kev.json
```

- The anonymization output is a **candidate** (`status: candidate, human_review_required`), never "anonymized": secrets and
  proper nouns written in free text can remain. `review_PRIVATE.txt` lists every replacement with the original value: keep
  it on the PC and delete it after the review.
- `split` excludes, with a reason per row id: missing fields, rows the current graphs cannot score, exact duplicates, one
  event labeled two ways. Near-duplicates (normalized text) go to the same side; `kimeru dataset check TUNE EVAL` finds
  leakage between files split some other way.
- The result records the data version, the graph fingerprints, the model identity and the scoring method. A result that
  does not match the dataset, graphs, model or scoring is `PROVENANCE_MISMATCH` and shows no numbers.
- Six categories are kept apart: correct / human_confirmation / safe_fallback / wrong_decision / critical_miss /
  undeterminable. `e2e.py` now prints severe misses apart from the other wrong decisions.
- No dataset or no rows is `NO_INPUT`; no result is `NOT_RUN`. Neither is shown as 0%. `--backend stub` is a
  `PIPELINE_CHECK` (the path works), not an accuracy.
- Jev: the result file is marked not shareable and no counts are printed or summarized.
- Results must use a new file outside the repository; existing inputs and results are never overwritten.
  Use `--out NEW_RESULT` for a rerun. `NO_INPUT` returns exit code 1. Missing manifest fields, changed files,
  and inconsistent row counts are `PROVENANCE_MISMATCH`, including before a result exists.
- Use `dataset_eval.py status` to compare against the current graphs and scoring implementation.
  `kimeru dataset status` checks dataset/result provenance and current graphs, without loading the current scorer;
  it explicitly reports scoring as unchecked and cannot mark the roadmap DONE.

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
python eval/e2e.py --backend kev --fixtures fixtures_requirements_holdout.jsonl   # 調整に使っていないと記録した 10 件（作った日に 1 回測っただけ）
```

要件の検討 6 件と、「要件」を含むが別の扱いになる 4 件（判断・進捗・共有・障害）です。

## 文面の質（`draft_quality.py`）

> 注意: writer（GitHub Copilot など）を実際に呼びます。12 イベントで約 15 回です。契約の月間の上限を使うので、回す前に残りを確認してください。

```bash
python eval/draft_quality.py --backend stub --writer copilot --per-kind 3 --show
python eval/draft_quality.py --fixtures fixtures_holdout.jsonl --per-kind 2
```

下書きごとに、日本語・英語の混入・定型文の複製・不自然な言い回し・材料にない権限者や完了・具体性・材料にない日付や数値を数えます（判定は LLM ではなく決まった規則なので、プロンプトの変更の前後で比べられます）。`--show` で、定型文と下書きを並べて読めます。最終的な確認は、読んで行ってください。

### 文脈の効果と事実照合率を測る（K01）

文脈を writer に渡す設定（`writer_precedent` など）を既定にするかは、数値で決めます。`--settings` は設定の組ごとに同じ fixtures を回し、その実行の間だけ環境変数に置いて、終わったら戻します（繰り返し可、`a=b,c=d` で複数の設定を 1 組に）。

```bash
# writer を明示する（省くと既定の copilot を実際に呼ぶ）。固定の台本 writer なら外部へは何も送らない
export KIMERU_WRITER_CMD="python /絶対パス/eval/scripted_writer.py"
python eval/draft_quality.py --backend stub --writer cmd --fixtures fixtures_context.jsonl --kinds teams.chat --per-kind 12     --settings writer_precedent=0 --settings writer_precedent=1 --out q.jsonl
python eval/revision_rate.py --out <状態フォルダ>     # PM の修正率を writer ごとに（件数と率だけ。本文・指示文は出さない）
```

1 下書きごとの記録（`--out` の JSONL）は、`grounded_rate`（下書きの日付・数値・人名のうち材料にある割合。同じ語は 1 つと数え、語が 0 なら null）、`tokens_total`、`tokens_unverified`、`flags`、`thread_given` / `precedent_given` / `style_given` / `today_given`（依頼文にそのブロックがあったか）です。`fixtures_context.jsonl` の 5 件目以降は `precedent`（前例の行）と `expect_facts`（返信に出てよい事実）を持ち、`expect_facts` にない語は `invented_vs_expect` に入ります（材料との照合より厳しい正解）。`revision_rate.py` の分子は `approvals.log.jsonl` の `修正` の行（`autorun.REPLY_NAMES`）、分母は `approvals.json` の `drafted_by` 付きの件数です。

固定の台本 writer（`eval/scripted_writer.py`。依頼の中身を見ず、毎回同じ 1 文を返す）で回した結果です（2026-10-24、起点 f84bb0c、stub backend、`teams.chat` の 12 件のうち下書きが出た 11 件）。**これは依頼文の組み立ての確認で、文脈が文面を良くするかの結果ではありません。** 台本の文は日付・数値・人名を含まないので `grounded_rate` は出ず（-）、設定による差は `precedent_given` にだけ出ます。

| 設定の組 | 種別 | 良い/全体 | grounded_rate | invented | precedent_given | thread_given |
|---|---|---|---|---|---|---|
| `writer_precedent=0` | teams.reply | 10/11 | - | 0 | 0/11 | 11/11 |
| `writer_precedent=0` | ado.create | 10/10 | - | 0 | 0/10 | 10/10 |
| `writer_precedent=0` | memo | 11/11 | - | 0 | - | - |
| `writer_precedent=1` | teams.reply | 10/11 | - | 0 | 7/11 | 11/11 |
| `writer_precedent=1` | ado.create | 10/10 | - | 0 | 6/10 | 10/10 |
| `writer_precedent=1` | memo | 11/11 | - | 0 | - | - |

`precedent_given` が設定 1 でも全部にならないのは、元の 4 件（ctx-1〜4）に `precedent` がなく、ctx-10（共有だけの件）に下書きが出ないためです。実際の writer（`--writer copilot` など）での数値は NOT_RUN です（契約の上限を使うので、回す前に残りを確認してください）。

## PM の 1 日（`day_run.py`）

```bash
python eval/day_run.py --backend stub                   # 文面は定型文（writer を呼ばない）
python eval/day_run.py --backend stub --writer copilot  # GitHub Copilot CLI を実際に呼ぶ（上限を使う）
```

`--writer` の既定は空で、定型文（テンプレート）で書きます。外部の CLI を呼ぶのは `--writer copilot` などと明示したときだけです。

## 期待経路（`eval/golden.json`）

`python eval/golden.py` は、ラベルだけで各 fixture がどこへ行くかを `eval/golden.json` と比べ、動いた件を `id: 前 -> 後` で出します（モデルは呼びません）。
グラフを意図して変えたら、差分を読んでから `python eval/golden.py --write` で書き直し、`eval/golden.json` も一緒に commit してください。
`tests/test_golden_paths.py` は、書き直さずに変えたときに落ちます。
