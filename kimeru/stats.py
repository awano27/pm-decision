"""`kimeru digest --week [--share]`: the last seven days in numbers.

--share is meant to be pasted into a public issue: numbers and environment facts only. No message text, subject, name,
id or path ever enters it. Jev's decisions are left out of the agreement rate, and the time line is not shown when Jev
is among the judges (TypeSafe's terms do not allow publishing Jev's performance).
"""
import json
import platform
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import __version__, config, review
from .backends import JevBackend


def _parse(ts):
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _within(rows, key, since, now=None):
    now = now or datetime.now(timezone.utc)
    out = []
    for r in rows:
        d = _parse(r.get(key))
        if d and since <= d <= now:
            out.append(r)
    return out


def _date_issues(rows, key, now, include_missing=True):
    """Count records that cannot support a weekly date-based metric."""
    invalid = future = 0
    for row in rows:
        raw = row.get(key)
        if not raw:
            invalid += bool(include_missing)
            continue
        parsed = _parse(raw)
        if parsed is None:
            invalid += 1
        elif parsed > now:
            future += 1
    return invalid, future


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


def _judge_of_review(rev, judge_of_key):
    return rev.get("judge") or judge_of_key.get(rev.get("key")) or "unknown"


def bundled_graph_names():
    """The names of the graphs that ship with kimeru: the only graph names --share may show."""
    names = set()
    for p in (Path(__file__).resolve().parent.parent / "graphs").glob("*.json"):
        try:
            names.add(json.loads(p.read_text(encoding="utf-8"))["name"])
        except (OSError, ValueError, KeyError):
            continue
    return names


def collect(out, now=None):
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=7)
    out = Path(out)
    decision_rows = review.read_jsonl(out / "decisions.jsonl")
    approval_rows = review.read_jsonl(out / "approvals.log.jsonl")
    review_rows = review.read_jsonl(review.reviews_path())
    decisions = _within(decision_rows, "at", since, now)
    dated = _within(approval_rows, "at", since, now)
    undated = sum(1 for r in approval_rows if not r.get("at"))
    judge_of_key = {review.decision_key(r): review.judge_of(r) for r in decision_rows}
    reviews = _within(review_rows, "at", since, now)
    sources = ((decision_rows, True), (approval_rows, False), (review_rows, True))
    invalid_evidence = sum(_date_issues(rows, "at", now, include_missing=missing)[0] for rows, missing in sources)
    future_evidence = sum(_date_issues(rows, "at", now, include_missing=False)[1] for rows, _ in sources)
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
    # The agreement rate leaves Jev's decisions out, whatever the period or the output folder: a review names the judge that
    # made the decision; an older one is looked up in this folder's decisions, and when that is not possible (another folder,
    # a decision no longer here) it is not counted, because it cannot be shown not to be Jev's.
    counted = [r for r in reviews if _judge_of_review(r, judge_of_key) not in (JevBackend.NAME, "unknown")]
    yes = sum(r.get("verdict") == "yes" for r in counted)
    no = sum(r.get("verdict") == "no" for r in counted)
    wrong = sum(r.get("verdict") == "wrong" for r in counted)
    left_out = sum(r.get("verdict") in ("yes", "no") for r in reviews) - yes - no
    n = len(decisions)
    return {
        "since": since.date().isoformat(), "decisions": n,
        "auto": sum(g["auto"] for g in per_graph.values()), "human": sum(g["human"] for g in per_graph.values()),
        "rule": sum(g["rule"] for g in per_graph.values()),
        "critical": sum(1 for r in decisions if r.get("notify")),
        "per_graph": per_graph, "approvals": statuses, "approvals_undated": undated,
        "invalid_evidence": invalid_evidence, "future_evidence": future_evidence,
        "judges": judges, "models": models, "reviewed_yes": yes, "reviewed_no": no,
        "reviewed_wrong": wrong, "reviewed_left_out": left_out,
        "perf": perf_line(decisions),
    }


_DPI_PROBE = ("import ctypes; u = ctypes.windll.user32; f = getattr(u, 'SetProcessDpiAwarenessContext', None); "
              "f and f(ctypes.c_void_p(-4)); print(u.GetDpiForSystem())")   # -4 is a handle-sized value: a plain int is passed as 32 bits and fails on 64-bit Python


def _dpi_percent():
    """The display scale of the primary screen in percent, or None when it cannot be told (then it is left out, not guessed).
    A plain call to GetDpiForSystem in this process says 96 (100%) at any scale, because the process is not DPI aware; the
    setting Windows applied is in the registry, and a child process that declares itself DPI aware is the fallback."""
    try:
        import winreg
    except ImportError:
        return None
    for sub, name in ((r"Control Panel\Desktop\WindowMetrics", "AppliedDPI"), (r"Control Panel\Desktop", "LogPixels")):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub) as k:
                v = winreg.QueryValueEx(k, name)[0]
        except OSError:
            continue
        if isinstance(v, int) and v > 0:
            return round(v * 100 / 96)
    try:
        r = subprocess.run([sys.executable, "-c", _DPI_PROBE], capture_output=True, text=True, timeout=10)
        v = int(r.stdout.strip())
        return round(v * 100 / 96) if v > 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def environment():
    return {"kimeru": __version__, "os": platform.platform(), "python": platform.python_version(),
            "writer": config.value("writer") or "未設定（定型文）", "display_scale_percent": _dpi_percent()}


def config_share():
    """`kimeru config show --share`: the settings in effect and where each came from, safe to paste into a public issue
    (see config.share_rows: no path, organization, subscription or address). The environment comes first."""
    env = environment()
    lines = [f"kimeru {env['kimeru']} / OS: {env['os']} / Python {env['python']}"
             + (f" / 画面の倍率 {env['display_scale_percent']}%" if env["display_scale_percent"] else ""),
             ""]
    rows = config.share_rows()
    width = max(len(k) for k, _, _ in rows)
    lines.append(f"{'setting'.ljust(width)}  {'value':<40}  source")
    lines += [f"{k.ljust(width)}  {(v or '-'):<40}  {src}" for k, v, src in rows]
    lines.append("secrets (environment variables; the value is never shown): "
                 + ", ".join(f"{n}={'set' if on else 'not set'}" for n, on in config.secrets_status().items()))
    return "\n".join(lines)


LABEL = {"approved": "OK", "rejected": "NG", "held": "保留", "redrafted": "修正", "redraft_failed": "修正できず", "ask_back": "聞き返し"}


def _graph_labels(per_graph, share):
    """{graph name: the name to print}. Shared output names only the graphs that ship with kimeru: a name the user gave
    to a graph of their own can be a project or a team, so it becomes 追加のグラフ 1, 2, ..."""
    if not share:
        return {n: n for n in per_graph}
    bundled = bundled_graph_names()
    extra = {n: f"追加のグラフ {i}" for i, n in enumerate(sorted(n for n in per_graph if n not in bundled), 1)}
    return {n: n if n in bundled else extra[n] for n in per_graph}


def render(s, share=False):
    lines = [f"kimeru の直近 7 日（{s['since']} から）"]
    n = s["decisions"]
    if not n:
        lines.append("判断: 0 件")
    else:
        lines.append(f"判断: {n} 件 / 自動で決めた {s['auto']} 件（{s['auto'] * 100 // n}%）・人に回した {s['human']} 件（{s['human'] * 100 // n}%）"
                     f" / 規則だけで決めた {s['rule']} 件 / 重大な通知 {s['critical']} 件")
        shown = _graph_labels(s["per_graph"], share)
        for name, g in sorted(s["per_graph"].items(), key=lambda kv: shown[kv[0]]):
            lines.append(f"  - {shown[name]}: {g['n']} 件（自動 {g['auto']}、人 {g['human']}、規則 {g['rule']}）")
    if s["approvals"]:
        lines.append("承認の結果: " + "、".join(f"{LABEL.get(k, k)} {v}" for k, v in sorted(s["approvals"].items(), key=lambda kv: str(kv[0]))))
    else:
        lines.append("承認の結果: 日時つきの記録なし")
    if s["approvals_undated"]:
        lines.append(f"（日時の無い古い承認の記録 {s['approvals_undated']} 件は、期間の集計に入れていません）")
    if s.get("invalid_evidence"):
        lines.append(f"日時が不正な記録 {s['invalid_evidence']} 件は、期間の集計に入れていません")
    if s.get("future_evidence"):
        lines.append(f"未来の日時の記録 {s['future_evidence']} 件は、期間の集計に入れていません")
    if s.get("perf") and "Jev" not in s["judges"]:
        lines.append(s["perf"])
    total = s["reviewed_yes"] + s["reviewed_no"]
    left_out = s.get("reviewed_left_out", 0)
    if total:
        lines.append(f"判断の一致率: {s['reviewed_yes'] * 100 // total}%（確かめた {total} 件のうち、合っている {s['reviewed_yes']} 件）")
        if s.get("reviewed_wrong"):
            lines.append(f"  最終結果が違うとされた判断: {s['reviewed_wrong']} 件（質問単位の一致率には混ぜていません）")
        if left_out:
            lines.append("  （Jev の判断、または判断モデルが分からない判断を確かめた分は、数えていません）")
    elif s.get("reviewed_wrong"):
        lines.append("判断の一致率: 出しません（最終結果が違うという確認は質問単位の一致率に混ぜません）")
        lines.append(f"  最終結果が違うとされた判断: {s['reviewed_wrong']} 件")
    elif "Jev" in s["judges"] or left_out:
        lines.append("判断の一致率: 出しません（数えられる判断がありません。Jev の判断は数えません。Jev の性能の数値は、TypeSafe の利用規約で公開できません）")
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
