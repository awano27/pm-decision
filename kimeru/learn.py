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
"""
import json
import re
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
    lid = rec.get("event_id")
    change = _change(L.get("before", DEFAULT_BAR), L.get("after", DEFAULT_BAR))
    counts = _counts(L)
    head = f"[kimeru #{n}] 学習の提案: {_question(L.get('node', ''))}: {change}" + (f"（{counts}）" if counts else "")
    return "\n".join([head, f"OK で変わります（戻すには kimeru learn revert {lid}）。NG なら変わりません",
                      f"OK {n} / NG {n} / 詳細 {n}"])


def format_detail(n, rec):
    """The answer to `詳細 N` for a card: the numbers behind it (non-Jev), and the same answers as the post."""
    L = rec.get("learn") or {}
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
    if len(argv) == 2 and argv[0] == "revert":
        res = revert(out, argv[1])
        print_fn({"reverted": f"{argv[1]} を取り消しました。その質問の基準は元に戻りました",
                  "none": f"{argv[1]} という採用した学習はありません（取り消しは一度だけです）",
                  "newer": f"{argv[1]} のあとに、同じ質問の基準が変わっています。新しいものから順に取り消してください（何も変えません）",
                  "busy": "approvals が別の処理で使われています。少し待って、もう一度実行してください"}[res])
        return 0 if res == "reverted" else 1
    print_fn("使い方: kimeru learn list ／ kimeru learn revert L<番号>")
    return 2
