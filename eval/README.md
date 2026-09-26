# Question-quality eval

`fixtures.jsonl` holds synthetic, labeled events for every shipped graph (36 events).
Each fixture labels some judge / plan nodes; all labeled questions for one event go to Jev in one request.

```bash
python eval/run_eval.py live --out answers.jsonl      # needs TYPESAFE_API_KEY
python eval/run_eval.py dump > requests.jsonl         # or run requests elsewhere (e.g. an MCP client)
python eval/run_eval.py score answers.jsonl
```

The report lists per-node `ok/n` and how many routed to `unsure` (a safe miss: the event goes to a human).

**Do not publish Jev results.** TypeSafe's terms forbid publishing Jev benchmark/performance data;
keep `answers*.jsonl` and scores out of this repository, issues and public CI logs.

## Per-backend thresholds

Node thresholds were tuned on Jev. A backend with a different confidence scale gets a profile in
`kimeru/profiles.py` (two global knobs: `conf_scale`, `noul_scale`). Tune on recorded answers — no
new model calls:

```bash
python eval/run_eval.py live --backend kev --out answers_kev.jsonl
python eval/run_eval.py tune answers_kev.jsonl           # grid search, never adds confidently-wrong decisions
python eval/run_eval.py score answers_kev.jsonl --profile kev
```

Kev-4B (local, published numbers are fine for Kev): untuned 52/81 decided correctly, 26 sent to a human,
3 confidently wrong; with the `kev` profile (0.8 / 0.75) 59 / 19 / 3.

## Question-writing rules learned from this eval

- One condition per `noul`. "A and (B or C)" questions under-fire; split them into chained nodes.
- Ordered answers (priority, severity) are `score` nodes with bands, not `choice`: adjacent-level splits
  are normal and the expected value is still meaningful, so `min_conf` can be low.
- Never reference a state field that does not exist (e.g. `` `board` `` when the state has `text`).
- Do not put your own verdict into the state (e.g. a "（今日）" due label next to an item you ask to rank);
  Jev anchors on it.
- When a label disagrees with Jev, re-read the question literally before "fixing" the question — the label may be wrong.
