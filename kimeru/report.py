"""`kimeru report`: one self-contained HTML page from out/decisions.jsonl (+ approvals).

No external assets, works offline, light and dark. Shows totals, then every event with
its judgment path (rule / model / playbook chips), the plan, and what was executed or
sent to a person.
"""
import html
import json
import time
from datetime import datetime
from pathlib import Path
from . import trial

KIND = {"match": ("規則", "rule"), "judge": ("判断", "model"), "plan": ("進め方", "plan")}
SRC = {"teams.chat": "Teams", "monitor.alert": "アラート", "ado.workitem.created": "ADO", "meeting.item": "議事録"}
ACTION = {"teams.reply": "Teams に返信", "teams.post": "Teams に投稿", "ado.comment": "ADO にコメント",
          "ado.update": "ADO を更新", "ado.create": "ADO に起票", "oncall.page": "当番を呼び出し", "log.only": "記録のみ"}

CSS = """
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1b;--mute:#6b6b66;--line:#e4e3de;
--rule:#b42318;--rule-bg:#fdecea;--model:#1f4e8c;--model-bg:#e8f0fb;--plan:#6b4bb8;--plan-bg:#efeafb;
--ok:#1a7f4b;--ok-bg:#e6f4ec;--safe:#9a6700;--safe-bg:#fdf3dc;--human:#b42318;--human-bg:#fdecea}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#1f1f1d;--ink:#ecebe6;--mute:#a09f98;--line:#33322e;
--rule:#ff8a80;--rule-bg:#3a1f1c;--model:#8ab4f8;--model-bg:#1c2a3d;--plan:#c3b0f5;--plan-bg:#2a2340;
--ok:#6fd49c;--ok-bg:#16301f;--safe:#f0c35a;--safe-bg:#342a12;--human:#ff8a80;--human-bg:#3a1f1c}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 system-ui,"Segoe UI","Yu Gothic UI",sans-serif}
main{max-width:980px;margin:0 auto;padding:28px 16px 60px}h1{font-size:22px;margin:0 0 4px}.sub{color:var(--mute);margin:0 0 20px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:24px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.tile b{display:block;font-size:26px;font-variant-numeric:tabular-nums}.tile span{color:var(--mute);font-size:13px}
.ev{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:10px;border-left:4px solid var(--line)}
.ev.ok{border-left-color:var(--ok)}.ev.safe{border-left-color:var(--safe)}.ev.human{border-left-color:var(--human)}
.head{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}.src{font-size:12px;color:var(--mute);border:1px solid var(--line);border-radius:4px;padding:0 6px}
.sum{font-weight:600}.path{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}
.chip{font-size:12px;border-radius:999px;padding:2px 9px;white-space:nowrap}
.chip.rule{color:var(--rule);background:var(--rule-bg)}.chip.model{color:var(--model);background:var(--model-bg)}.chip.plan{color:var(--plan);background:var(--plan-bg)}
.out{font-size:14px}.out li{margin:2px 0}.badge{font-size:12px;font-weight:600;border-radius:4px;padding:1px 7px}
.badge.ok{color:var(--ok);background:var(--ok-bg)}.badge.safe{color:var(--safe);background:var(--safe-bg)}.badge.human{color:var(--human);background:var(--human-bg)}
.plan{font-size:13px;color:var(--mute);margin:4px 0 0}ol{margin:4px 0 0 18px;padding:0}ul{margin:4px 0 0 18px;padding:0}
.legend{display:flex;gap:10px;flex-wrap:wrap;margin:-8px 0 18px;font-size:13px;color:var(--mute)}
"""


def _rows(p):
    p = Path(p)
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.exists() else []


def _e(s):
    return html.escape(str(s if s is not None else ""))


def _observed_time(value):
    parsed = trial._timestamp(value)
    return value if parsed is not None and 0 <= parsed <= time.time() else "Unknown"


def _action(a):
    label = ACTION.get(a.get("type"), a.get("type"))
    d = a.get("title") or a.get("text") or a.get("summary") or ""
    if a.get("fields"):
        d = ", ".join(f"{k.split('.')[-1]}={v}" for k, v in a["fields"].items())
    return f"{label}: {d}" if d else label


def _chip(s, kinds):
    kind = kinds.get(s["node"], "judge")
    label, cls = KIND.get(kind, ("判断", "model"))
    a, edge = s.get("answer") or {}, s["edge"]
    if kind == "match":
        v = "該当" if edge == "yes" else "該当なし"
    elif kind == "plan":
        v = a.get("playbook") if edge == "ok" else "決めきれない"
    elif "choice" in a:
        v = f"{a['choice']} {a.get('confidence', 0):.2f}"
    elif "noul" in a:
        v = f"はい {a['noul']:.2f}"
    elif "score" in a:
        v = f"{a['score']:.2f}"
    else:
        v = edge
    if edge == "unsure":
        v += " → 確信なし"
    return f'<span class="chip {cls}" title="{_e(s["node"])}">{label}: {_e(v)}</span>'


def _metadata(value):
    return value if isinstance(value, dict) else {}


def _verified_times(item, timing, reference):
    now = time.time()
    valid = (trial.measurement_valid(item, timing, reference) and not item.get("delivery_unknown")
             and timing.get("posting_source") == "bridge_readback")
    posted = (_observed_time(timing.get("posted_at")) if valid and
              trial._interval(timing, "ready_at", "posted_at", now) is not None else "Unknown")
    approved = (_observed_time(timing.get("approval_observed_at")) if posted != "Unknown" and
                trial._interval(timing, "posted_at", "approval_observed_at", now) is not None else "Unknown")
    return posted, approved


def _case_indexes(out, items):
    """Map decision keys and merged follow-up event ids to their stable case."""
    by_key = {}
    for number, item in items.items():
        record = item.get("record") or {}
        by_key[item.get("key")] = (number, item)
        by_key[f"{record.get('graph')}:{record.get('event_id')}:{record.get('node')}"] = (number, item)
    by_event = {}
    for merge in _rows(Path(out) / "merged.jsonl"):
        stable = merge.get("into")
        if stable in by_key:
            found = by_key[stable]
            graph = (found[1].get("record") or {}).get("graph")
            if graph:
                by_event[(graph, str(merge.get("event_id")))] = (found, merge.get("revision"))
    return by_key, by_event


def _current_case(r, by_key, by_event):
    key = f"{r.get('graph')}:{r.get('event_id')}:{r.get('node')}"
    alias = by_event.get((r.get("graph"), str(r.get("event_id"))))
    found = (by_key.get(key) or by_key.get(_metadata(r.get("measurement")).get("case_key"))
             or (alias[0] if alias else None))
    if not found:
        return None, False
    _, item = found
    record = item.get("record") or {}
    current_key = f"{record.get('graph')}:{record.get('event_id')}:{record.get('node')}"
    current_revision = int(item.get("revision") or record.get("revision") or 1)
    row_revision = r.get("revision")
    if row_revision is not None:
        same_revision = str(row_revision) == str(current_revision)
    elif alias and alias[1] is not None and str(alias[1]) == str(current_revision):
        same_revision = True  # merged.jsonl ties this event id to the current case revision
    else:
        # Reused event keys after a redraft/revision cannot prove which snapshot
        # the decision row describes, so never attach the newer case's evidence.
        same_revision = current_revision == 1
    return found, current_key == key and same_revision


def _execution_states(out, items):
    """Latest state per idempotency key, reconciled with approvals' durable copy."""
    evidence = {}
    for row in _rows(Path(out) / "executions.jsonl"):
        key = row.get("key")
        if key:
            evidence.setdefault(str(key), []).append(row)
    for number, item in items.items():
        actions = (item.get("record") or {}).get("actions") or []
        for index, state in (item.get("exec") or {}).items():
            if not isinstance(state, dict) or not state.get("key"):
                continue
            action = actions[int(index)] if str(index).isdigit() and int(index) < len(actions) else {}
            evidence.setdefault(str(state["key"]), []).append({
                **state, "id": number, "type": action.get("type"), "target": action.get("id"),
                "_approval_copy": True,
            })

    result = {}
    for key, rows in evidence.items():
        ledger = [row for row in rows if not row.get("_approval_copy")]
        ap_rows = [row for row in rows if row.get("_approval_copy")]
        latest_ledger = ledger[-1] if ledger else None
        latest_ap = ap_rows[-1] if ap_rows else None
        if latest_ledger and latest_ap and latest_ledger.get("state") != latest_ap.get("state"):
            def stamp(row):
                try:
                    return datetime.fromisoformat(row["at"]).timestamp()
                except (KeyError, TypeError, ValueError):
                    return None
            lt, at = stamp(latest_ledger), stamp(latest_ap)
            if lt is None or at is None or lt == at:
                row = {**latest_ledger, "state": "unknown", "conflict": True}
            else:
                row = latest_ledger if lt > at else latest_ap
        else:
            row = latest_ledger or latest_ap
        result[key] = row
    return result


def _delivery_note(out, ap):
    items = ap.get("items", {})
    simulated = sum(1 for item in items.values() if item.get("posted") and
                    _metadata((item.get("record") or {}).get("measurement")).get("source") == "demo")
    case_done = sum(1 for item in items.values() if item.get("posted")) - simulated
    case_unknown = sum(1 for item in items.values() if item.get("delivery_unknown"))
    notices = ap.get("notices", [])
    notice_unknown = ap.get("notice_delivery_unknown", {}) or {}
    if isinstance(notice_unknown, list):
        notice_unknown = {key: True for key in notice_unknown}
    notice_attempts = ap.get("notice_delivery_attempts", {}) or {}
    notice_unknown_count = len(set(notice_unknown) | set(notice_attempts))
    outbox_unknown = bool(ap.get("outbox_delivery_unknown") or ap.get("outbox_delivery_attempt"))
    state = json.loads((Path(out) / "daily_state.json").read_text(encoding="utf-8")) if (Path(out) / "daily_state.json").exists() else {}
    brief_unknown = bool(state.get("brief_delivery_unknown"))
    return (f"自己チャット配信確認: ケース {case_done} 件、notice {len(notices)} 件 ・ "
            f"配信結果不明: ケース {case_unknown} 件、notice {notice_unknown_count} 件、"
            f"outbox {'1' if outbox_unknown else '0'} 件、brief {'1' if brief_unknown else '0'} 件 ・ "
            f"simulated self-chat postings {simulated} (no Teams write); posting does not prove phone receipt")


def _execution_label(row):
    state = row.get("state")
    labels = {"done": "実行確認済み", "closed": "本人が確認して終了（API実行の確認ではない）",
              "skipped": "実行対象外・未実行", "failed": "実行失敗", "running": "結果不明（実行中記録）",
              "unknown": "結果不明"}
    label = labels.get(state, "結果不明")
    result = row.get("result") or {}
    if state == "done" and result.get("comment_id") is not None:
        label += f"（ADO コメント {result['comment_id']}）"
    if row.get("conflict"):
        label = "結果不明（記録が不一致）"
    return label


def _io_note(out, ap, executions):
    states = {}
    for row in executions.values():
        st = row.get("state", "unknown")
        states[st] = states.get(st, 0) + 1
    known = ", ".join(f"{_execution_label({'state': st})}: {count}" for st, count in sorted(states.items())) or "実行記録なし"
    comments = [str((row.get("result") or {}).get("comment_id")) for row in executions.values()
                if row.get("state") == "done" and (row.get("result") or {}).get("comment_id") is not None]
    if comments:
        known += " ・ ADO コメント " + ", ".join(_e(value) for value in comments)
    return (_delivery_note(out, ap) + " ・ 外部実行記録: " + known + "（記録なしは成功を意味しません） ・ "
            "External writes are record-only by default; approval or a posting count does not prove an ADO write.")


def build(out, graphs, title="kimeru 判断レポート"):
    out = Path(out)
    decisions = _rows(out / "decisions.jsonl")
    ap = json.loads((out / "approvals.json").read_text(encoding="utf-8")) if (out / "approvals.json").exists() else {"items": {}}
    by_key, by_event = _case_indexes(out, ap.get("items", {}))
    executions = _execution_states(out, ap.get("items", {}))
    kinds = {}
    for lst in graphs.values():
        for g in lst:
            kinds.update({nid: n["kind"] for nid, n in g["nodes"].items()})

    def cls(r):
        if r.get("needs_human") or r.get("outcome") == "advise":
            return "human"
        if any(kinds.get(s["node"]) == "match" and s["edge"] == "yes" for s in r["path"]):
            return "ok"   # a rule decided it; a later low-confidence step only changes detail
        return "safe" if any(s["edge"] == "unsure" for s in r["path"]) else "ok"

    counts = {"ok": 0, "safe": 0, "human": 0}
    rule = 0
    for r in decisions:
        counts[cls(r)] += 1
        rule += any(kinds.get(s["node"]) == "match" and s["edge"] == "yes" for s in r["path"])
    n = len(decisions)
    tiles = [(n, "判断した件数"), (counts["ok"], "自動で決定"), (rule, "うち規則（安全網）で即決定"),
             (counts["safe"], "確信が低く安全側で決定"), (counts["human"], "人の確認へ")]
    parts = [f"<!doctype html><html lang='ja'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>",
             f"<title>{_e(title)}</title><style>{CSS}</style></head><body><main>",
             f"<h1>{_e(title)}</h1><p class='sub'>{_e(out)} ・ {_e(_io_note(out, ap, executions))}</p>",
             "<div class='tiles'>" + "".join(f"<div class='tile'><b>{v}</b><span>{_e(l)}</span></div>" for v, l in tiles) + "</div>",
             "<div class='legend'><span class='chip rule'>規則</span><span class='chip model'>判断（モデル）</span><span class='chip plan'>進め方</span>"
             "<span class='badge ok'>自動</span><span class='badge safe'>安全側</span><span class='badge human'>人の確認</span></div>"]
    evidence = trial.collect(out)
    parts.append("<section class='ev'><h2>Measurement coverage</h2><pre style='white-space:pre-wrap'>" +
                 _e(trial.render(evidence)) + "</pre><p class='plan'>Trace: decisions.jsonl (ready/runtime/provenance), "
                 "approvals.json and approvals.log.jsonl (proposal posting/accepted OK), trial.json (explicit observations). "
                 "Unknown is missing evidence; no business efficiency gain is established.</p></section>")
    for number, item in ap["items"].items():
        timing = _metadata(item.get("measurement"))
        reference = _metadata(item.get("record", {}).get("measurement"))
        posted, approved = _verified_times(item, timing, reference)
        source = "synthetic demo" if timing.get("source") == "demo" else "local/legacy"
        parts.append("<p class='plan'>" + _e(f"Current proposal #{number}: generation {timing.get('generation', 'Unknown')}; "
                     f"{source}; ready {_observed_time(timing.get('ready_at'))}; "
                     f"verified post {posted}; accepted OK observation "
                     f"{approved}. Evidence: approvals.json; "
                     "source decision/proposal preparation: decisions.jsonl.") + "</p>")
    for r in decisions:
        c = cls(r)
        badge = {"ok": "自動", "safe": "安全側", "human": "人の確認"}[c]
        timing = _metadata(r.get("measurement"))
        found, current = _current_case(r, by_key, by_event)
        ap_note = ""
        current_item = None
        if found:
            num, item = found
            if not current:
                ap_note = f" ・ 履歴: ケース #{_e(num)} の改訂 {_e(item.get('revision', 1))} で置換済み"
            else:
                current_item = item
                st = item.get("status", "Unknown")
                ap_note = (f" ・ ケース #{_e(num)} / 改訂 {_e(item.get('revision', 1))}: "
                           f"{_e({'approved': '承認', 'rejected': '却下', 'held': '保留', 'pending': '返信待ち'}.get(st, st))}")
                if st == "approved":
                    progress = item.get("work") if isinstance(item.get("work"), dict) else {}
                    state = progress.get("state") or "Unknown"
                    ap_note += f" ・ 作業進捗: {_e(state)}"
        parts.append(f"<section class='ev {c}'><div class='head'><span class='src'>{_e(SRC.get(r.get('event_kind'), r.get('event_kind')))}</span>"
                     f"<span class='sum'>{_e(r.get('summary') or r.get('event_id'))}</span></div>")
        parts.append("<div class='path'>" + "".join(_chip(s, kinds) for s in r["path"]) + "</div>")
        generation = timing.get("generation")
        source = "synthetic demo" if timing.get("source") == "demo" else "local/legacy (not business-effect evidence)"
        matched = found[1] if found else {}
        history = matched.get("measurement_history")
        history = history if isinstance(history, list) else []
        observations = [_metadata(matched.get("measurement")), *history]
        observed = next((m for m in observations if trial.measurement_valid(matched, m, timing)), {})
        posted, approved = _verified_times(matched, observed, timing)
        if generation is not None and generation != _metadata(matched.get("measurement")).get("generation") and matched:
            parts.append("<p class='plan'>superseded proposal: case status refers to the current proposal, "
                         "not approval of this older generation.</p>")
        parts.append("<p class='plan'>" + _e(f"Measurement: {source}; generation {generation if generation is not None else 'Unknown'}; "
                     f"ready {_observed_time(timing.get('ready_at'))}; positively observed post "
                     f"{posted}; "
                     f"accepted OK observed {approved} (includes polling).") + "</p>")
        if r.get("plan"):
            steps = "".join(f"<li>{_e(s['title'])}（{_e(s['due'])}）</li>" for s in r["plan"]["steps"])
            parts.append(f"<div class='plan'>進め方: {_e(r['plan']['title'])}<ol>{steps}</ol></div>")
        acts = r.get("actions") or []
        exec_rows = []
        if current_item:
            current_record = current_item.get("record") or {}
            for index, action in enumerate(current_record.get("actions") or []):
                state = (current_item.get("exec") or {}).get(str(index)) or {}
                exec_key = state.get("key")
                if not exec_key and current_item.get("key"):
                    from . import execute
                    exec_key = execute.idem_key(current_item["key"], index, action)
                evidence = executions.get(str(exec_key)) if exec_key else None
                if evidence:
                    exec_rows.append((action, _execution_label(evidence)))
                elif action:
                    label = "実行対象外・未実行" if action.get("exec_skip") else "予定（dry-run／実行記録との対応は未確認）"
                    exec_rows.append((action, label))
        elif acts and found and not current:
            acts = []  # a superseded decision does not inherit its successor's execution state
        verb = "承認後の計画（外部書き込みは既定で記録のみ）" if c == "human" else "記録された計画"
        parts.append(f"<div class='out'><span class='badge {c}'>{badge}</span>{ap_note}"
                     + (f"<ul>{''.join('<li>' + _e(verb) + ' — ' + _e(_action(a)) + '</li>' for a in acts)}</ul>" if acts else "")
                     + (f"<ul>{''.join('<li>' + _e(label) + ' — ' + _e(_action(action)) + '</li>' for action, label in exec_rows)}</ul>" if exec_rows else "")
                     + (f"<p class='plan'>メモ: {_e(r['advice'])}</p>" if r.get("advice") else "") + "</div></section>")
    parts.append("</main></body></html>")
    return "".join(parts)
