"""`kimeru digest --week [--share]`: the last seven days in numbers.

--share is meant to be pasted into a public issue: numbers and environment facts only. No message text, subject, name,
id or path ever enters it. When the judge is Jev the agreement rate is not shown (TypeSafe's terms do not allow
publishing Jev's performance).
"""
import ctypes
import json
import os
import platform
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import __version__, config, review


def _parse(ts):
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _within(rows, key, since):
    out = []
    for r in rows:
        d = _parse(r.get(key))
        if d and d >= since:
            out.append(r)
    return out


def _rule_only(rec):
    path = rec.get("path") or []
    return all("matched" in (s.get("answer") or {}) for s in path)


def _median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else 0


def perf_line(rows):
    """One line: what one event cost (calls to the judge, questions, seconds incl. the writer): median and max."""
    ps = [r["perf"] for r in rows if r.get("perf")]
    if not ps:
        return ""
    secs = [p.get("judge_sec", 0) + p.get("writer_sec", 0) for p in ps]
    return (f"1 イベントあたり（{len(ps)} 件）: 判断の呼び出し 中央値 {_median([p.get('calls', 0) for p in ps])} 回・"
            f"質問 中央値 {_median([p.get('questions', 0) for p in ps])} 問（最大 {max(p.get('questions', 0) for p in ps)}）・"
            f"時間 中央値 {_median(secs):.1f} 秒・最大 {max(secs):.1f} 秒（writer を含む）")


def collect(out, now=None):
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=7)
    out = Path(out)
    decisions = _within(review.read_jsonl(out / "decisions.jsonl"), "at", since)
    approvals = review.read_jsonl(out / "approvals.log.jsonl")
    dated = _within(approvals, "at", since)
    undated = sum(1 for r in approvals if not r.get("at"))
    reviews = _within(review.read_jsonl(review.reviews_path()), "at", since)
    per_graph = {}
    for r in decisions:
        g = per_graph.setdefault(r.get("graph", "?"), {"n": 0, "auto": 0, "human": 0, "rule": 0})
        g["n"] += 1
        g["human" if r.get("needs_human") else "auto"] += 1
        g["rule"] += _rule_only(r)
    statuses = {}
    for r in dated:
        statuses[r.get("status")] = statuses.get(r.get("status"), 0) + 1
    judges = sorted({(r.get("judge") or {}).get("name", "unknown") for r in decisions})
    models = sorted({(r.get("judge") or {}).get("model", "") for r in decisions if (r.get("judge") or {}).get("model")})
    yes = sum(r.get("verdict") == "yes" for r in reviews)
    no = sum(r.get("verdict") == "no" for r in reviews)
    n = len(decisions)
    return {
        "since": since.date().isoformat(), "decisions": n,
        "auto": sum(g["auto"] for g in per_graph.values()), "human": sum(g["human"] for g in per_graph.values()),
        "rule": sum(g["rule"] for g in per_graph.values()),
        "critical": sum(1 for r in decisions if r.get("notify")),
        "per_graph": per_graph, "approvals": statuses, "approvals_undated": undated,
        "judges": judges, "models": models, "reviewed_yes": yes, "reviewed_no": no,
        "perf": perf_line(decisions),
    }


def _dpi_percent():
    try:
        return round(ctypes.windll.user32.GetDpiForSystem() * 100 / 96)
    except Exception:
        return None


def environment():
    return {"kimeru": __version__, "os": platform.platform(), "python": platform.python_version(),
            "writer": config.value("writer") or "未設定（定型文）", "display_scale_percent": _dpi_percent()}


LABEL = {"approved": "OK", "rejected": "NG", "held": "保留", "redrafted": "修正", "redraft_failed": "修正できず", "ask_back": "聞き返し"}


def render(s, share=False):
    lines = [f"kimeru の直近 7 日（{s['since']} から）"]
    n = s["decisions"]
    if not n:
        lines.append("判断: 0 件")
    else:
        lines.append(f"判断: {n} 件 / 自動で決めた {s['auto']} 件（{s['auto'] * 100 // n}%）・人に回した {s['human']} 件（{s['human'] * 100 // n}%）"
                     f" / 規則だけで決めた {s['rule']} 件 / 重大な通知 {s['critical']} 件")
        for name, g in sorted(s["per_graph"].items()):
            lines.append(f"  - {name}: {g['n']} 件（自動 {g['auto']}、人 {g['human']}、規則 {g['rule']}）")
    if s["approvals"]:
        lines.append("承認の結果: " + "、".join(f"{LABEL.get(k, k)} {v}" for k, v in sorted(s["approvals"].items(), key=lambda kv: str(kv[0]))))
    else:
        lines.append("承認の結果: 日時つきの記録なし")
    if s["approvals_undated"]:
        lines.append(f"（日時の無い古い承認の記録 {s['approvals_undated']} 件は、期間の集計に入れていません）")
    if s.get("perf") and "Jev" not in s["judges"]:
        lines.append(s["perf"])
    total = s["reviewed_yes"] + s["reviewed_no"]
    if "Jev" in s["judges"]:
        lines.append("判断の一致率: 出しません（判断モデルが Jev のため。Jev の性能の数値は、TypeSafe の利用規約で公開できません）")
    elif total:
        lines.append(f"判断の一致率: {s['reviewed_yes'] * 100 // total}%（確かめた {total} 件のうち、合っている {s['reviewed_yes']} 件）")
    else:
        lines.append("判断の一致率: まだ確かめた判断がありません（kimeru review）")
    if share:
        env = environment()
        lines += ["", "環境:",
                  f"  - kimeru {env['kimeru']} / 判断モデル: {', '.join(s['judges']) or '記録なし'}"
                  + (f"（{', '.join(s['models'])}）" if s["models"] else ""),
                  f"  - writer: {env['writer']}",
                  f"  - OS: {env['os']} / Python {env['python']}"
                  + (f" / 画面の倍率 {env['display_scale_percent']}%" if env["display_scale_percent"] else "")]
    return "\n".join(lines)


def week(out, share=False, now=None):
    return render(collect(out, now), share=share)
