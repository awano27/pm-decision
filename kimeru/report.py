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
from . import learn, trial
from .writer import FIELD, writer_label

KIND = {"match": ("規則", "rule"), "judge": ("判断", "model"), "plan": ("進め方", "plan")}
SRC = {"teams.chat": "Teams", "monitor.alert": "アラート", "ado.workitem.created": "ADO", "meeting.item": "議事録"}
ACTION = {"teams.reply": "Teams に返信", "teams.post": "Teams に投稿", "ado.comment": "ADO にコメント",
          "ado.update": "ADO を更新", "ado.create": "ADO に起票", "oncall.page": "当番を呼び出し", "log.only": "記録のみ"}

CSS = """
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1b;--mute:#6b6b66;--line:#e4e3de;
--rule:#b42318;--rule-bg:#fdecea;--model:#1f4e8c;--model-bg:#e8f0fb;--plan:#6342ad;--plan-bg:#efeafb;
--ok:#16703f;--ok-bg:#e6f4ec;--safe:#7f5500;--safe-bg:#fdf3dc;--human:#b42318;--human-bg:#fdecea}
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
h2{font-size:18px;margin:28px 0 10px}h3{font-size:15px;margin:0 0 6px}p,li,blockquote{overflow-wrap:anywhere}
.lead{margin:0 0 16px;max-width:46em}.notice{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--safe);
border-radius:10px;padding:12px 16px;margin:0 0 20px;font-size:14px}.notice b{display:block;margin-bottom:2px}
.case{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin-bottom:16px}
.case>h3{font-size:16px}
.flow{list-style:none;margin:14px 0 0;padding:0;counter-reset:f}
.flow>li{position:relative;padding:0 0 18px 28px;margin:0 0 0 13px;border-left:2px solid var(--line)}
.flow>li:last-child{border-left-color:transparent;padding-bottom:0}
.flow>li::before{counter-increment:f;content:counter(f);position:absolute;left:-14px;top:-2px;width:26px;height:26px;border-radius:50%;
background:var(--card);border:2px solid var(--ink);font-weight:700;font-size:13px;line-height:22px;text-align:center}
.flow h3 span{color:var(--mute);font-weight:400;font-size:13px;margin-left:8px}
.flow p{margin:6px 0 2px}
blockquote{margin:4px 0;padding:8px 12px;border-left:3px solid var(--line);background:var(--bg);border-radius:4px}
.who{font-size:13px;color:var(--mute)}.warn{color:var(--safe);font-size:13px}
.st{display:inline-block;border:1px solid var(--ink);border-radius:6px;padding:2px 8px;margin:2px 6px 4px 0;font-size:13px;background:var(--card)}
.st b{font-weight:700;margin-right:2px}.st.wait{border-style:dashed}.note{font-size:13px;color:var(--mute);margin:6px 0 0}
details{margin:28px 0 0;border-top:1px solid var(--line);padding-top:8px}summary{cursor:pointer;color:var(--mute);font-size:14px;padding:6px 0}
:focus-visible{outline:3px solid var(--model);outline-offset:2px}
.empty{background:var(--card);border:1px dashed var(--line);border-radius:10px;padding:18px}
@media (max-width:560px){main{padding:18px 12px 40px}.tiles{grid-template-columns:repeat(2,1fr)}.flow>li{padding-left:22px}.case{padding:14px 12px}.chip{white-space:normal}.st{display:block;width:fit-content;max-width:100%}}
h1,h3,.sum,.head,.tile span{overflow-wrap:anywhere}
"""


SKIPPED = {}   # file name -> lines that could not be read in the last build (shown at the top of the page)


def _rows(p):
    """The JSON lines of a record file; a line that cannot be read is skipped and counted (a half-written last line, a hand edit)."""
    p = Path(p)
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            row = None
        if isinstance(row, dict):
            rows.append(row)
        else:
            SKIPPED[p.name] = SKIPPED.get(p.name, 0) + 1
    return rows


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
              "unknown": "結果不明", "requested": "送信待ち（次の --send の実行で 1 回送る）"}
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


DECISION = {"pending": ("⏳", "確認待ち（まだ決めていない）"), "held": ("⏸", "保留"), "approved": ("✔", "承認済み"),
            "rejected": ("✖", "却下")}
WORK = {"approved": "未着手", "in_progress": "作業中", "done": "作業完了", "blocked": "停止中"}
TRY_NOTICE = ("体験モードの記録です。判断はキーワード一致の簡易ルールです。要点・足りない情報・選択肢・返信案と、確認の返信（詳細・修正・OK）は"
              "台本の固定文で、AI モデルの推論でも、あなたの操作でもありません。"
              "経路に出る 0.85・0.90 などの値は簡易ルールの固定値で、確率や正解率ではありません。Teams・ADO・AI モデルには接続しておらず、"
              "外へは何も送っていません。")
LOGGED = {"redrafted": "修正を指示 → 返信案を書き直し", "redraft_failed": "修正を指示 → 書き直せず（元の案のまま）",
          "approved": "承認", "rejected": "却下", "held": "保留", "ask_back": "聞き返しを指示", "detail": "全文（詳細）を確認",
          "closed": "済（本人が確認して閉じた）", "send": "送信を指示"}
# how the case flow names the judgment points and answers of the shipped graphs (other ids are shown as they are)
NODE = {"critical_incident": "重大障害の規則", "ack_only": "お礼・了解だけの規則", "intent": "依頼の種類", "urgency": "急ぎ度",
        "plan_decision": "進め方の型", "plan_requirements": "進め方の型", "critical_outage": "重大障害の規則",
        "critical_bug": "重大バグの規則", "ready": "着手できるか", "priority": "優先度", "item_type": "行の種類",
        "label_decision": "「決定」の印", "label_task": "「タスク」の印", "label_risk": "「リスク」の印", "label_info": "「共有」の印",
        "has_owner": "担当者がいるか", "has_due": "期限があるか"}
CHOICE = {"decision": "判断の依頼", "requirements": "要件の検討", "status": "状況の確認", "incident": "障害", "fyi": "共有",
          "none": "どれでもない", "action": "タスク", "risk": "リスク", "info": "共有"}
CHOICE_OF = {"item_type": {"decision": "決定", "action": "タスク", "risk": "リスク", "info": "共有"}}   # a value that means something else there


def _chip_ja(s, kinds):
    kind = kinds.get(s["node"], "judge")
    label, cls = KIND.get(kind, ("判断", "model"))
    a, edge = s.get("answer") or {}, s["edge"]
    name = NODE.get(s["node"], s["node"])
    if kind == "match":
        v = "該当" if edge == "yes" else "該当なし"
    elif kind == "plan":
        v = a.get("playbook") if edge == "ok" else "決めきれない"
    elif "choice" in a:
        v = f"{CHOICE_OF.get(s['node'], CHOICE).get(a['choice'], a['choice'])}（{_num(a.get('confidence', 0))}）"
    elif "noul" in a:
        v = f"はい {_num(a['noul'])}"
    elif "score" in a:
        v = _num(a["score"])
    else:
        v = edge
    if edge == "unsure":
        v += " → 確信なし・人へ"
    head = f"{label}・{name}" if s["node"] in NODE else label
    return f'<span class="chip {cls}" title="{_e(s["node"])}">{_e(head)}: {_e(v)}</span>'


def _timeline(log, number, item):
    """The PM's side of case #number, in order: each post of a proposal, and what the PM answered to it (`log`: the rows of
    approvals.log.jsonl, read once for the page)."""
    steps = []
    for m in [*(item.get("measurement_history") or []), _metadata(item.get("measurement"))]:
        if isinstance(m, dict) and m.get("posted_at"):
            gen = _int(m.get("generation"))
            steps.append(((gen, 0), m["posted_at"], f"確認を依頼（自分とのチャットへ投稿・案 {gen}）"))
    for row in log:
        if str(row.get("id")) != str(number):
            continue
        gen = _int(_metadata(row.get("measurement")).get("generation"))
        st = row.get("status")
        text = LOGGED.get(st, str(st))
        if st == "redrafted" and row.get("instruction_len"):
            text += f"（指示は {row['instruction_len']} 文字。指示の本文は記録しない）"
        steps.append(((gen, -1) if st == "redrafted" else (gen, 1), row.get("at", ""), text))
    steps.sort(key=lambda s: s[0])   # a rewrite makes proposal N+1, which is posted after it; an answer follows its post
    return [(at, text) for _, at, text in steps]


def _num(value, fmt="{:.2f}"):
    """A judge's number as text; a broken one is shown as — instead of stopping the page."""
    try:
        return fmt.format(float(value))
    except (TypeError, ValueError):
        return "—"


def _int(value, default=1):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clock(value):
    parsed = trial._timestamp(value)
    return datetime.fromtimestamp(parsed).strftime("%m/%d %H:%M") if parsed is not None else "時刻不明"


NOT_RUN = {"pending": "承認待ちのため", "held": "保留中のため", "rejected": "却下したため"}


def _reply_state(item, index, executions):
    """(mark, words, css class) for the reply to the sender at `index` of the case: an execution record, else the case's status,
    else the PM's own `送った N`; never the approval alone. The words have no "相手への返信:" in front."""
    own = (item.get("exec") or {}).get(str(index)) or {}
    row = executions.get(str(own.get("key"))) if own.get("key") else None
    state = (row or own).get("state")
    if (row or {}).get("conflict") or state in ("running", "unknown"):
        return ("?", "送信結果不明（自動では送り直さない）", "")
    known = {"done": ("✉", "送信済み（実行記録あり）", ""), "failed": ("!", "送信失敗", ""),
             "requested": ("…", "送信待ち（次の --send の実行で 1 回送る）", "wait"),
             "closed": ("✔", "本人が確認して終了（送信の記録ではない）", ""), "skipped": ("—", "対象外のため送っていない", "")}
    if state in known:
        return known[state]
    status = item.get("status")
    if status in NOT_RUN:
        return ("—", NOT_RUN[status] + "送っていない", "" if status == "rejected" else "wait")
    if status != "approved":
        return ("?", "判断の状態が不明のため送っていない", "")
    if item.get("sent_confirmed"):
        return ("✔", "本人が「送った」と記録（kimeru は送っていない）", "")
    return ("—", "送っていない（記録だけ）", "wait")


def _sent_state(item, executions):
    for index, action in enumerate((item.get("record") or {}).get("actions") or []):
        if action.get("type") == "teams.reply":
            mark, words, cls = _reply_state(item, index, executions)
            return (mark, "相手への返信: " + words, cls)
    return None


def _drafted_texts(actions):
    """The texts a rewrite (修正 N, 聞き返し N) can change, in order: what tells a rewritten case from one that was only posted again."""
    return [(a.get("type"), a.get(FIELD[a["type"]])) for a in actions or [] if isinstance(a, dict) and a.get("type") in FIELD]


def _post_state(item):
    if item.get("delivery_unknown"):
        return ("?", "自分とのチャットへの投稿: 配信結果不明", "")
    if not item.get("posted"):
        return ("—", "自分とのチャットへの投稿: まだ", "wait")
    if _metadata(item.get("measurement")).get("posting_source") == "closed_not_delivered":
        return ("✖", "自分とのチャットへの投稿: 届いたと確かめられないまま閉じた", "")
    if _metadata((item.get("record") or {}).get("measurement")).get("source") == "demo":
        return ("◇", "自分とのチャットへの投稿: 模擬（実際の Teams には書いていない）", "")
    return ("✔", "自分とのチャットへの投稿: 読み戻しで確認済み", "")


def _flow(log, number, item, kinds, executions, mode):
    """One case on one screen: the request, what kimeru read, the proposal, the PM's check, the record."""
    record = item.get("record") or {}
    event = record.get("event") or {}
    memo = record.get("memo") or {}
    who = event.get("author") or event.get("created_by") or ""
    src = SRC.get(record.get("event_kind"), record.get("event_kind") or "")
    body = event.get("text") or event.get("item") or event.get("title") or record.get("summary") or ""
    parts = [f"<section class='case'><h3>#{_e(number)} {_e(record.get('summary') or body)}</h3><ol class='flow'>",
             f"<li><h3>依頼<span>{_e(src)}{' ・ ' + _e(who) if who else ''}</span></h3><blockquote>{_e(body)}</blockquote></li>"]
    chips = "".join(_chip_ja(s, kinds) for s in record.get("path") or [] if isinstance(s, dict) and "node" in s and "edge" in s)
    missing = "".join(f"<li>{_e(m)}</li>" for m in memo.get("missing") or [])
    parts.append(f"<li><h3>根拠<span>{'台本の要点と簡易ルールの経路（AI ではない）' if mode == 'try' else 'kimeru が読み取ったこと'}</span></h3>"
                 + (f"<p>{_e(memo['summary'])}</p>" if memo.get("summary") else "")
                 + f"<div class='path'>{chips}</div>"
                 + (f"<p><b>足りない情報</b></p><ul>{missing}</ul>" if missing else "")
                 + ("<p class='note'>経路の値は簡易ルールの固定値です（確率ではありません）。</p>"
                    if mode == "try" or (record.get("judge") or {}).get("name") == "StubBackend" else
                    "<p class='note'>経路の値は判断モデルの確信度で、人へ回すかを決めるしきい値に使います（正解の確率ではありません）。</p>"
                    if chips else "") + "</li>")
    plan = record.get("plan") or {}
    steps = "".join(f"<li>{_e(st['title'])}（{_e(st.get('due', ''))}）</li>" for st in plan.get("steps") or [])
    drafts = []
    for a in record.get("actions") or []:
        if a.get("type") == "teams.reply":
            warn = f"<p class='warn'>⚠ 元の材料に無い日付・数値: {_e('、'.join(a['unverified']))}</p>" if a.get("unverified") else ""
            by = f"（{_e(writer_label(record))}）" if a.get("drafted_by") else "（定型文）"
            drafts.append(f"<p><b>返信案</b> <span class='who'>{by}</span></p><blockquote>{_e(a.get('text', ''))}</blockquote>{warn}")
    later = "".join(f"<li>{_e(_action(a))}</li>" for a in record.get("actions") or [] if a.get("type") != "teams.reply")
    parts.append("<li><h3>提案<span>承認されるまで何も実行しない</span></h3>"
                 + (f"<p><b>進め方の案</b>: {_e(plan.get('title', ''))}</p><ol>{steps}</ol>" if steps else "")
                 + (f"<p><b>メモ</b>: {_e(record['advice'])}</p>" if record.get("advice") and not steps else "")
                 + "".join(drafts)
                 + (f"<p><b>承認後の計画</b>（既定は記録だけ。ADO・Teams には書かない）</p><ul>{later}</ul>" if later else "") + "</li>")
    options = "".join(f"<li>{_e(o)}</li>" for o in memo.get("options") or [])
    history = "".join(f"<li><span class='who'>{_e(_clock(at))}</span> {_e(text)}</li>" for at, text in _timeline(log, number, item))
    parts.append("<li><h3>人の確認<span>決めるのはあなた</span></h3>"
                 + (f"<p><b>人が決めること</b></p><ul>{options}</ul>" if options else "")
                 + (f"<p><b>確認の履歴</b></p><ul>{history}</ul>" if history else "<p>まだ確認していません。</p>") + "</li>")
    mark, label = DECISION.get(item.get("status"), ("?", f"状態不明（{item.get('status')}）"))
    badges = [(mark, f"判断: {label}", "" if item.get("status") in ("approved", "rejected") else "wait")]
    if item.get("status") == "approved":
        work = (item.get("work") or {}).get("state") if isinstance(item.get("work"), dict) else None
        none = "進捗の記録なし" + ("" if mode == "try" else "（work set で記録）")
        label = WORK.get(work, f"状態不明（{work}）") if work else none
        badges.append(("✔" if work == "done" else "○", f"作業: {label}",
                       "" if work == "done" else "wait"))
    sent = _sent_state(item, executions)
    if sent:
        badges.append(sent)
    badges.append(_post_state(item))
    status = "".join(f"<span class='st {cls}'><b>{_e(m)}</b>{_e(t)}</span>" for m, t, cls in badges)
    parts.append(f"<li><h3>記録</h3><div>{status}</div>"
                 "<p class='note'>判断・作業・相手への送信は別々に記録します。承認しただけでは、送信も作業の完了もしたことになりません。"
                 "判断の経路は decisions.jsonl、修正と承認の履歴は approvals.log.jsonl にあります。</p></li></ol></section>")
    return "".join(parts)


def _approvals(out):
    """approvals.json, else its .bak (as notify reads it), else nothing; a broken file is named at the top of the page."""
    for name in ("approvals.json", "approvals.json.bak"):
        p = Path(out) / name
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            SKIPPED[name] = SKIPPED.get(name, 0) + 1
            continue
        if isinstance(data, dict) and isinstance(data.get("items", {}), dict):
            items = {k: v for k, v in data.get("items", {}).items()
                     if isinstance(v, dict) and isinstance(v.get("record", {}), dict)}
            if len(items) < len(data.get("items", {})):
                SKIPPED[name] = SKIPPED.get(name, 0) + len(data["items"]) - len(items)
            return {**data, "items": items}
        SKIPPED[name] = SKIPPED.get(name, 0) + 1
    return {"items": {}}


def build(out, graphs, title="kimeru 判断レポート", mode=None):
    """The page. mode="try": the page of `kimeru try` (says that the judge is the keyword stub and the texts a script)."""
    out = Path(out)
    SKIPPED.clear()
    decisions = []
    for r in _rows(out / "decisions.jsonl"):
        if isinstance(r.get("path"), list) and all(isinstance(x, dict) and "node" in x and "edge" in x for x in r["path"]):
            decisions.append(r)
        else:
            SKIPPED["decisions.jsonl"] = SKIPPED.get("decisions.jsonl", 0) + 1
    log = _rows(out / "approvals.log.jsonl")
    ap = _approvals(out)
    ap = {**ap, "items": learn.cases(ap.get("items", {}))}   # R21: the cards are no case of the report
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
             f"<h1>{_e(title)}</h1><p class='sub'>記録フォルダ: {_e(out)}</p>",
             "<p class='lead'>あなたの確認に回った依頼を、<b>依頼 → 根拠 → 提案 → 人の確認 → 記録</b> の順に並べています。"
             "kimeru は案を用意して記録するだけで、決めるのも相手へ送るのもあなたです。</p>"]
    if mode == "try":
        parts.append(f"<div class='notice' role='note'><b>体験モード</b>{_e(TRY_NOTICE)}</div>")
    notice_at = len(parts)   # the note on unreadable records goes here once every file has been read (see the end)
    if not decisions and not ap["items"]:
        parts.append("<p class='empty'>まだ判断の記録がありません。<code>python -m kimeru try</code> で、架空の相談 1 件の記録を作れます。</p>")
    if ap["items"]:
        parts.append("<h2>あなたの確認に回った件</h2>")
        parts += [_flow(log, number, item, kinds, executions, mode) for number, item in ap["items"].items()]
    if decisions:
        parts += ["<h2>判断したすべての件</h2>",
                  "<p class='note'>自動で決めた件も含む、判断の全記録です。書き直す前の案も、そのときの記録として残ります。</p>",
                  "<div class='tiles'>" + "".join(f"<div class='tile'><b>{v}</b><span>{_e(l)}</span></div>" for v, l in tiles) + "</div>",
                  "<div class='legend'><span class='chip rule'>規則</span><span class='chip model'>"
                  + ("判断（簡易ルール）" if mode == "try" or all((r.get("judge") or {}).get("name") == "StubBackend" for r in decisions)
                     else "判断（モデル）") + "</span><span class='chip plan'>進め方</span>"
                  "<span class='badge ok'>自動</span><span class='badge safe'>安全側</span><span class='badge human'>人の確認</span></div>"]
    try:
        coverage = trial.render(trial.collect(out))
    except (ValueError, TypeError, KeyError, AttributeError):   # trial reads the records again; one of the wrong shape ends here
        coverage = "Unknown: the records could not be read for the measurement summary"
        SKIPPED["approvals.json"] = SKIPPED.get("approvals.json", 0)
    details = ["<details><summary>計測の詳細（英語・開発者向け）</summary>",
               "<p class='plan'>" + _e(_io_note(out, ap, executions)) + "</p>",
               "<section class='ev'><h2>Measurement coverage</h2><pre style='white-space:pre-wrap'>" +
               _e(coverage) + "</pre><p class='plan'>Trace: decisions.jsonl (ready/runtime/provenance), "
               "approvals.json and approvals.log.jsonl (proposal posting/accepted OK), trial.json (explicit observations). "
               "Unknown is missing evidence; no business efficiency gain is established.</p></section>"]
    for number, item in ap["items"].items():
        timing = _metadata(item.get("measurement"))
        reference = _metadata(item.get("record", {}).get("measurement"))
        posted, approved = _verified_times(item, timing, reference)
        source = "synthetic demo" if timing.get("source") == "demo" else "local/legacy"
        details.append("<p class='plan'>" + _e(f"Current proposal #{number}: generation {timing.get('generation', 'Unknown')}; "
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
                    state = progress.get("state")
                    ap_note += f" ・ 作業進捗: {_e(WORK.get(state, f'状態不明（{state}）') if state else '記録なし')}"
        parts.append(f"<section class='ev {c}'><div class='head'><span class='src'>{_e(SRC.get(r.get('event_kind'), r.get('event_kind')))}</span>"
                     f"<span class='sum'>{_e(r.get('summary') or r.get('event_id'))}</span></div>")
        parts.append("<div class='path'>" + "".join(_chip_ja(s, kinds) for s in r["path"]) + "</div>")
        generation = timing.get("generation")
        source = "synthetic demo" if timing.get("source") == "demo" else "local/legacy (not business-effect evidence)"
        matched = found[1] if found else {}
        history = matched.get("measurement_history")
        history = history if isinstance(history, list) else []
        observations = [_metadata(matched.get("measurement")), *history]
        observed = next((m for m in observations if trial.measurement_valid(matched, m, timing)), {})
        posted, approved = _verified_times(matched, observed, timing)
        newer = bool(matched) and generation is not None and generation != _metadata(matched.get("measurement")).get("generation")
        # a newer proposal is a rewrite only when a drafted text changed (a held case shown again, or a post made again, is not)
        superseded = newer and _drafted_texts(r.get("actions")) != _drafted_texts((matched.get("record") or {}).get("actions"))
        if newer:
            details.append(f"<p class='plan'>{_e(r.get('summary') or r.get('event_id'))}: superseded proposal: case status "
                           "refers to the current proposal, not approval of this older generation.</p>")
        details.append("<p class='plan'>" + _e(f"{r.get('summary') or r.get('event_id')}: Measurement: {source}; generation {generation if generation is not None else 'Unknown'}; "
                     f"ready {_observed_time(timing.get('ready_at'))}; positively observed post "
                     f"{posted}; "
                     f"accepted OK observed {approved} (includes polling).") + "</p>")
        if r.get("plan"):
            steps = "".join(f"<li>{_e(s['title'])}（{_e(s['due'])}）</li>" for s in r["plan"]["steps"])
            parts.append(f"<div class='plan'>進め方: {_e(r['plan']['title'])}<ol>{steps}</ol></div>")
        acts = r.get("actions") or []
        exec_rows = []
        status = (current_item or {}).get("status")
        if current_item:
            current_record = current_item.get("record") or {}
            for index, action in enumerate(current_record.get("actions") or []):
                state = (current_item.get("exec") or {}).get(str(index)) or {}
                exec_key = state.get("key")
                if not exec_key and current_item.get("key"):
                    from . import execute
                    exec_key = execute.idem_key(current_item["key"], index, action)
                evidence = executions.get(str(exec_key)) if exec_key else None
                if action.get("type") == "teams.reply":   # the same words as the case card above
                    exec_rows.append((action, _reply_state(current_item, index, executions)[1]))
                elif evidence:
                    exec_rows.append((action, _execution_label(evidence)))
                elif action:
                    if action.get("exec_skip"):
                        label = "実行対象外・未実行"
                    elif status in NOT_RUN:
                        label = NOT_RUN[status] + ("実行しない" if status == "rejected" else "未実行")
                    elif status == "approved":
                        label = "予定（記録だけ。実行の記録とは対応づいていない）"
                    else:
                        label = "判断の状態が不明のため未実行"
                    exec_rows.append((action, label))
        elif acts and found and not current:
            acts = []  # a superseded decision does not inherit its successor's execution state
        if superseded:   # an earlier draft of a case that was rewritten: the approval (if any) is of the current one
            verb = "書き直し前の案（承認の対象ではない）"
            ap_note += f" ・ 書き直し前の案（今の案は {_e(_int(_metadata(matched.get('measurement')).get('generation')))}）"
        else:
            verb = "承認後の計画（外部書き込みは既定で記録のみ）" if c == "human" else "記録された計画"
        parts.append(f"<div class='out'><span class='badge {c}'>{badge}</span>{ap_note}"
                     + (f"<ul>{''.join('<li>' + _e(verb) + ' — ' + _e(_action(a)) + '</li>' for a in acts)}</ul>" if acts else "")
                     + (f"<ul>{''.join('<li>' + _e(label) + ' — ' + _e(_action(action)) + '</li>' for action, label in exec_rows)}</ul>" if exec_rows else "")
                     + (f"<p class='plan'>メモ: {_e(r['advice'])}</p>" if r.get("advice") else "") + "</div></section>")
    if SKIPPED:   # every record has been read by now (the case cards and the measurement summary included)
        broken = "、".join(f"{name} の {n} 件" for name, n in SKIPPED.items() if n) or "記録の一部"
        parts.insert(notice_at, f"<div class='notice' role='alert'><b>読めない記録がありました</b>{_e(broken)}を飛ばして表示しています。"
                                "ファイルが書き込み途中だったか、壊れています。</div>")
    parts += details + ["</details></main></body></html>"]
    return "".join(parts)
