"""`kimeru demo`: replay a scripted PM day for a live presentation.

Teams is simulated in memory (nothing is posted anywhere); judgments come from the real
backend (Kev by default). Each step prints what arrived, how each judgment point was
decided (rule / model / playbook), and what was executed automatically or sent to a
person. Decisions are written to --out so `kimeru report` can render them afterwards.
"""
import json
import sys
import time
from pathlib import Path

from . import brief as brief_mod, events, notify, pull

KIND_LABEL = {"match": "規則", "judge": "判断", "plan": "進め方"}
ACTION_LABEL = {
    "teams.reply": "Teams に返信", "teams.post": "Teams チャネルに投稿", "ado.comment": "ADO にコメント",
    "ado.update": "ADO を更新", "ado.create": "ADO に起票", "oncall.page": "当番を呼び出し", "log.only": "記録のみ",
}


class DemoTeams:
    """In-memory Teams: a chat list for pull and a self chat for notify/approvals."""

    def __init__(self):
        self.chat_list, self.timeline, self.posts = [], [], []

    def chats(self):
        return self.chat_list

    def post(self, text, send):
        self.posts.append(text)
        if send and text.startswith("[kimeru #"):
            self.timeline.append("P:" + text.split("#", 1)[1].split("]", 1)[0])
        return {"ok": True}

    def read(self):
        return {"ok": True, "timeline": list(self.timeline)}


class Timed:
    """Wraps the backend to measure time spent in the model."""

    def __init__(self, inner):
        self.inner, self.seconds, self.calls = inner, 0.0, 0
        self.profile = getattr(inner, "profile", None)

    def ask(self, state, questions):
        t = time.time()
        try:
            return self.inner.ask(state, questions)
        finally:
            self.seconds += time.time() - t
            self.calls += 1


_REC = None   # when recording: list of [line, pause] with pause in "pace units" (1.0 = --pace)
_PACE = 0.0


def _w(s="", units=0.0):
    """Print a line, then pause `units` x the --pace seconds (recorded in units, so a
    run made with --pace 0 still replays with pauses)."""
    print(s, flush=True)
    if _REC is not None:
        _REC.append([s, units])
    if units and _PACE:
        time.sleep(units * _PACE)


def record_to(path, results_fn):
    """Run `results_fn()` while capturing every demo line, then save them to `path`."""
    global _REC
    _REC = []
    try:
        return results_fn()
    finally:
        lines, _REC = _REC, None
        Path(path).write_text(json.dumps({"v": 1, "lines": lines}, ensure_ascii=False), encoding="utf-8")


_STEP = False   # presenter mode: wait for Enter before each event of the day


def _wait():
    if _STEP:
        try:
            input("    （Enter で次へ）")
        except EOFError:
            pass


def replay(path, pace=1.5, step=False):
    """Print a recorded demo with the same line-by-line pacing, without any model."""
    global _STEP
    _STEP = step
    rec = json.loads(Path(path).read_text(encoding="utf-8"))
    for line, units in rec["lines"]:
        if line.startswith("[") or line.startswith("=== まとめ"):
            _wait()
        print(line, flush=True)
        if pace and units:
            time.sleep(units * pace)


def _action(a):
    label = ACTION_LABEL.get(a.get("type"), a.get("type"))
    detail = a.get("title") or a.get("text") or a.get("summary") or ""
    if a.get("fields"):
        detail = ", ".join(f"{k.split('.')[-1]}={v}" for k, v in a["fields"].items())
    detail = str(detail).replace("\n", " ")
    return f"{label}: {detail[:60]}" if detail else label


def show_result(r, graphs, pace):
    g = next(x for x in graphs[r["event_kind"]] if x["name"] == r["graph"])
    for s in r["path"]:
        kind = g["nodes"][s["node"]]["kind"]
        a = s["answer"]
        if kind == "match":
            note = "該当 → モデルに聞かずに決定" if s["edge"] == "yes" else "該当なし"
        elif kind == "plan":
            note = f"型={a.get('playbook')}（確信度 {a.get('confidence', 0):.2f}）" if s["edge"] == "ok" else "型を決めきれない"
        elif "choice" in a:
            note = f"{a['choice']}（確信度 {a.get('confidence', 0):.2f}）"
        elif "noul" in a:
            note = f"はい の確率 {a['noul']:.2f}"
        elif "score" in a:
            note = f"{a['score']:.2f}（確信度 {a.get('confidence', 0):.2f}）"
        else:
            note = ""
        edge = "確信なし → 安全側へ" if s["edge"] == "unsure" else s["edge"]
        _w(f"      [{KIND_LABEL.get(kind, kind)}] {s['node']}: {note}  → {edge}", pace / 3)
    if r.get("plan"):
        _w(f"      進め方: {r['plan']['title']}", pace / 3)
        for i, st in enumerate(r["plan"]["steps"], 1):
            _w(f"        {i}. {st['title']}（{st['due']}）", pace / 6)
    if r["needs_human"] or r["outcome"] == "advise":
        _w(f"    → 人の確認へ: {r['advice']}", pace)
    else:
        for a in r["actions"]:
            _w(f"    → 自動実行（試し）: {_action(a)}", pace / 3)
        if r.get("advice"):
            _w(f"      PM へのメモ: {r['advice']}", pace / 3)


def run(scenario, graphs, backend, playbooks, process, out, pace=1.5, step=False):
    global _PACE, _STEP
    _STEP = step
    _PACE, pace = pace, 1.0   # below, `pace` is one pause unit; _w converts units to seconds
    sc = json.loads(Path(scenario).read_text(encoding="utf-8"))
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    for f in ("decisions.jsonl", "queue.jsonl", "approvals.json"):
        (out / f).unlink(missing_ok=True)
    teams, be = DemoTeams(), Timed(backend)
    sec = {"sigs": {"_": "_"}}   # skip the "first poll is a baseline" rule
    results, posted = [], []
    _w(f"=== {sc['title']}  （判断: {getattr(backend, 'NAME', type(backend).__name__)}、Teams はデモ用の模擬）", pace)
    for st in sc["steps"]:
        _w("")
        _wait()
        _w(f"[{st['time']}] {st['label']}", pace / 2)
        src = st["source"]
        if src == "teams":
            teams.chat_list = [st["chat"]]
            evs = pull.teams_events(teams.chats(), sec)
            _w(f"    Teams のチャット一覧から検出: 「{st['chat']['preview']}」", pace / 2)
            payloads = evs
        elif src in ("alert", "ado", "minutes"):
            payloads = [st["payload"]]
            if src == "minutes":
                payloads = events.normalize(st["payload"])   # one event per line
                _w(f"    議事録を {len(payloads)} 行に分けて 1 行ずつ判断", pace / 2)
        elif src == "notify":
            posted = notify.notify(out, teams, send=True)
            for text in teams.posts[-len(posted):] if posted else []:
                _w("    自分とのチャットに投稿:", pace / 3)
                for line in text.splitlines():
                    _w(f"      | {line}", pace / 6)
            if not posted:
                _w("    確認待ちはありません", pace)
            continue
        elif src == "reply":
            words = st.get("replies") or [st.get("reply", "OK")] * len(posted)
            for n, word in zip(posted, words):
                teams.timeline.append(f"R:{word} {n}")
                _w(f"    iPhone から「{word} {n}」と返信", pace / 2)
            names = {"approved": "承認", "rejected": "却下", "held": "保留"}
            for ch in notify.collect(out, teams):
                _w(f"    #{ch['id']} → {names.get(ch['status'], ch['status'])}", pace / 2)
                for e in ch.get("executed", []):
                    _w(f"      → 実行（試し）: {_action(e['action'])}", pace / 3)
            continue
        elif src == "brief":
            text, _ = brief_mod.build(out, be)
            for line in text.splitlines():
                _w(f"      | {line}", pace / 4)
            continue
        else:
            continue
        for p in payloads:
            t0, c0 = be.seconds, be.calls
            if src == "minutes":
                _w(f"    ・「{p['item']}」", pace / 3)
            for r in process(p, graphs, be, out, playbooks):
                show_result(r, graphs, pace)
                results.append(r)
            if be.calls > c0:
                _w(f"    （モデルの判断 {be.calls - c0} 回、{be.seconds - t0:.1f} 秒）", pace / 3)

    kinds = {nid: n["kind"] for lst in graphs.values() for g in lst for nid, n in g["nodes"].items()}
    by_rule = lambda r: any(kinds.get(s["node"]) == "match" and s["edge"] == "yes" for s in r["path"])
    low = lambda r: any(s["edge"] == "unsure" for s in r["path"]) and not by_rule(r)
    auto = sum(1 for r in results if r["outcome"] == "decide" and not low(r))
    safe = sum(1 for r in results if r["outcome"] == "decide" and low(r))
    human = sum(1 for r in results if r["outcome"] == "advise")
    rule = sum(1 for r in results if by_rule(r))
    _w("")
    _wait()
    _w("=== まとめ")
    _w(f"    判断 {len(results)} 件: 自動で決定 {auto} / 確信が低く安全側で決定 {safe} / 人の確認 {human}")
    _w(f"    うち規則（安全網）で即決定 {rule} 件、モデルの判断 {be.calls} 回・合計 {be.seconds:.1f} 秒")
    _w(f"    記録: {out / 'decisions.jsonl'}")
    return results
