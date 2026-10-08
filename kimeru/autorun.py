"""Trial autopilot: the scheduled run (自動運転) switched on for a limited time, and one result sheet afterwards.

  `schedule trial-start`  records the window in <data>/trial-autorun.json (setup-managed.cmd trial calls it) and can drop
                          one fictional ADO work item that lacks information, so the first cycle posts a confirmation
  `schedule report`       reads daily.log.jsonl / decisions.jsonl / approvals.json / approvals.log.jsonl / notices.jsonl
                          and writes numbers, OK/NG and short codes: no message text, title, name, organization or path

All times are local and naive (the form daily.log.jsonl uses); the records that carry a UTC offset are converted.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median

from . import config, fsutil

FILE = "trial-autorun.json"
SAMPLE_ID = "999001"               # the fictional work item; it is left out of every real count
SAMPLE_TITLE = "試験: 内容が未記入のサンプル"
KINDS = {"teams.chat": "teams", "ado.workitem.created": "ado", "monitor.alert": "alert", "meeting.item": "meeting"}
REPLY_NAMES = {"approved": "OK", "rejected": "NG", "held": "保留", "redrafted": "修正", "ask_back": "聞き返し"}
STAMP = "%Y-%m-%dT%H:%M:%S"


def path(out):
    return Path(out) / FILE


def _local(value):
    """A record time as a naive local datetime (None when it cannot be read). UTC-offset times are converted."""
    if not value:
        return None
    try:
        t = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return t.astimezone().replace(tzinfo=None) if t.tzinfo else t


def _rows(p):
    rows = []
    try:
        with Path(p).open(encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict):
                    rows.append(r)
    except OSError:
        pass
    return rows


def read_trial(out):
    d = fsutil.read_json(path(out), {})
    return d if isinstance(d, dict) else {}


def is_sample(event_kind, event_id):
    return event_kind == "ado.workitem.created" and str(event_id) == SAMPLE_ID


def _key_is_sample(key):
    """An approval or a processed key of the fictional item: "<graph>:<id>:<node>" (approvals) or "<graph>@<v>:<kind>:<id>"."""
    parts = str(key or "").split(":")
    return (len(parts) >= 3 and parts[1] == SAMPLE_ID and "@" not in parts[0]) or str(key or "").endswith(f":ado.workitem.created:{SAMPLE_ID}")


# ---- start of a trial -------------------------------------------------------------------------------------------------

def _processed(out):
    p = Path(out) / "processed.txt"
    try:
        return p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def drop_sample(out, inbox, org="", project=""):
    """Put the one fictional work item into the inbox. Returns "dropped" or "already" (a trial before already judged it: it is
    not judged twice, so nothing is added)."""
    if any(_key_is_sample(k) for k in _processed(out)):
        return "already"
    from . import pull
    payload = {"eventType": "workitem.created", "resource": {"id": int(SAMPLE_ID), "fields": {
        "System.WorkItemType": "Bug", "System.Title": SAMPLE_TITLE,
        "System.CreatedDate": datetime.now().astimezone().isoformat(timespec="seconds"),
        "System.CreatedBy": {"displayName": "kimeru sample"}}}}
    if org and project:
        payload["kimeru_origin"] = {"org": org, "project": project}
    pull._drop(inbox, f"ado-sample-{SAMPLE_ID}", payload)
    return "dropped"


def start_trial(out, hours=8, minutes=5, ado=False, sample=False, org="", project="", now=None, inbox=None):
    """Record the window and (with sample) drop the fictional item. Returns the record written."""
    now = (now or datetime.now()).replace(microsecond=0)
    out = Path(out)
    hours = int(hours) if float(hours).is_integer() else float(hours)
    rec = {"start": now.strftime(STAMP), "end": (now + timedelta(hours=hours)).strftime(STAMP), "hours": hours,
           "minutes": minutes, "ado": bool(ado), "sample": "no", "sample_id": SAMPLE_ID}
    if sample:
        rec["sample"] = drop_sample(out, inbox or out / "inbox", org if ado else "", project if ado else "")
    out.mkdir(parents=True, exist_ok=True)
    fsutil.write_atomic(path(out), json.dumps(rec, ensure_ascii=False, indent=1) + "\n")
    return rec


def mark_ended(out, how, now=None):
    """Record that the trial is over (timer | manual); the report then ends there. Returns the record or None."""
    rec = read_trial(out)
    if not rec or rec.get("ended"):
        return rec or None
    rec["ended"] = (now or datetime.now()).replace(microsecond=0).strftime(STAMP)
    rec["ended_by"] = how
    fsutil.write_atomic(path(out), json.dumps(rec, ensure_ascii=False, indent=1) + "\n")
    return rec


def overdue(out, now=None):
    """True when a trial is recorded, not ended, and its planned end has passed (the PC was off or asleep at that time)."""
    rec = read_trial(out)
    end = _local(rec.get("end")) if rec else None
    return bool(rec and not rec.get("ended") and end and (now or datetime.now()) >= end)


def start_notes(rec):
    """The plain Japanese statement of what the trial will do (printed by setup-managed.cmd trial)."""
    end = _local(rec["end"])
    lines = [
        "",
        "試験運転を始めました。これから起きること:",
        f"・{rec['minutes']} 分ごとに、判断が必要な件を自分とのチャット（Teams）へ投稿します。相手のいるチャットへは何も送りません",
        f"・{end:%m/%d %H:%M}（{rec['hours']} 時間後）に自分で止まります。止まるときに自動運転と Kev の自動起動を外し、結果ファイル（kimeru-autorun-result.txt）を作ってクリップボードへ入れます",
        "  この PC が止まっていた場合は、動き出し次第止まります。それでも止まらないときは、次に setup-managed.cmd status か report を実行した時点で止めます",
        "・途中でやめるときは  setup-managed.cmd remove  （結果ファイルもそのとき作ります）",
        "・ADO へは何も書きません（コメントも更新も、承認しても書かない既定のままのとき。設定 execute を有効にしている場合を除く）",
    ]
    if rec.get("ado"):
        lines.append("・ADO は読むだけです（新しい作業項目を取り込みます）")
    if rec.get("sample") == "dropped":
        lines.append("・試験用に、架空の作業項目 1 件（番号 " + SAMPLE_ID + "、題名は「試験:」で始まる）を入れました。最初のサイクルで確認待ちとして投稿されます。内容を確かめて「NG 番号」と返信すると閉じます。結果ファイルの件数には入りません")
    elif rec.get("sample") == "already":
        lines.append("・試験用の作業項目は、以前の試験で取り込み済みのため、今回は入れていません")
    lines.append("・作業中に画面が切り替わらないか、スマホに投稿が届くかを、目で見て確かめてください（結果ファイルの末尾に確認の一覧があります）")
    return lines


# ---- the report -------------------------------------------------------------------------------------------------------

def _in(t, lo, hi):
    return t is not None and (lo is None or t >= lo) and (hi is None or t <= hi)


def _line(name, mark, text):
    return f"{name:<9}{mark:<3}{text}"


def _count_text(counts, order=None, empty="なし"):
    keys = list(order) if order else sorted(counts)
    shown = [f"{k} {counts[k]}" for k in keys if counts.get(k)]
    return " / ".join(shown) or empty


def build_report(out, since=None, until=None, now=None):
    """The result sheet as a list of lines. since/until: naive local datetimes (default: the recorded trial window)."""
    from . import __version__, stats
    from . import brief as brief_mod, execute
    out = Path(out)
    now = (now or datetime.now()).replace(microsecond=0)
    trial = read_trial(out)
    t_start, t_end = _local(trial.get("start")), _local(trial.get("end"))
    ended = _local(trial.get("ended"))
    minutes = int(trial.get("minutes") or 5)
    lo = since or t_start
    hi = until or ended or (min(now, t_end) if t_end else now)
    log = _rows(out / "daily.log.jsonl")
    log_rows = [(r, _local(r.get("at"))) for r in log]
    if lo is None and log_rows:
        lo = min((t for _, t in log_rows if t), default=None)
    win = [(r, t) for r, t in log_rows if _in(t, lo, hi)]
    cycles = [(r, t) for r, t in win if r.get("step") == "cycle" and isinstance(r.get("report"), dict)]
    starts = sorted(t for r, t in win if r.get("step") == "config" and t)

    # ---- cycles
    expected = max(1, int((hi - lo).total_seconds() // (minutes * 60))) if lo and hi else 0
    durations = []
    for r, t in cycles:
        before = [s for s in starts if s <= t and (t - s).total_seconds() < 1800]
        if before:
            durations.append((t - before[-1]).total_seconds())
    limit_hit = sum(1 for r, t in win if r.get("step") == "judge" and "over_budget" in r)
    kev_down = sum(1 for r, t in win if r.get("step") == "judge" and "waiting" in r)
    failed, busy = {}, {}
    for r, t in cycles:
        rep = r["report"]
        for k, v in rep.items():
            if isinstance(v, str) and v.startswith("error:") and k != "brief":
                failed[k] = failed.get(k, 0) + 1
        for k in (rep.get("busy") or {}):
            busy[k] = busy.get(k, 0) + 1
    brief_errors = sum(1 for r, t in cycles if isinstance(r["report"].get("brief"), str))

    # ---- decisions
    kinds, outcome, p1, info_req, ado_p = {}, {"自動": 0, "PM へ": 0, "通知": 0}, 0, 0, {}
    sample_seen = False
    for r in _rows(out / "decisions.jsonl"):
        if not _in(_local(r.get("at")), lo, hi):
            continue
        if is_sample(r.get("event_kind"), r.get("event_id")):
            sample_seen = True
            continue
        k = KINDS.get(r.get("event_kind"), "その他")
        kinds[k] = kinds.get(k, 0) + 1
        if r.get("needs_human"):
            outcome["PM へ"] += 1
        elif r.get("notify"):
            outcome["通知"] += 1
        else:
            outcome["自動"] += 1
        if r.get("node") == "request_info":
            info_req += 1
        if r.get("event_kind") == "ado.workitem.created" and r.get("outcome") == "decide" and not r.get("needs_human") and not r.get("notify"):
            code = brief_mod._ado_outcome(r)
            if code in ("P2", "P3"):
                ado_p[code] = ado_p.get(code, 0) + 1
    for r in _rows(out / "notices.jsonl"):
        if _in(_local(r.get("at")), lo, hi) and not is_sample(r.get("event_kind"), r.get("event_id")):
            p1 += 1

    # ---- approvals (self-chat posts) and replies
    ap = fsutil.read_json(out / "approvals.json", {"items": {}})
    items = ap.get("items", {}) if isinstance(ap, dict) else {}
    sent = sample_posted = 0
    unknown = 0
    by_key = {}
    for it in items.values():
        if not isinstance(it, dict):
            continue
        rec = it.get("record") or {}
        by_key[it.get("key")] = it
        if it.get("delivery_unknown"):
            unknown += 1
        if not _in(_local(rec.get("at")), lo, hi):
            continue
        if is_sample(rec.get("event_kind"), rec.get("event_id")):
            sample_posted += bool(it.get("posted"))
            continue
        sent += bool(it.get("posted"))
    nu = ap.get("notice_delivery_unknown", {}) if isinstance(ap, dict) else {}
    unknown += len(nu) if isinstance(nu, (list, dict)) else 0
    unknown += 1 if isinstance(ap, dict) and ap.get("outbox_delivery_unknown") else 0
    state = fsutil.read_json(out / "daily_state.json", {})
    unknown += 1 if isinstance(state, dict) and state.get("brief_delivery_unknown") else 0

    replies, ready, sample_reply = {}, 0, ""
    for r in _rows(out / "approvals.log.jsonl"):
        if not _in(_local(r.get("at")), lo, hi):
            continue
        word = REPLY_NAMES.get(r.get("status"))
        if _key_is_sample(r.get("key")):
            sample_reply = word or "その他"
            continue
        replies[word or "その他"] = replies.get(word or "その他", 0) + 1
        if r.get("status") == "approved" and config.value("send_ready_post") != "0":
            acts = ((by_key.get(r.get("key")) or {}).get("record") or {}).get("actions") or []
            if any(a.get("type") in execute.SEND_READY and str(a.get("text", "")).strip() for a in acts if isinstance(a, dict)):
                ready += 1

    # ---- Teams screen
    opened = sum(int((r["report"].get("perf") or {}).get("chats_opened", 0)) for r, t in cycles
                 if isinstance(r["report"].get("perf"), dict))
    restore_failed = moved_self = 0
    for r, t in win:
        if r.get("step") == "read_restore":
            al = r.get("alerts") or []
            restore_failed += "failed" in al
            moved_self += "self" in al
    for r in _rows(out / "decisions.jsonl"):
        rf = r.get("read_full") or {}
        if _in(_local(r.get("at")), lo, hi) and rf.get("returned") is False and rf.get("restore") not in ("self", "failed"):
            restore_failed += 1   # an older record that says only "not put back"
    pc_in_use = teams_in_use = 0
    for r, t in win:
        if r.get("step") == "judge" and r.get("read_budget"):
            s = str(r["read_budget"])
            pc_in_use += "キーボード" in s
            teams_in_use += "前面" in s

    # ---- brief per day
    days = []
    if lo and hi:
        d = lo.date()
        while d <= hi.date():
            days.append(d)
            d += timedelta(days=1)
    try:
        brief_hour = config.int_value("brief_hour")
    except Exception:
        brief_hour = 8
    brief_lines = []
    for d in days:
        mine = [r["report"].get("brief") for r, t in cycles if t.date() == d and "brief" in r["report"]]
        if any(isinstance(b, int) for b in mine):
            brief_lines.append(f"{d:%m/%d} 投稿")
        elif any(isinstance(b, str) for b in mine):
            brief_lines.append(f"{d:%m/%d} NG")
        elif hi >= datetime(d.year, d.month, d.day, brief_hour) and lo <= datetime(d.year, d.month, d.day, 23, 59):
            brief_lines.append(f"{d:%m/%d} 未投稿")
        else:
            brief_lines.append(f"{d:%m/%d} 対象外（{brief_hour} 時前）")

    # ---- verdicts
    n_cycles = len(cycles)
    verdicts = []
    verdicts.append(("cycles", "OK" if n_cycles else "NG", "サイクルが動いた" if n_cycles else "サイクルが 1 回も動いていない"))
    nf = sum(failed.values())
    verdicts.append(("steps", "NG" if nf else "OK", f"失敗した手順あり（延べ {nf} 回）" if nf else "失敗した手順なし"))
    kev_ng = n_cycles and kev_down * 2 > n_cycles
    verdicts.append(("judge", "NG" if kev_ng else "OK",
                     f"判断役（Kev）が使えなかったサイクルが半数を超えた（{kev_down}/{n_cycles}）" if kev_ng else "判断役（Kev）は半数以上のサイクルで使えた"))
    verdicts.append(("delivery", "NG" if unknown else "OK", f"配信結果不明 {unknown} 件" if unknown else "配信結果不明なし"))
    verdicts.append(("teams", "NG" if restore_failed else "OK", f"Teams の表示を戻せなかった {restore_failed} 回" if restore_failed else "Teams の表示を戻せなかったことはない"))
    ng = sum(1 for v in verdicts if v[1] == "NG")

    span = f"{lo:%m/%d %H:%M} - {hi:%m/%d %H:%M}" if lo and hi else "不明"
    head = f"kimeru 試験運転の結果 {now:%Y-%m-%d %H:%M}  NG={ng}"
    L = [head, ""]
    L.append(_line("build", "", f"kimeru {__version__} ({stats.build_id()})"))
    how = {"timer": "予定の時刻に自動で停止", "manual": "手で停止"}.get(trial.get("ended_by"), "")
    L.append(_line("window", "", f"{span}  間隔 {minutes} 分  ADO {'あり' if trial.get('ado') else 'なし'}" + (f"  {how}" if how else "")))
    ratio = f"{n_cycles} / 見込み {expected}"
    low = expected and n_cycles * 2 < expected
    L.append(_line("run", "注意" if low and n_cycles else "", f"実行 {ratio}" + ("（半分未満。PC が止まっていた時間があるかもしれません）" if low and n_cycles else "")))
    if durations:
        L.append(_line("time", "", f"1 サイクルの所要 中央値 {median(durations):.0f} 秒 / 最大 {max(durations):.0f} 秒"))
    else:
        L.append(_line("time", "", "所要時間: 記録なし"))
    L.append(_line("limit", "", f"時間切れ（持ち越しあり）のサイクル {limit_hit}"))
    L.append(_line("kev", "", f"判断役（Kev）が使えなかったサイクル {kev_down}"))
    L.append(_line("failed", "", "失敗した手順: " + (", ".join(f"{k} {v}" for k, v in sorted(failed.items())) or "なし")))
    L.append(_line("busy", "", "他の実行と重なり見送った手順: " + (", ".join(f"{k} {v}" for k, v in sorted(busy.items())) or "なし")))
    L.append("")
    L.append(_line("events", "", "取り込み: " + _count_text(kinds, ["teams", "ado", "alert", "meeting", "その他"])))
    L.append(_line("decide", "", "判断: " + _count_text(outcome, ["自動", "PM へ", "通知"])))
    L.append(_line("notice", "", f"P1 通知 {p1} / 情報不足の確認待ち {info_req}"))
    L.append(_line("ado", "", "ADO の自動 " + (_count_text(ado_p, ["P2", "P3"]) if ado_p else "P2・P3 なし")))
    L.append("")
    L.append(_line("posts", "", f"自分とのチャットへ投稿した確認待ち {sent} / 配信結果不明 {unknown}"))
    L.append(_line("replies", "", "読み取った返信: " + _count_text(replies, ["OK", "NG", "保留", "修正", "聞き返し", "その他"])))
    L.append(_line("ready", "", f"送信用の文面を投稿した（承認からの推定）{ready}"))
    if trial.get("sample") in ("dropped", "already") or sample_seen or sample_posted:
        s_ok = "投稿済み" if sample_posted else ("判断済み・未投稿" if sample_seen else "未判断")
        L.append(_line("sample", "", f"試験用の作業項目: {s_ok} / 返信 {sample_reply or 'なし'}（上の件数には入れていません）"))
    L.append("")
    L.append(_line("screen", "", f"開いたチャット {opened} / 戻せなかった {restore_failed} / 自分のチャットへ移した {moved_self}"))
    L.append(_line("screen", "", f"使用中で見送った: キーボード・マウス {pc_in_use} / Teams 前面 {teams_in_use}"))
    L.append(_line("screen", "", "Teams を前面に出した回数: 記録なし"))
    L.append(_line("brief", "", "朝のまとめ: " + (" / ".join(brief_lines) or "対象の日なし") + (f"（失敗 {brief_errors}）" if brief_errors else "")))
    L.append("")
    for name, mark, text in verdicts:
        L.append(_line(name, mark, text))
    first = min((t for _, t in log_rows if t), default=None)
    if lo and first and first > lo + timedelta(minutes=2 * minutes):
        L.append(_line("log", "注意", "記録の先頭が試験の開始より後（古い分が整理された可能性。サイクル数は少なめに出ます）"))
    L += ["", "目で確かめるもの:",
          "  ・作業中に、画面が勝手に切り替わらなかったか（Teams が前に出る・入力が奪われる）",
          "  ・スマホ（Teams）に、自分とのチャットの投稿が届いたか。届くまでの時間は",
          "  ・返信（OK / NG / 保留 + 番号）が次のサイクルで読まれたか",
          "  ・朝のまとめが届いたか"]
    return L


def write_report(out, dest=None, since=None, until=None, now=None):
    lines = build_report(out, since=since, until=until, now=now)
    if dest:
        Path(dest).write_text("\r\n".join(lines) + "\r\n", encoding="utf-8-sig")
    return lines
