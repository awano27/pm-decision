"""R21 (learn=1): a threshold proposal for ONE question, sent as an approval card.

calibrate.py tunes two coefficients per judge. A question that is systematically wrong (confident and wrong) cannot be fixed
that way. Here a single question's confidence bar (or its unsure band) is proposed instead, from the PM's own reviewed decisions
(labels from `kimeru review` and the replies 違う N / 違う N k). The proposal is a card in the approvals (queue.jsonl, notify
posts it): only an OK on a run with --send, from the daily or approvals path, writes the local entry (thresholds.json "nodes",
laid over one question by profiles.for_node). Nothing in the graphs, the processed records or the decisions changes.

  - a proposal only makes a question more careful: a higher bar, or a wider unsure band (never a wider automatic decision);
  - the search half of the labels picks it, the confirming half must not gain confident errors;
  - at most two cards a week, none while a card waits, and an NG keeps the question quiet for 28 days;
  - a card is never a case: brief, work, stats, trial, report and requirements do not count it (is_card);
  - with the judge Jev no count is shown anywhere (TypeSafe's terms): the card, the ledger and `learn list`.

`kimeru learn list` shows the cards that wait and the ledger; `kimeru learn revert L<n>` takes an adopted entry back.
The ledger is state/learned.jsonl (adoptions and reverts, never deleted; counts only for judges other than Jev).
R33 (learn=1): a `不要 N` answer (NG, and not needed) for the same first sentence of a minutes line three times on two days
makes one card a day; an OK writes out/learned_rules.json (`kimeru learn revert A<n>` takes it back). A line of that sentence whose
kind was unsure is then recorded only (log.only, demoted_by); see demote().
"""
import hashlib
import json
import re
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

from . import config, fsutil, profiles

KIND = "kimeru.learn"
PREFIX = KIND + ":"
MIN_QUESTIONS = 30          # labels for one question before anything is proposed for it
CONF_UP = (1.1, 1.2, 1.3, 1.5)   # the confidence bar, only up (more to a person)
NOUL_UP = (1.05, 1.1, 1.2)       # the unsure band of a yes/no question, only wider
REJECT_DAYS = 28
PER_WEEK = 2
LEDGER = "learned.jsonl"
DEFAULT_BAR = {"conf_scale": 1.0, "noul_scale": 1.0}
LABELS = {"intent": "依頼の種類", "priority": "優先度", "impact": "影響の大きさ", "item_type": "項目の種類",
          "horizon": "時期の見通し"}


def enabled():
    return config.value("learn") == "1"


def ledger_path():
    return config.state_dir() / LEDGER


def is_card_key(key):
    """True for the key of a card (the approval log keeps the key of each reply)."""
    return isinstance(key, str) and key.startswith(PREFIX)


def is_card(x):
    """True for an approval item (or a queue row) that is a card of this kind, never for a case."""
    if not isinstance(x, dict):
        return False
    key = x.get("key")
    if isinstance(key, str) and key.startswith(PREFIX):
        return True
    rec = x.get("record") if isinstance(x.get("record"), dict) else x
    return rec.get("graph") == KIND


def cases(items):
    """The approval items without the cards: what counts as a case (brief, work, report, trial, stats, requirements)."""
    if not isinstance(items, dict):
        return {}
    return {n: it for n, it in items.items() if not is_card(it)}


def _naive(t):
    if t is None:
        return None
    return t.astimezone().replace(tzinfo=None) if t.tzinfo else t


def _iso(t):
    return _naive(t).isoformat(timespec="seconds")


def _parse(s):
    try:
        return datetime.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None


def _n(x):
    """1.0 -> '1.0', 1.2 -> '1.2', 1.05 -> '1.05'."""
    s = f"{float(x):.2f}"
    return s[:-1] if s.endswith("0") else s


def _ledger():
    try:
        text = ledger_path().read_text(encoding="utf-8")
    except OSError:
        return []
    rows = []
    for line in text.splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict):
            rows.append(r)
    return rows


def _ledger_add(row):
    p = ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def current_bar(key):
    """The question's own entry as it is in the file now (1.0 when there is none)."""
    o = profiles.node_overrides().get(key) or {}
    return {"conf_scale": float(o.get("conf_scale", 1.0)), "noul_scale": float(o.get("noul_scale", 1.0))}


def _write_node(key, values):
    """Set (values) or remove (None) one question's entry; every other key of the file stays as it is."""
    data = profiles.thresholds_raw()
    nodes = data.get(profiles.NODE_KEY) if isinstance(data.get(profiles.NODE_KEY), dict) else {}
    if values is None:
        nodes.pop(key, None)
    else:
        nodes[key] = values
    if nodes:
        data[profiles.NODE_KEY] = nodes
    else:
        data.pop(profiles.NODE_KEY, None)
    p = profiles.override_path()
    if data:
        p.parent.mkdir(parents=True, exist_ok=True)
        fsutil.write_atomic(p, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    elif p.exists():
        p.unlink()


def _change(bar_before, bar_after):
    parts = []
    if bar_after["conf_scale"] != bar_before["conf_scale"]:
        parts.append(f"確信の基準を {_n(bar_before['conf_scale'])}→{_n(bar_after['conf_scale'])} 倍に")
    if bar_after["noul_scale"] != bar_before["noul_scale"]:
        parts.append(f"判断に迷う幅を {_n(bar_before['noul_scale'])}→{_n(bar_after['noul_scale'])} 倍に")
    return "、".join(parts) or "変えません"


def _question(node_key):
    node_id = node_key.partition(":")[2]
    return f"「{LABELS.get(node_id, node_id)}」の質問"


def _signed(d):
    return f"{d:+d}"


def _counts(L):
    """The search and confirming numbers as one short line; '' when the card carries none (judge Jev)."""
    c = L.get("counts")
    if not isinstance(c, dict):
        return ""
    s, f = c["search"], c["confirm"]
    return (f"探した側 {s['questions']} 問: 確信して間違えた {s['wrong'][0]}→{s['wrong'][1]}、"
            f"人に回る {_signed(s['human'][1] - s['human'][0])}")


def propose_node(qs, node, key, judge, base, cur):
    """A more careful bar for one question, or None. qs: the labeled questions of this question (calibrate.labeled_questions
    rows); base: the judge's profile with its file coefficients (profiles.effective); cur: the question's own entry now."""
    from . import calibrate
    if len(qs) < MIN_QUESTIONS:
        return None
    a = [q for q in qs if calibrate._half(q) == 0]
    b = [q for q in qs if calibrate._half(q) == 1]
    if not a or not b:
        return None
    cc, cn = float(cur["conf_scale"]), float(cur["noul_scale"])
    cands = [(c, n) for c in [cc] + [x for x in CONF_UP if x > cc] for n in [cn] + [x for x in NOUL_UP if x > cn]
             if (c, n) != (cc, cn)]
    if not cands:
        return None

    def score(part, c, n):   # the totals the route really uses: the judge's coefficients times the question's own
        return calibrate._score(part, base["conf_scale"] * c, base["noul_scale"] * n)

    best = min(cands, key=lambda k: (score(a, *k)["wrong"], score(a, *k)["human"], k))
    base_a, best_a = score(a, cc, cn), score(a, *best)
    if best_a["wrong"] >= base_a["wrong"]:
        return None
    base_b, best_b = score(b, cc, cn), score(b, *best)
    if best_b["wrong"] > base_b["wrong"]:   # the confirming half must not gain confident errors
        return None
    return {"node": key, "judge": judge, "before": {"conf_scale": cc, "noul_scale": cn},
            "after": {"conf_scale": best[0], "noul_scale": best[1]},
            "search": {"questions": len(a), "wrong": [base_a["wrong"], best_a["wrong"]],
                       "human": [base_a["human"], best_a["human"]]},
            "confirm": {"questions": len(b), "wrong": [base_b["wrong"], best_b["wrong"]]}}


def _waiting(it):
    return is_card(it) and (it.get("status") in ("pending", "held") or it.get("learn_state") == "pending")


def _recent_rejects(items, now):
    keys = set()
    for it in items.values():
        if not is_card(it) or it.get("learn_state") != "rejected":
            continue
        t = _parse(it.get("learn_rejected_at"))
        key = ((it.get("record") or {}).get("learn") or {}).get("node")
        if t and key and now - _naive(t) < timedelta(days=REJECT_DAYS):
            keys.add(key)
    return keys


def _queue_rows(out):
    """The rows of queue.jsonl that read back (a row that does not parse is no row)."""
    try:
        lines = (Path(out) / "queue.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line) if line.strip() else None
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _unposted_card(out, items):
    """A card that is queued but not yet in the approvals (notify has not seen it): it waits as well."""
    keys = {it.get("key") for it in items.values()}
    return any(is_card(r) and f"{r.get('graph')}:{r.get('event_id')}:{r.get('node')}" not in keys for r in _queue_rows(out))


def _numbers(out, items):
    """Every L-number already used (ledger, approval keys, queue rows): a new card never reuses one."""
    used = [0]
    for r in _ledger():
        m = re.match(r"^L(\d+)$", str(r.get("id", "")))
        if m:
            used.append(int(m.group(1)))
    for it in items.values():
        m = re.match(r"^kimeru\.learn:L(\d+):", str(it.get("key", "")))
        if m:
            used.append(int(m.group(1)))
    for r in _queue_rows(out):
        m = re.match(r"^L(\d+)$", str(r.get("event_id", "")))
        if m:
            used.append(int(m.group(1)))
    return max(used) + 1


def _card(lid, p):
    L = {"node": p["node"], "judge": p["judge"], "before": p["before"], "after": p["after"]}
    if p["judge"] != "Jev":   # Jev's counts are not shown anywhere (TypeSafe's terms)
        L["counts"] = {"search": p["search"], "confirm": p["confirm"]}
    return {"graph": KIND, "event_kind": KIND, "event_id": lid, "node": "threshold", "outcome": "advise",
            "needs_human": True, "advice": f"学習の提案 {lid}", "actions": [], "learn": L}


def offer(out, graphs, backend, now=None):
    """Once a week (learn=1, the daily path only): queue at most PER_WEEK cards for questions that the labels show to be
    answered confidently and wrongly. Returns {"state": ...} and writes the card rows to queue.jsonl (notify posts them)."""
    if not enabled():
        return {"state": "off"}
    from . import backends, calibrate, notify
    judge = backends.judge_name(backend)
    pname = profiles.JUDGE_PROFILE.get(judge)
    if pname is None:
        return {"state": "no_profile"}
    now = _naive(now) or datetime.now()
    week = now.strftime("%G-W%V")
    out = Path(out)
    with fsutil.exclusive(notify.lock_path(out)) as got:
        if not got:
            return {"state": "busy"}
        st_path = out / "daily_state.json"
        st = fsutil.read_json(st_path, {})
        if not isinstance(st, dict):
            st = {}
        if st.get("learn_week") == week:
            return {"state": "this_week"}
        items = notify.Approvals(out).data.get("items", {})
        if any(_waiting(it) for it in items.values()) or _unposted_card(out, items):
            return {"state": "pending"}
        recent = _recent_rejects(items, now)
        by_name = {g["name"]: g for lst in graphs.values() for g in lst}
        groups = {}
        for q in calibrate.labeled_questions(out, graphs):
            if q.get("judge") == judge:
                groups.setdefault(f"{q['graph']}:{q['node_id']}", []).append(q)
        base = profiles.effective(profiles.PROFILES[pname])
        props = []
        for key in sorted(groups):
            gname, _, nid = key.partition(":")
            g = by_name.get(gname)
            if key in recent or not profiles.learnable(g, nid):
                continue
            p = propose_node(groups[key], g["nodes"][nid], key, judge, base, current_bar(key))
            if p:
                props.append(p)
            if len(props) >= PER_WEEK:
                break
        st["learn_week"] = week
        ids = []
        if props:
            k = _numbers(out, items)
            out.mkdir(parents=True, exist_ok=True)
            with (out / "queue.jsonl").open("a", encoding="utf-8") as f:
                for p in props:
                    lid = f"L{k}"
                    k += 1
                    ids.append(lid)
                    f.write(json.dumps(_card(lid, p), ensure_ascii=False) + "\n")
        fsutil.write_atomic(st_path, json.dumps(st, ensure_ascii=False, indent=1))
        return {"state": "queued", "ids": ids} if ids else {"state": "nothing"}


def on_status(it, new, now=None):
    """Keep the card's own state with the PM's answer: OK waits to be adopted, NG starts the 28 days."""
    if not is_card(it):
        return
    if new == "approved":
        it["learn_state"] = "pending"
    elif new == "rejected":
        it["learn_state"] = "rejected"
        it["learn_rejected_at"] = _iso(now or datetime.now())


def adopt(out, it, now=None, real=False, send=False):
    """Write the question's entry for an approved card. Only with real and send (the daily or approvals path, with --send);
    otherwise the card stays pending and the next such run adopts it. The bar must still be the one the card was made from."""
    if not is_card(it) or it.get("learn_state") != "pending":
        return None
    if not (real and send):
        return {"state": "pending"}
    rec = it.get("record") or {}
    L = rec.get("learn") or {}
    if L.get("rule"):   # R33: a rule's card writes out/learned_rules.json (_adopt_rule), not a threshold
        return _adopt_rule(out, it, rec, L, now)
    lid, key = rec.get("event_id"), L.get("node")
    before = {"conf_scale": float(L.get("before", {}).get("conf_scale", 1.0)), "noul_scale": float(L.get("before", {}).get("noul_scale", 1.0))}
    if not key or not isinstance(L.get("after"), dict) or current_bar(key) != before:
        it["learn_state"] = "stale"
        return {"state": "stale", "id": lid}
    after = {"conf_scale": float(L["after"]["conf_scale"]), "noul_scale": float(L["after"]["noul_scale"])}
    _write_node(key, after)
    row = {"id": lid, "kind": "adopt", "node": key, "judge": L.get("judge"), "before": before, "after": after,
           "adopted_at": _iso(now or datetime.now())}
    if "counts" in L:
        row["counts"] = L["counts"]
    _ledger_add(row)
    it["learn_state"] = "adopted"
    return {"state": "adopted", "id": lid, "node": key}


def revert(out, lid, now=None):
    """Take one adopted entry back: the question's bar returns to what it was before. "reverted", "none", "newer" or "busy".
    Only while the bar is still the one this adoption set: a later adoption of the same question (or a hand edit) would be
    lost otherwise, so that one is taken back first ("newer" changes nothing)."""
    from . import notify
    with fsutil.exclusive(notify.lock_path(Path(out))) as got:
        if not got:
            return "busy"
        rows = [r for r in _ledger() if r.get("id") == lid]
        if not rows or rows[-1].get("kind") != "adopt":
            return "none"
        row = rows[-1]
        key = row.get("node")
        before = row.get("before") or {}
        after = row.get("after") or {}
        held = {"conf_scale": float(after.get("conf_scale", 1.0)), "noul_scale": float(after.get("noul_scale", 1.0))}
        if current_bar(key) != held:
            return "newer"
        restore = {"conf_scale": float(before.get("conf_scale", 1.0)), "noul_scale": float(before.get("noul_scale", 1.0))}
        _write_node(key, None if restore == DEFAULT_BAR else restore)
        _ledger_add({"id": lid, "kind": "revert", "node": key, "reverted_at": _iso(now or datetime.now())})
        return "reverted"


def listing(out):
    """What `kimeru learn list` shows: the cards that wait, the ledger, and the entries the file has but kimeru does not read."""
    from . import notify
    lines = []
    items = notify.Approvals(Path(out)).data.get("items", {})
    for n, it in items.items():
        if _waiting(it):
            rec = it.get("record") or {}
            L = rec.get("learn") or {}
            if L.get("rule"):
                lines.append(f"待っている札 {rec.get('event_id')}（#{n}）: 「{L.get('phrase', '')}」を記録だけにする")
                continue
            lines.append(f"待っている札 {rec.get('event_id')}（#{n}）: {_question(L.get('node', ''))} "
                         f"{_change(L.get('before', DEFAULT_BAR), L.get('after', DEFAULT_BAR))}")
    for r in _ledger():
        if r.get("kind") == "adopt":
            b, a = r.get("before") or {}, r.get("after") or {}
            line = (f"採用 {r.get('id')}: {r.get('node')} 確信の基準 {_n(b.get('conf_scale', 1.0))}→{_n(a.get('conf_scale', 1.0))}、"
                    f"判断に迷う幅 {_n(b.get('noul_scale', 1.0))}→{_n(a.get('noul_scale', 1.0))}（{r.get('adopted_at')}）")
            c = r.get("counts")
            if isinstance(c, dict):
                line += f" 根拠: {_counts({'counts': c})}"
            lines.append(line)
        elif r.get("kind") == "revert":
            lines.append(f"取り消し {r.get('id')}: {r.get('node')}（{r.get('reverted_at')}）")
    lines.extend(_rule_lines(out))   # R33: the rules (learned_rules.json)
    data = profiles.thresholds_raw()
    if profiles.NODE_KEY in data:
        raw = data[profiles.NODE_KEY]
        if isinstance(raw, dict):
            readable = profiles.node_overrides()
            for key in sorted(raw, key=str):
                if key not in readable:
                    lines.append(f"無視している設定: {key}（値を読めないため、何も変えません）")
        else:   # the whole key is not an object (a number, a list, a string, null): nothing in it is read
            lines.append(f"無視している設定: {profiles.NODE_KEY}（値を読めないため、何も変えません）")
    return lines or ["待っている札も、採用した学習もありません。"]


def format_post(n, rec):
    """The approval post of a card: what it changes, what OK does, and how to undo it. Never a count for Jev."""
    L = rec.get("learn") or {}
    if L.get("rule"):   # R33: the card of a rule (_rule_post)
        return _rule_post(n, rec)
    lid = rec.get("event_id")
    change = _change(L.get("before", DEFAULT_BAR), L.get("after", DEFAULT_BAR))
    counts = _counts(L)
    head = f"[kimeru #{n}] 学習の提案: {_question(L.get('node', ''))}: {change}" + (f"（{counts}）" if counts else "")
    return "\n".join([head, f"OK で変わります（戻すには kimeru learn revert {lid}）。NG なら変わりません",
                      f"OK {n} / NG {n} / 詳細 {n}"])


def format_detail(n, rec):
    """The answer to `詳細 N` for a card: the numbers behind it (non-Jev), and the same answers as the post."""
    L = rec.get("learn") or {}
    if L.get("rule"):   # R33: the numbers behind a rule (_rule_detail)
        return _rule_detail(n, rec)
    lid = rec.get("event_id")
    c = L.get("counts")
    if isinstance(c, dict):
        s, f = c["search"], c["confirm"]
        basis = (f"探した側 {s['questions']} 問: 確信して間違えた {s['wrong'][0]}→{s['wrong'][1]}、人に回る {_signed(s['human'][1] - s['human'][0])}。"
                 f"確かめた側 {f['questions']} 問: 確信して間違えた {f['wrong'][0]}→{f['wrong'][1]}")
    else:
        basis = "件数は出しません（判断モデルが Jev のため）"
    return "\n".join([
        f"[kimeru 詳細 #{n}] 学習の提案 {lid}（判断モデル: {L.get('judge', '')}）",
        f"変えること: {_question(L.get('node', ''))}（{L.get('node', '')}）: "
        f"{_change(L.get('before', DEFAULT_BAR), L.get('after', DEFAULT_BAR))}",
        f"根拠: {basis}",
        f"OK で変わります（graph の JSON と処理済みの記録は変わりません）。NG なら変わりません。取り消し: kimeru learn revert {lid}",
        f"返信: OK {n} / NG {n} / 保留 {n}"])


def dispatch(argv, out, print_fn=print):
    """`kimeru learn list` and `kimeru learn revert L<n>`. Exit code: 0 done, 1 nothing to revert (or busy), 2 usage."""
    argv = list(argv or [])
    if not argv or argv == ["list"]:
        for line in listing(out):
            print_fn(line)
        return 0
    if len(argv) == 2 and argv[0] == "revert" and argv[1][:1] == "A":   # R33: a rule (A<n>)
        res = revert_rule(out, argv[1])
        print_fn({"reverted": f"{argv[1]} を取り消しました。その言い回しは、もう記録だけにしません",
                  "none": f"{argv[1]} という有効な規則はありません（取り消しは一度だけです）",
                  "busy": "approvals が別の処理で使われています。少し待って、もう一度実行してください"}[res])
        return 0 if res == "reverted" else 1
    if len(argv) == 2 and argv[0] == "revert":
        res = revert(out, argv[1])
        print_fn({"reverted": f"{argv[1]} を取り消しました。その質問の基準は元に戻りました",
                  "none": f"{argv[1]} という採用した学習はありません（取り消しは一度だけです）",
                  "newer": f"{argv[1]} のあとに、同じ質問の基準が変わっています。新しいものから順に取り消してください（何も変えません）",
                  "busy": "approvals が別の処理で使われています。少し待って、もう一度実行してください"}[res])
        return 0 if res == "reverted" else 1
    print_fn("使い方: kimeru learn list ／ kimeru learn revert L<番号>（または A<番号>）")
    return 2


# --- R33 (learn=1): a rule from the PM's 不要 N answers -------------------------------------------------------------------------
# `不要 N` is NG, and the PM did not need to see the line. Three of them on two days for the same first sentence of a minutes line
# whose kind was unsure, and no OK for that sentence, make one card a day. An OK writes out/learned_rules.json. After that a line of
# the same kind that begins with the sentence is recorded only (log.only, `demoted_by`), and the morning brief names it. Nothing else
# changes: no post, no ADO item, no request to anyone.
RULE_NODE = ("minutes-followup", "check_line")   # the only question a rule may decide: the kind of the line (item_type) was unsure
RULES = "learned_rules.json"
RULE_MARKS = ("？", "?", "ください", "お願い", "決め", "至急", "本日中")   # a request in the line: no rule decides it
RULE_GREETINGS = ("お疲れ様", "お疲れさま", "おはよう", "こんにちは", "こんばんは", "ご苦労様")
RULE_TEXT_FIELDS = ("text", "item", "description", "context", "condition")
RULE_NEVER_ACTIONS = ("oncall.page", "teams.post", "ado.comment")   # a line whose result has one of these is never recorded only
RULE_DAYS = 14           # the 不要 answers are counted this far back
RULE_MIN_TIMES = 3       # for one sentence ...
RULE_MIN_DAYS = 2        # ... on at least this many days
RULE_NG_DAYS = 30        # after an NG on its card the sentence is not offered again for this many days
RULE_UNUSED_DAYS = 60    # a rule not used (or adopted) for this many days stops applying
RULE_FOLLOWUP_HOURS = 24  # a new line from the chat of a recorded-only line within this many hours stops its rule
RULE_BRIEF_MAX = 5


def _nfkc(text):
    return unicodedata.normalize("NFKC", " ".join(str(text or "").split()))


def phrase_of(event):
    """The phrase of a free-text event: its first sentence (a greeting sentence is skipped), NFKC, each run of digits turned into a
    digit pattern, and 4 to 30 characters long; None when there is none. The group prefix (the author's name and a colon) goes first."""
    text = _nfkc(event.get("text") or event.get("item"))
    author = _nfkc(event.get("author"))
    if author and text.startswith(author + ":"):
        text = text[len(author) + 1:].lstrip()
    for sent in re.split(r"(?<=[。!?])", text):
        sent = sent.strip().rstrip("。!?.")
        if not sent or sent.startswith(RULE_GREETINGS):
            continue
        phrase = re.sub(r"\d+", lambda m: "\\d+", sent)
        return phrase if 4 <= len(phrase) <= 30 else None
    return None


def _node_key():
    return f"{RULE_NODE[0]}:{RULE_NODE[1]}"


def _rule_hash(phrase):
    return hashlib.sha1(f"{_node_key()}|{phrase}".encode("utf-8")).hexdigest()[:12]


def rules_path(out):
    return Path(out) / RULES


def _rules_lock(out):
    return Path(out) / "learn.lock"


def _rules_read(out):
    """The rules the file has (a missing or damaged file has none)."""
    data = fsutil.read_json(rules_path(out), {})
    rows = data.get("rules") if isinstance(data, dict) else None
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _rules_write(out, rows):
    p = rules_path(out)
    p.parent.mkdir(parents=True, exist_ok=True)
    fsutil.write_atomic(p, json.dumps({"rules": rows}, ensure_ascii=False, indent=1) + "\n")


def _rule_is(r, graph, node, phrase):
    return (r.get("graph"), r.get("node"), r.get("phrase")) == (graph, node, phrase)


def _live(r, now):
    """A rule that is on and was used (or adopted) within RULE_UNUSED_DAYS."""
    if r.get("active") is not True:
        return False
    t = _parse(r.get("last_used") or r.get("adopted_at"))
    return t is not None and timedelta(0) <= now - _naive(t) < timedelta(days=RULE_UNUSED_DAYS)


def _next_rule_id(rows):
    nums = [int(m.group(1)) for m in (re.match(r"^A(\d+)$", str(r.get("id", ""))) for r in rows) if m]
    return f"A{max(nums, default=0) + 1}"


def mark_not_needed(it, now=None):
    """What a `不要 N` leaves on the item (notify._collect; the log row takes the result). The mark is written only for a minutes line of
    RULE_NODE whose first sentence has a phrase: the item gets the time, and the log gets a key of the phrase (12 characters), never the
    sentence. Other items (Teams included) get no mark, so they never make a card."""
    mark = {"not_needed": True}
    rec = it.get("record") or {}
    if is_card(it) or (rec.get("graph"), rec.get("node")) != RULE_NODE:
        return mark
    phrase = phrase_of(rec.get("event") or {})
    if phrase is None:
        return mark
    it["not_needed_at"] = _iso(_naive(now) or datetime.now())
    return {**mark, "node": _node_key(), "phrase": _rule_hash(phrase)}


def _rule_counts(items, now):
    """{phrase key: {"phrase", "times": [the day of each 不要], "ok": n}} over the minutes lines of RULE_NODE (cards are no case)."""
    counts = {}
    for it in items.values():
        rec = it.get("record") or {}
        if is_card(it) or (rec.get("graph"), rec.get("node")) != RULE_NODE:
            continue
        phrase = phrase_of(rec.get("event") or {})
        if phrase is None:
            continue
        c = counts.setdefault(_rule_hash(phrase), {"phrase": phrase, "times": [], "ok": 0})
        if it.get("status") == "approved":
            c["ok"] += 1
        t = _parse(it.get("not_needed_at"))
        if t is not None and timedelta(0) <= now - _naive(t) < timedelta(days=RULE_DAYS):
            c["times"].append(_naive(t).date().isoformat())
    return counts


def _rule_rejected(items, now):
    """The keys of the rule cards that were NG'd within RULE_NG_DAYS: their sentence is not offered again for that long."""
    keys = set()
    for it in items.values():
        L = (it.get("record") or {}).get("learn") or {}
        t = _parse(it.get("learn_rejected_at"))
        if (is_card(it) and L.get("rule") and it.get("learn_state") == "rejected" and t
                and timedelta(0) <= now - _naive(t) < timedelta(days=RULE_NG_DAYS)):
            keys.add(L.get("node"))
    return keys


def _rule_pick(items, rows, now):
    """(phrase, times, days, key) of the sentence to offer, or None: RULE_MIN_TIMES 不要 on RULE_MIN_DAYS days, no OK for it, no rule
    that is on for it, and no NG for it within RULE_NG_DAYS."""
    rejected = _rule_rejected(items, now)
    best = None
    for h, c in sorted(_rule_counts(items, now).items()):
        times = c["times"]
        if len(times) < RULE_MIN_TIMES or len(set(times)) < RULE_MIN_DAYS or c["ok"]:
            continue
        key = f"{_node_key()}:{h}"
        if key in rejected or any(_live(r, now) and _rule_is(r, *RULE_NODE, c["phrase"]) for r in rows):
            continue
        if best is None or len(times) > len(best[1]):
            best = (c["phrase"], times, len(set(times)), key)
    return best


def offer_rule(out, backend, now=None):
    """R33 (learn=1, the daily path only): at most one card a day for a sentence that the PM answered 不要 three times on two days and
    never OK'd. Returns {"state": ...}; the card goes to queue.jsonl (notify posts it). Nothing is adopted here."""
    if not enabled():
        return {"state": "off"}
    from . import backends, notify
    now = _naive(now) or datetime.now()
    day = now.date().isoformat()
    out = Path(out)
    with fsutil.exclusive(notify.lock_path(out)) as got:
        if not got:
            return {"state": "busy"}
        st_path = out / "daily_state.json"
        st = fsutil.read_json(st_path, {})
        if not isinstance(st, dict):
            st = {}
        if st.get("learn_rule_day") == day:
            return {"state": "this_day"}
        items = notify.Approvals(out).data.get("items", {})
        if any(_waiting(it) for it in items.values()) or _unposted_card(out, items):
            return {"state": "pending"}
        pick = _rule_pick(items, _rules_read(out), now)
        if pick is None:
            return {"state": "nothing"}
        phrase, times, days, key = pick
        lid = f"L{_numbers(out, items)}"
        L = {"rule": True, "node": key, "graph": RULE_NODE[0], "node_id": RULE_NODE[1], "phrase": phrase,
             "judge": backends.judge_name(backend), "times": len(times), "days": days}
        row = {"graph": KIND, "event_kind": KIND, "event_id": lid, "node": "rule", "outcome": "advise", "needs_human": True,
               "advice": f"学習の提案 {lid}", "actions": [], "learn": L}
        out.mkdir(parents=True, exist_ok=True)
        with (out / "queue.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        st["learn_rule_day"] = day
        fsutil.write_atomic(st_path, json.dumps(st, ensure_ascii=False, indent=1))
        return {"state": "queued", "id": lid}


def _eligible(res):
    """The result is the check_line of minutes-followup, and its last step is the item_type question answered unsure."""
    if (res.get("graph"), res.get("node")) != RULE_NODE:
        return False
    path = res.get("path") or []
    return bool(path) and path[-1].get("node") == "item_type" and path[-1].get("edge") == "unsure"


def _unmarked(ev):
    """True when no free-text field of the line has a request mark (RULE_MARKS)."""
    text = _nfkc(" ".join(str(ev.get(k) or "") for k in RULE_TEXT_FIELDS))
    return not any(m in text for m in RULE_MARKS)


def _recorded_only_ids(out, chat, now):
    """The ids of the rules that recorded a line of this chat within RULE_FOLLOWUP_HOURS (decisions.jsonl; a damaged line is no line)."""
    ids = set()
    try:
        lines = (Path(out) / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return ids
    for line in lines:
        if '"demoted_by"' not in line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        ev = row.get("event") if isinstance(row, dict) else None
        when = _parse(row.get("at")) if isinstance(row, dict) else None
        if not isinstance(ev, dict) or ev.get("chat_id") != chat or when is None:
            continue
        if timedelta(0) <= now - _naive(when) < timedelta(hours=RULE_FOLLOWUP_HOURS):
            ids.add(str(row.get("demoted_by")))
    return ids


def _stop_rules(out, ids, now, reason):
    """Stop the rules in `ids` that are on. The ids stopped now ([] when none was on); None when the lock is busy or cannot be made."""
    try:
        with fsutil.exclusive(_rules_lock(out)) as got:
            if not got:
                return None
            rows = _rules_read(out)
            done = []
            for r in rows:
                if r.get("id") in ids and r.get("active") is True:
                    r["active"] = False
                    r["stopped_at"] = _iso(now)
                    r["stopped_reason"] = reason
                    done.append(r["id"])
            if done:
                _rules_write(out, rows)
            return done
    except fsutil.LockFolderError:
        return None


def demote(out, g, res, ev, now=None):
    """R33 (learn=1 only): a minutes line that a rule names is recorded only. Returns `res` itself unless the line is eligible: the
    check_line result reached through an unsure kind (item_type), with no request mark, no posting, paging or gated action, no severe
    hit, and a rule that is on for its first sentence. The result then reads outcome decide, one log.only action and `demoted_by` (the
    rule id); its path and via_unsure stay. A new line from a chat stops the rules that recorded a line of that chat within
    RULE_FOLLOWUP_HOURS. Whatever the graph decided, that line then goes to the PM (outcome advise, needs_human, actions only
    proposed); so does a line whose rules could not be locked. The chat is read only while a rule is on, and only a line that
    carries a chat_id can stop a rule (the shipped minutes scope has none, so only a manual revert or RULE_UNUSED_DAYS stops one)."""
    if not enabled() or not isinstance(res, dict) or not isinstance(ev, dict):
        return res
    from . import execute
    from . import graph as graphs
    out, now = Path(out), _naive(now) or datetime.now()
    chat = ev.get("chat_id")
    if chat and any(r.get("active") is True for r in _rules_read(out)):
        ids = _recorded_only_ids(out, chat, now)
        if ids:
            stopped = _stop_rules(out, ids, now, "followup")
            if stopped is None:   # the rules could not be locked: the line is not left to the graph's own decision
                return {**res, "outcome": "advise", "needs_human": True}
            if stopped:
                return {**res, "outcome": "advise", "needs_human": True,
                        "advice": f"{res.get('advice') or ''}（規則を戻しました）"}
    if not _eligible(res) or not _unmarked(ev):
        return res
    if any(a.get("type") in RULE_NEVER_ACTIONS or execute.is_gated(a) for a in res.get("actions") or []):
        return res
    phrase = phrase_of(ev)
    if phrase is None or graphs.severe_hit(g, res.get("node"), ev):
        return res
    key = (RULE_NODE[0], RULE_NODE[1], phrase)
    if not any(_live(r, now) and _rule_is(r, *key) for r in _rules_read(out)):
        return res
    try:
        with fsutil.exclusive(_rules_lock(out)) as got:
            if not got:   # another run holds the rules: the line goes to the PM as it is
                return res
            rows = _rules_read(out)
            hit = next((r for r in rows if _live(r, now) and _rule_is(r, *key)), None)
            if hit is None:
                return res
            hit["uses"] = int(hit.get("uses") or 0) + 1
            hit["last_used"] = _iso(now)
            _rules_write(out, rows)
    except fsutil.LockFolderError:
        return res
    return {**res, "outcome": "decide", "needs_human": False, "notify": False,
            "actions": [{"type": "log.only"}], "demoted_by": hit["id"]}


def _adopt_rule(out, it, rec, L, now):
    """An OK on a rule's card: the sentence becomes a rule in learned_rules.json (A<n>), once. A rule that is on for it already: stale."""
    lid, phrase = rec.get("event_id"), L.get("phrase")
    now = _naive(now) or datetime.now()
    if not isinstance(phrase, str) or not phrase:
        it["learn_state"] = "stale"
        return {"state": "stale", "id": lid}
    try:
        with fsutil.exclusive(_rules_lock(out)) as got:
            if not got:   # stays pending: the next run with --send adopts it
                return {"state": "pending", "id": lid}
            rows = _rules_read(out)
            if any(_live(r, now) and _rule_is(r, *RULE_NODE, phrase) for r in rows):
                it["learn_state"] = "stale"
                return {"state": "stale", "id": lid}
            aid = _next_rule_id(rows)
            rows.append({"id": aid, "graph": RULE_NODE[0], "node": RULE_NODE[1], "phrase": phrase, "adopted_at": _iso(now),
                         "uses": 0, "active": True, "judge": L.get("judge", "")})
            _rules_write(out, rows)
    except fsutil.LockFolderError:
        return {"state": "pending", "id": lid}
    _ledger_add({"id": lid, "kind": "rule_adopt", "rule": aid, "node": _node_key(), "adopted_at": _iso(now)})
    it["learn_state"] = "adopted"
    return {"state": "adopted", "id": lid, "node": _node_key(), "rule": aid}


def revert_rule(out, rid, now=None):
    """Take one rule back (`kimeru learn revert A<n>`): "reverted", "none" (no such rule that is on) or "busy"."""
    now = _naive(now) or datetime.now()
    done = _stop_rules(out, {rid}, now, "revert")
    if done is None:
        return "busy"
    if not done:
        return "none"
    _ledger_add({"id": rid, "kind": "rule_revert", "reverted_at": _iso(now)})
    return "reverted"


def _rule_post(n, rec):
    L = rec.get("learn") or {}
    return "\n".join([
        f"[kimeru #{n}] 迷ったときは記録だけにしますか: 「{L.get('phrase', '')}」で始まる議事録の行（種類が決まらないもの）",
        "OK で、その行は確かめずに記録だけにします（規則は kimeru learn list に出ます）。NG なら作りません（30 日は出しません）",
        f"OK {n} / NG {n} / 詳細 {n}"])


def _rule_detail(n, rec):
    L = rec.get("learn") or {}
    basis = ("件数は出しません（判断モデルが Jev のため）" if L.get("judge") == "Jev"
             else f"「不要」と返した件 {L.get('times', 0)} 件（{L.get('days', 0)} 日にわたる）。OK は 0 件")
    return "\n".join([
        f"[kimeru 詳細 #{n}] 学習の規則の提案 {rec.get('event_id')}（判断モデル: {L.get('judge', '')}）",
        f"変えること: 「{L.get('phrase', '')}」で始まる議事録の行で、種類が決まらないものは確かめず、記録だけにする",
        f"根拠: {basis}",
        "対象外: 依頼の印（？・ください・お願い・決め・至急・本日中）がある行、確信して決めた行、Sev0/1、投稿・通知・ADO の件",
        "OK で規則になります（取り消し: kimeru learn revert A<番号>）。NG なら作りません",
        f"返信: OK {n} / NG {n} / 保留 {n}"])


def _rule_lines(out):
    """What `learn list` shows of the rules: the ones on (how often they recorded a line, not with Jev), and the ones that stopped."""
    lines = []
    now = datetime.now()
    for r in _rules_read(out):
        head = f"規則 {r.get('id')}: 「{r.get('phrase', '')}」で始まる議事録の行を記録だけにする"
        if _live(r, now):
            used = "" if r.get("judge") == "Jev" else f"、使った件数 {int(r.get('uses') or 0)}"
            lines.append(f"{head}（採用 {r.get('adopted_at')}{used}）")
        else:
            lines.append(f"{head}（止まっています: {r.get('stopped_reason') or '未使用'}）")
    return lines
