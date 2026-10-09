"""`kimeru diagnose`: one sheet that says why self-chat posts end as "delivery unknown", how each decision walked its graph, and
what the last cycles reported (run-diagnose.cmd -> tools/diagnose.ps1 -> here). Nothing is posted and nothing is changed.

The sheet is meant to be shared: it holds numbers, yes/no answers, node and edge names of the graphs, exception class names and a
fixed list of known short messages, and kimeru's own case numbers. It never holds a message text, a title, a person's or a chat's
name, an organization or project, a path, or an id other than a case number. The texts the probe reads go through memory only.

The probe (on by default) is the read the approvals step makes of the self chat, done with the same message reader as the send
readback (tools/teams-self.ps1 -Action probe): it opens the self chat if needed, types nothing and sends nothing. It runs only
while approvals.lock is free and is skipped when Teams is not running."""
import argparse
import json
import os
import platform
import re
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import config, fsutil

RESULT_FILE = "kimeru-diagnose-result.txt"
KINDS = {"teams.chat": "teams", "ado.workitem.created": "ado", "monitor.alert": "alert", "meeting.item": "meeting"}
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
# the short messages a cycle report may show as they are (everything else: the exception class only)
KNOWN_MESSAGES = [re.compile(p) for p in (
    r"^#\d+ was not posted with verified readback$",
    r"^#\d+ Copilot request was not posted with verified readback$",
    r"^brief delivery_unknown; use delivery show/confirm/retry$",
    r"^brief delivery [a-z_]+$",
    r"^Teams window not found$",
    r"^compose box not found$",
    r"^compose box does not start with \[kimeru; not sending$",
    r"^compose box does not hold exactly the planned text \(before=\d+ box=\d+ planned=\d+ endsWith=\w+ startsWith=\w+\); not sent",
    r"^the compose box holds a draft of yours \(\d+ characters\); nothing pasted, will retry later$",
    r"^an earlier kimeru post is left in the compose box and could not be cleared",
    r"^another kimeru Teams operation is running; Teams was not touched$",
    r"^Teams is not the foreground window; no keys sent",
    r"^Teams is in use \(it is in front, or an input box has the focus\); nothing was opened$",
    r"^the keyboard / mouse has been in use for \d+ s; Teams was not touched",
    r"^self chat not found in chat list",
    r"^self chat did not open",
    r"^the window title is not the self chat \(learned name\); nothing pasted$",
    r"^the window title changed before the self-chat readback baseline; nothing sent$",
    r"^window changed before send \(title is not the self chat\); aborted$",
    r"^keyboard focus is not the compose box; nothing sent$",
    r"^the script did not finish in time$",
    r"^the script's output could not be read$",
)]
CLASS = re.compile(r"^([A-Z][A-Za-z0-9_]{1,60}(?:Error|Exception|Unavailable|Expired|Exit|Interrupt|Failure)): ?(.*)$", re.S)
TRUNCATION = ("…", "...", "もっと見る", "さらに表示", "See more", "Show more", "続きを読む")
HEAD = re.compile(r"^\[kimeru (?:(?P<test>試験 )?(?P<run>実行 )?#(?P<num>\d+)(?P<copilot> Copilot 用)?|(?P<notice>通知)|(?P<ready>送信用 #\d+)|(?P<brief>brief))")


def norm(s):
    """The text as the send readback compares it (teams-self.ps1 Normalize-MeaningfulText)."""
    return re.sub(r"[\s\u00a0]+", " ", re.sub(r"[\u200b\ufeff]", "", str(s or ""))).strip()


def _name(v):
    v = str(v or "")
    return v if SAFE_NAME.match(v) else "?"


def _num(v):
    return f"{v:.2f}" if isinstance(v, (int, float)) and not isinstance(v, bool) else "?"


def _jsonl(p, last=None):
    p = Path(p)
    if not p.exists():
        return []
    rows = []
    with p.open("rb") as f:   # the tail only: a long log is never read whole
        size = f.seek(0, 2)
        f.seek(max(0, size - 512 * 1024))
        data = f.read().decode("utf-8", errors="ignore").splitlines()
    if size > 512 * 1024:
        data = data[1:]
    for line in data:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict):
            rows.append(r)
    return rows[-last:] if last else rows


def _age(at, now):
    try:
        t = datetime.fromisoformat(str(at))
    except ValueError:
        return "?"
    if t.tzinfo is None:
        t = t.astimezone()
    m = int((now - t).total_seconds() // 60)
    return f"{m} 分前" if m < 120 else f"{m // 60} 時間前"


def _shape(body):
    body = str(body or "")
    return f"{len(body)} 字 {body.count(chr(10)) + 1 if body else 0} 行"


def error_text(value):
    """'error: Class: message' -> 'Class: <known message>' or 'Class' (never the message itself unless it is a known one)."""
    s = str(value or "")
    if s.startswith("error:"):
        s = s[len("error:"):].strip()
    m = CLASS.match(s)
    cls, msg = (m.group(1), m.group(2).strip()) if m else ("", s)
    known = next((k for k in KNOWN_MESSAGES if k.match(msg)), None)
    if known:
        shown = known.match(msg).group(0)
        return f"{cls}: {shown}" if cls else shown
    return cls or "(内容は非表示)"


# ---------------------------------------------------------------- A. unknown deliveries

def unknown_deliveries(out):
    """Every send whose result is unknown, with the body that was attempted (kept in memory only)."""
    out = Path(out)
    ap = fsutil.read_json(out / "approvals.json", {"items": {}})
    ap = ap if isinstance(ap, dict) else {"items": {}}
    rows = []
    for num, it in (ap.get("items") or {}).items():
        if isinstance(it, dict) and it.get("delivery_unknown"):
            att = it.get("delivery_attempt") or {}
            rows.append({"kind": "case", "case": "#" + str(num), "part": it.get("delivery_unknown_part", "post"),
                         "at": att.get("at") or it.get("delivery_unknown_at"), "body": att.get("body"),
                         "evidence": {k: att[k] for k in ("readback", "matched", "sent", "typed") if k in att}})
    uncertain = ap.get("notice_delivery_unknown") or {}
    if isinstance(uncertain, list):
        uncertain = {k: None for k in uncertain}
    attempts = ap.get("notice_delivery_attempts") or {}
    for i, (key, at) in enumerate(uncertain.items(), 1):
        att = attempts.get(key) or {}
        rows.append({"kind": "notice", "case": f"通知{i}", "part": "notice", "at": att.get("at") or at, "body": att.get("body"),
                     "evidence": {}})
    if ap.get("outbox_delivery_unknown"):
        att = ap.get("outbox_delivery_attempt") or {}
        m = re.match(r"^\[kimeru (?:実行|送信用) #(\d+)", str(att.get("body") or ""))
        rows.append({"kind": "outbox", "case": "#" + m.group(1) if m else "-", "part": "outbox",
                     "at": att.get("at") or ap.get("outbox_delivery_unknown_at"), "body": att.get("body"), "evidence": {}})
    st = fsutil.read_json(out / "daily_state.json", {})
    b = st.get("brief_delivery_unknown") if isinstance(st, dict) else None
    if isinstance(b, dict):
        rows.append({"kind": "brief", "case": str(b.get("date") or "-")[:10], "part": "brief", "at": b.get("at"), "body": b.get("body") or b.get("text"),
                     "evidence": {}})
    return rows


def _char(c):
    """What a character is, never the character when it could be part of a word."""
    if not c:
        return "（終わり）"
    cat = unicodedata.category(c)
    if cat[0] in "SZCP" or c in "\ufe0f\u200d":
        return f"U+{ord(c):04X} {cat}"
    return cat


def compare(expected, visible):
    """Numbers only: how a stored body and a text read from the screen differ."""
    e, v = norm(expected), norm(visible)
    k = 0
    while k < min(len(e), len(v)) and e[k] == v[k]:
        k += 1
    out = {"equal": e == v, "expected": len(e), "visible": len(v), "prefix": k}
    if e != v:
        out["at"] = f"期待 {_char(e[k:k + 1])} / 画面 {_char(v[k:k + 1])}"
    return out


def _truncated(text):
    t = norm(text)
    return any(t.endswith(m) or m in t[-40:] for m in TRUNCATION)


def _head(text):
    m = HEAD.match(norm(text))
    if not m:
        return "other", ""
    if m.group("notice"):
        return "notice", ""
    if m.group("ready"):
        return "ready", ""
    if m.group("brief"):
        return "brief", ""
    kind = "test" if m.group("test") else "result" if m.group("run") else "copilot" if m.group("copilot") else "post"
    return kind, m.group("num")


def run_probe(out, bridge=None):
    """The self chat as the send readback reads it. Returns (result dict or None, why it was skipped)."""
    out = Path(out)
    from . import notify
    with fsutil.exclusive(notify.lock_path(out)) as got:
        if not got:
            return None, "スキップ（approvals.lock を他の実行が使用中。自動運転のサイクル中かもしれません）"
        bridge = bridge or notify.PowerShellBridge()
        try:
            return bridge.probe(), ""
        except Exception as e:   # Teams not running, the self chat not found, a timeout: say which, never the text
            msg = str(e).split("\n")[0]
            return None, "スキップ（" + error_text(f"{type(e).__name__}: {msg}") + "）"


def section_a(out, now, probe=None, probe_note=""):
    rows = unknown_deliveries(out)
    L = ["== A. 配信結果不明（自分とのチャットへの投稿）", f"件数 {len(rows)}"]
    for r in rows:
        ev = r["evidence"]
        saved = ", ".join(f"{k}={json.dumps(v) if isinstance(v, bool) else '記録あり'}" for k, v in ev.items()) or "なし"
        L.append(f"- {r['kind']} {r['case']} {r['part']}  {_age(r['at'], now)}  本文 {_shape(r['body'])}  保存された読み返しの記録: {saved}")
    if rows:
        L.append("  ※ 読み返しの結果（matched / sent / typed）は保存されません。不明の記録に残るのは送ろうとした本文と時刻だけです"
                 "（notify.py: bridge.post の戻り値は判定に使ったあと捨てられる）")
    L.append("")
    L.append("-- 画面の読み取り（送信なし。送信時の読み返しと同じ読み方）")
    if probe is None:
        L.append(probe_note or "実行しませんでした")
        return L
    msgs = [m for m in (probe.get("messages") or []) if isinstance(m, dict)]
    own = [m for m in msgs if norm(m.get("text")).startswith("[kimeru") or norm(m.get("joined")).startswith("[kimeru")]
    tall = sum(1 for m in msgs if isinstance(m.get("height"), (int, float)) and m["height"] > 0.6)
    L.append(f"読み方 {probe.get('how')} / ノード {probe.get('rows')} / メッセージ {len(msgs)} / kimeru の投稿 {len(own)} / "
             f"自分とのチャットの題名 {'一致' if probe.get('selfTitle') else '不一致'} / 画面の 6 割を超える高さのメッセージ {tall}")
    for r in rows:
        if not r["body"]:
            L.append(f"- {r['kind']} {r['case']}: 送ろうとした本文の記録がないため比べられません")
            continue
        head = norm(str(r["body"]).split("\n")[0])
        cands = [m for m in msgs if norm(m.get("text")).startswith(head) or norm(m.get("joined")).startswith(head)]
        if not cands:
            L.append(f"- {r['kind']} {r['case']} {r['part']}: 画面に見つからない（先頭行が一致するメッセージ 0）")
            continue
        m = cands[-1]
        c, j = compare(r["body"], m.get("text")), compare(r["body"], m.get("joined"))
        L.append(f"- {r['kind']} {r['case']} {r['part']}: 候補 {len(cands)} / 読み返しの比較 {'一致' if c['equal'] else '不一致'}"
                 f"（画面 {c['visible']} 字 / 送信 {c['expected']} 字 / 先頭から一致 {c['prefix']} 字"
                 + (f" / 最初の違い {c['at']}" if not c["equal"] else "") + "）"
                 + f" / 全ノードをつなぐと {'一致' if j['equal'] else '不一致'}（{j['visible']} 字）"
                 + f" / ノード {m.get('fragments')} / 行 {m.get('raw_lines')} / 省略の印 {'あり' if _truncated(m.get('text')) else 'なし'}"
                 + f" / 高さ {_num(m.get('height'))}")
    bodies = {(r["case"].lstrip("#"), r["part"]): r["body"] for r in rows if r["kind"] == "case" and r["body"]}
    L.append("直近の kimeru の投稿（新しい順に 10 件。種類 番号: 画面の字数 / 行 / ノード / 省略の印 / 高さ / 送ろうとした本文との比較）")
    for m in list(reversed(own))[:10]:
        kind, num = _head(m.get("text") or m.get("joined"))
        line = (f"- {kind} {('#' + num) if num else ''}: {len(norm(m.get('text')))} 字 / {m.get('raw_lines')} 行 / "
                f"ノード {m.get('fragments')}（つなぐと {len(norm(m.get('joined')))} 字） / 省略の印 "
                f"{'あり' if _truncated(m.get('text')) else 'なし'} / 高さ {_num(m.get('height'))}")
        body = bodies.get((num, {"post": "post", "copilot": "copilot_request"}.get(kind, "")))
        if body:
            c = compare(body, m.get("text"))
            line += f" / 送信 {c['expected']} 字と{'一致' if c['equal'] else '不一致（先頭から一致 ' + str(c['prefix']) + ' 字）'}"
        L.append(line)
    return L


# ---------------------------------------------------------------- B. judgment paths

def _answer(a):
    if not isinstance(a, dict):
        return ""
    if "invalid" in a:
        return "invalid"
    if "matched" in a:   # the pattern that matched is not shown
        return "match" if a.get("matched") else "nomatch"
    bits = []
    if "playbook" in a:
        bits.append("playbook=" + _name(a.get("playbook")))
    for k in ("noul", "score"):
        if k in a:
            bits.append(f"{k}={_num(a[k])}")
    if "choice" in a:
        bits.append("choice=" + _name(a.get("choice")))
    if "confidence" in a:
        bits.append(f"c={_num(a['confidence'])}")
    if a.get("guard"):
        bits.append("guard")
    return " ".join(bits)


def section_b(out, last=50):
    rows = _jsonl(Path(out) / "decisions.jsonl", last=last)
    L = ["== B. 判断の経路（decisions.jsonl の直近 %d 件。node[edge 値 確信度] -> 終点 / 行き先）" % last, f"件数 {len(rows)}"]
    tally, dest = {}, {}
    for r in rows:
        kind = KINDS.get(r.get("event_kind"), _name(r.get("event_kind")))
        steps = []
        stop = None
        for s in r.get("path") or []:
            if not isinstance(s, dict):
                continue
            steps.append(f"{_name(s.get('node'))}[{_name(s.get('edge'))} {_answer(s.get('answer'))}]".replace(" ]", "]"))
            if stop is None and s.get("edge") == "unsure":
                stop = _name(s.get("node")) + "(unsure)"
        to = "PM" if r.get("needs_human") else "通知" if r.get("notify") else "自動"
        dest[to] = dest.get(to, 0) + 1
        judge = _name((r.get("judge") or {}).get("name")) if isinstance(r.get("judge"), dict) else "?"
        L.append(f"- {kind} {judge}: " + " > ".join(steps) + f" -> {_name(r.get('node'))} {_name(r.get('outcome'))} / {to}"
                 + (" / severe" if r.get("severe_guard") else ""))
        if to != "自動":
            key = (kind, stop or _name(r.get("node")))
            tally[key] = tally.get(key, 0) + 1
    L.append("行き先: " + (", ".join(f"{k} {v}" for k, v in sorted(dest.items())) or "なし"))
    L.append("PM・通知へ行った件の集計（種類, 迷った節点（unsure）または終点）:")
    for (kind, node), n in sorted(tally.items(), key=lambda x: -x[1]):
        L.append(f"  {kind}, {node}: {n}")
    return L


# ---------------------------------------------------------------- C. delivery list, status, cycles

def _status_line(line):
    line = re.sub(r"(last error: )\S.*$", lambda m: m.group(1) + error_text(m.group(0)[len(m.group(1)):]), str(line))
    if re.search(r"[A-Za-z]:\\|\\\\|https?://|@", line):
        return "(パスやアドレスを含むため省略)"
    return line


def _cycle_value(v):
    if isinstance(v, str) and v.startswith("error"):
        return "error(" + error_text(v) + ")"
    if isinstance(v, list):
        return f"ok({len(v)})"
    if isinstance(v, bool):
        return "ok" if v else "no"
    if isinstance(v, int):
        return f"ok({v})"
    if isinstance(v, dict):
        return "ok"
    if isinstance(v, str):
        return "ok" if not v.startswith(("failed", "shown (not")) else _name(v.split(":")[0].split(" ")[0])
    return "?"


def section_c(out, now, cycles=10):
    from . import daily, notify
    out = Path(out)
    L = ["== C. 配信の一覧・自動運転の状態・直近のサイクル"]
    rows = notify.delivery_pending(out)
    if getattr(rows, "busy", False):
        L.append("delivery list: 他の実行が approvals.lock を使用中のため読めませんでした")
    else:
        n_notice = 0
        L.append(f"delivery list: {len(rows)} 件")
        for r in rows:
            if r.get("kind") == "notice":
                n_notice += 1
                target = f"通知{n_notice}"
            elif r.get("kind") == "case":
                target = "#" + str(r.get("target"))
            else:
                target = _name(r.get("kind"))
            part = f" {_name(r.get('part'))}" if r.get("part") else ""
            L.append(f"- {_name(r.get('kind'))} {target}{part} {_age(r.get('at'), now)}")
    try:
        L += ["status: " + _status_line(s) for s in daily.status_lines(out)]
    except Exception as e:
        L.append("status: 読めませんでした（" + type(e).__name__ + "）")
    log = _jsonl(out / "daily.log.jsonl")
    reports = [r for r in log if r.get("step") == "cycle" and isinstance(r.get("report"), dict)][-cycles:]
    holds = sum(1 for r in log if r.get("step") == "brief" and r.get("status") == "delivery_unknown_hold")
    L.append(f"直近 {len(reports)} サイクル（古い順。手順 -> ok / error(例外の種類)）。朝のまとめを保留した記録 {holds} 回")
    skip = {"perf", "busy", "full_text_delete_failed"}
    for r in reports:
        rep = r["report"]
        at = str(r.get("at", ""))[5:16].replace("T", " ")
        parts = [f"{_name(k)}={_cycle_value(v)}" for k, v in rep.items() if k not in skip]
        if rep.get("busy"):
            parts.append("busy=" + ",".join(_name(k) for k in rep["busy"]))
        L.append(f"- {at} " + " ".join(parts))
    warns = _jsonl(out / "warnings.jsonl", last=20)
    keys = {}
    for w in warns:
        for k in w:
            if k != "at":
                keys[_name(k)] = keys.get(_name(k), 0) + 1
    L.append("warnings.jsonl の直近 20 件の項目: " + (", ".join(f"{k} {v}" for k, v in sorted(keys.items())) or "なし"))
    return L


# ---------------------------------------------------------------- D. environment

def kev_state():
    url = config.value("kev_url") or "http://127.0.0.1:8009/v1"
    host = urllib.parse.urlparse(url).hostname or ""
    from . import backends
    if host not in backends.LOOPBACK:
        return "この PC 以外（確認しません）"
    try:
        backends._DIRECT.open(urllib.request.Request(url.rstrip("/") + "/models"), timeout=3).close()
        return "up"
    except Exception:
        return "down"


def section_d(teams_version=None, kev=None):
    from . import __version__, stats
    tv = teams_version if teams_version and re.match(r"^[0-9.]{1,30}$", teams_version) else "不明"
    kev = kev if kev is not None else kev_state()
    return ["== D. 環境",
            f"kimeru {__version__} ({stats.build_id()}) / Python {platform.python_version()} / Windows {_name(platform.version())} / "
            f"Teams {tv} / 判断役 {_name(config.value('backend'))} / Kev {kev} / writer {_name(config.value('writer')) if config.value('writer') else '未設定'}"]


# ---------------------------------------------------------------- the sheet

def build(out, now=None, probe=None, probe_note="", teams_version=None, kev=None):
    now = now or datetime.now(timezone.utc)
    head = f"kimeru 診断 {now.astimezone():%Y-%m-%d %H:%M}（何も送っていません。本文・題名・名前・パスは含みません）"
    L = [head, ""]
    for part in (lambda: section_d(teams_version, kev), lambda: section_a(out, now, probe, probe_note),
                 lambda: section_b(out), lambda: section_c(out, now)):
        try:
            L += part()
        except Exception as e:   # one broken record must not cost the rest of the sheet
            L.append(f"（この節を作れませんでした: {type(e).__name__}）")
        L.append("")
    return L


DRIVES = (("G:\\", "マイドライブ"), ("G:\\", "My Drive"), ("~", "Google Drive"), ("~", "My Drive"))


def drive_folders():
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return [Path(home if base == "~" else base) / sub / "kimeru-release" for base, sub in DRIVES]


def dispatch(argv, out, print_fn=print, bridge=None, folders=None):
    p = argparse.ArgumentParser(prog="kimeru diagnose")
    p.add_argument("--write", help="also write the sheet to this file (UTF-8)")
    p.add_argument("--no-probe", action="store_true", help="do not read the self chat")
    p.add_argument("--teams-version", default=None)
    p.add_argument("--drive", action="store_true", help="copy the file into a Google Drive folder named kimeru-release")
    try:
        a = p.parse_args(list(argv))
    except SystemExit as e:
        return int(e.code or 0)
    out = Path(out)
    probe, note = None, ""
    if a.no_probe:
        note = "実行しませんでした（--no-probe）"
    elif os.name != "nt" and bridge is None:
        note = "実行しませんでした（Windows 以外）"
    else:
        probe, note = run_probe(out, bridge)
    lines = build(out, probe=probe, probe_note=note, teams_version=a.teams_version)
    text = "\r\n".join(lines) + "\r\n"
    for line in lines:
        print_fn(line)
    if a.write:
        Path(a.write).write_text(text, encoding="utf-8-sig", newline="")
        if a.drive:
            for d in (folders if folders is not None else drive_folders()):
                if d.is_dir():
                    try:
                        (d / RESULT_FILE).write_text(text, encoding="utf-8-sig", newline="")
                        print_fn(f"Google ドライブへコピーしました: {d / RESULT_FILE}")
                    except OSError as e:
                        print_fn(f"Google ドライブへのコピーに失敗しました: {type(e).__name__}")
                    break
            else:
                print_fn("Google ドライブの kimeru-release フォルダは見つかりませんでした（コピーしていません）")
    return 0
