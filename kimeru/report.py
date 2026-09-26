"""`kimeru report`: one self-contained HTML page from out/decisions.jsonl (+ approvals).

No external assets, works offline, light and dark. Shows totals, then every event with
its judgment path (rule / model / playbook chips), the plan, and what was executed or
sent to a person.
"""
import html
import json
from pathlib import Path

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


def build(out, graphs, title="kimeru 判断レポート"):
    out = Path(out)
    decisions = _rows(out / "decisions.jsonl")
    ap = json.loads((out / "approvals.json").read_text(encoding="utf-8")) if (out / "approvals.json").exists() else {"items": {}}
    status = {it["key"]: (n, it["status"]) for n, it in ap["items"].items()}
    kinds = {}
    for lst in graphs.values():
        for g in lst:
            kinds.update({nid: n["kind"] for nid, n in g["nodes"].items()})

    def cls(r):
        if r["outcome"] == "advise":
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
             f"<h1>{_e(title)}</h1><p class='sub'>{_e(out)} ・ 外部への書き込みはすべて試し実行（記録のみ）</p>",
             "<div class='tiles'>" + "".join(f"<div class='tile'><b>{v}</b><span>{_e(l)}</span></div>" for v, l in tiles) + "</div>",
             "<div class='legend'><span class='chip rule'>規則</span><span class='chip model'>判断（モデル）</span><span class='chip plan'>進め方</span>"
             "<span class='badge ok'>自動</span><span class='badge safe'>安全側</span><span class='badge human'>人の確認</span></div>"]
    for r in decisions:
        c = cls(r)
        badge = {"ok": "自動", "safe": "安全側", "human": "人の確認"}[c]
        key = f"{r.get('graph')}:{r.get('event_id')}:{r.get('node')}"
        ap_note = ""
        if key in status:
            num, st = status[key]
            ap_note = f" ・ 確認 #{_e(num)}: {_e({'approved': '承認', 'rejected': '却下', 'held': '保留', 'pending': '返信待ち'}.get(st, st))}"
        parts.append(f"<section class='ev {c}'><div class='head'><span class='src'>{_e(SRC.get(r.get('event_kind'), r.get('event_kind')))}</span>"
                     f"<span class='sum'>{_e(r.get('summary') or r.get('event_id'))}</span></div>")
        parts.append("<div class='path'>" + "".join(_chip(s, kinds) for s in r["path"]) + "</div>")
        if r.get("plan"):
            steps = "".join(f"<li>{_e(s['title'])}（{_e(s['due'])}）</li>" for s in r["plan"]["steps"])
            parts.append(f"<div class='plan'>進め方: {_e(r['plan']['title'])}<ol>{steps}</ol></div>")
        acts = r.get("actions") or []
        verb = "承認されたら実行" if c == "human" else "実行（試し）"
        parts.append(f"<div class='out'><span class='badge {c}'>{badge}</span>{ap_note}"
                     + (f"<ul>{''.join('<li>' + _e(verb) + ' — ' + _e(_action(a)) + '</li>' for a in acts)}</ul>" if acts else "")
                     + (f"<p class='plan'>メモ: {_e(r['advice'])}</p>" if r.get("advice") else "") + "</div></section>")
    parts.append("</main></body></html>")
    return "".join(parts)
