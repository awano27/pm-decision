"""`kimeru calibrate`: tune the two threshold coefficients (profiles.py) on the PM's own reviewed decisions.

The model is never called: the answers recorded in decisions.jsonl are replayed with other coefficients and compared with
the labels from `kimeru review`. A change is proposed only when all of these hold, and otherwise the command says what is
missing and changes nothing:

  - enough labeled questions (default 100);
  - the data is split in two: the coefficients are searched on one half and confirmed on the other;
  - on the confirming half, the number of confident wrong answers did not increase (a search on 39 events once made
    that number go from 3 to 7 on 60 others: see profiles.py).

By default only a more careful change is proposed (more items go to a person). Widening what is decided automatically needs
--allow-wider. Applying writes a local file (thresholds.json); graphs and profiles.py are never edited (a graph edit would
change the key that marks an event as decided, and the same events would be judged again). `--revert` removes it.
"""
import hashlib
import json

from . import graph as graph_mod, profiles, review

CONF_GRID = (0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.5)
NOUL_GRID = (0.85, 0.9, 0.95, 1.0)
MIN_QUESTIONS = 100


def _matches(node, ans, edge, label):
    """Did a confident answer agree with the label?"""
    if node["kind"] == "plan":
        return ans.get("playbook") == label or (isinstance(label, list) and ans.get("playbook") in label)
    t = node["question"]["type"]
    if t == "choice":
        return edge == label or (isinstance(label, list) and edge in label)
    if t == "noul":
        return (edge == "yes") == bool(label)
    lo, hi = label
    # Keep calibration's label check in sync with eval/run_eval.judge: labels
    # are inclusive score ranges, and rounding would move a boundary answer.
    return lo <= ans.get("score", -99) <= hi


def labeled_questions(out, graphs):
    """[{key, node, node_id, ans, label}] from the reviews that carry a label, joined with the recorded answers."""
    by_name = {g["name"]: g for lst in graphs.values() for g in lst}
    decisions = {review.decision_key(r): r for r in review.read_jsonl(f"{out}/decisions.jsonl") if r.get("path") is not None}
    fx = {r["id"]: r for r in review.read_jsonl(review.fixtures_path())}
    qs = []
    for rev in review.read_jsonl(review.reviews_path()):
        rec = decisions.get(rev.get("key"))
        row = fx.get("user-" + hashlib.sha1(str(rev.get("key")).encode("utf-8")).hexdigest()[:8])
        if not rec or not row:
            continue
        g = by_name.get(rec.get("graph"))
        for step in rec.get("path", []):
            nid = step.get("node")
            node = (g or {}).get("nodes", {}).get(nid)
            if node and nid in row["expect"] and "matched" not in (step.get("answer") or {}):
                qs.append({"key": rev["key"], "node": node, "node_id": nid, "ans": step["answer"], "label": row["expect"][nid],
                           "judge": (rec.get("judge") or {}).get("name", "unknown")})
    return qs


def _outcome(q, conf_scale, noul_scale):
    """'ok' (confident and right), 'wrong' (confident and wrong) or 'human' (sent to a person) with these coefficients."""
    prof = {"name": "calibration", "conf_scale": conf_scale, "noul_scale": noul_scale}
    node, ans = q["node"], q["ans"]
    if node["kind"] == "plan":
        from .profiles import conf
        if ans.get("confidence", 0) < conf(node, "min_conf", 0.6, prof) or ans.get("playbook") in (None, "none"):
            return "human"
        return "ok" if _matches(node, ans, "ok", q["label"]) else "wrong"
    edge, _ = graph_mod.route(node, ans, prof)
    if edge == "unsure":
        return "human"
    return "ok" if _matches(node, ans, edge, q["label"]) else "wrong"


def _score(qs, cs, ns):
    res = {"ok": 0, "wrong": 0, "human": 0}
    for q in qs:
        res[_outcome(q, cs, ns)] += 1
    return res


def _half(q):
    return int(hashlib.sha1(q["key"].encode("utf-8")).hexdigest(), 16) % 2


def propose(qs, current, min_questions=MIN_QUESTIONS, allow_wider=False):
    """(proposal or None, lines explaining what was found or what is missing)."""
    lines = []
    if len(qs) < min_questions:
        lines.append(f"足りません: 正誤を付けた質問が {len(qs)} 問です（下限 {min_questions} 問）。`kimeru review` で増やしてください。何も変えません。")
        return None, lines
    a = [q for q in qs if _half(q) == 0]
    b = [q for q in qs if _half(q) == 1]
    if not a or not b:
        lines.append("足りません: データを 2 つに分けられません（片方が空です）。何も変えません。")
        return None, lines
    cur = (current["conf_scale"], current["noul_scale"])
    cands = [(cs, ns) for cs in CONF_GRID for ns in NOUL_GRID if allow_wider or (cs >= cur[0] and ns >= cur[1])]
    if cur not in cands:
        cands.append(cur)
    scored = sorted(cands, key=lambda c: (_score(a, *c)["wrong"], _score(a, *c)["human"]))
    best = scored[0]
    base_a, best_a = _score(a, *cur), _score(a, *best)
    base_b, best_b = _score(b, *cur), _score(b, *best)
    lines.append(f"探した側（{len(a)} 問）: 確信して間違えた件数 {base_a['wrong']} → {best_a['wrong']}、人に回る件数 {base_a['human']} → {best_a['human']}")
    lines.append(f"確かめた側（{len(b)} 問）: 確信して間違えた件数 {base_b['wrong']} → {best_b['wrong']}、人に回る件数 {base_b['human']} → {best_b['human']}")
    fewer_errors = best_a["wrong"] < base_a["wrong"]
    more_automatic = allow_wider and best_a["wrong"] == base_a["wrong"] and best_a["human"] < base_a["human"]
    if best == cur or not (fewer_errors or more_automatic):
        lines.append("今の係数より良いものは見つかりませんでした。何も変えません。")
        return None, lines
    if best_b["wrong"] > base_b["wrong"]:
        lines.append("確かめた側で、確信して間違える件数が増えるので、提案しません。何も変えません。")
        return None, lines
    lines.append(f"提案: conf_scale {cur[0]} → {best[0]}、noul_scale {cur[1]} → {best[1]}"
                 + ("" if best[0] >= cur[0] and best[1] >= cur[1] else "（自動で決める範囲が広がります）"))
    return {"conf_scale": best[0], "noul_scale": best[1]}, lines


def apply(profile_name, values):
    if profile_name not in profiles.JUDGE_PROFILE.values():   # the stub or an unknown judge: nothing reads that name
        raise ValueError(f"no threshold profile named {profile_name!r} to apply to")
    p = profiles.override_path()
    data = {}
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            data = {}
    data[profile_name] = values
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


def revert():
    p = profiles.override_path()
    if p.exists():
        p.unlink()
        return True
    return False


def run(out, graphs, min_questions=MIN_QUESTIONS, allow_wider=False, do_apply=False, print_fn=print):
    """Search and (with do_apply) apply, one judge at a time: the coefficients belong to the judge that made the answers,
    so a record that mixes judges is split first. A record whose judge is not known (the offline stub, an unknown name)
    is never tuned or applied: the file would be written under a name nothing reads. Returns {profile name: proposal}
    or None."""
    qs = labeled_questions(out, graphs)
    if not qs:
        print_fn(f"足りません: 正誤を付けた質問が 0 問です（下限 {min_questions} 問）。`kimeru review` で増やしてください。何も変えません。")
        return None
    by_judge = {}
    for q in qs:
        by_judge.setdefault(q["judge"], []).append(q)
    proposals, skipped = {}, []
    for judge in sorted(by_judge):
        jq = by_judge[judge]
        name = profiles.JUDGE_PROFILE.get(judge)
        tag = f"[{judge}] " if len(by_judge) > 1 else ""
        if name is None:
            skipped.append(judge)
            print_fn(f"{tag}判断モデルが分からない、または係数を持たない記録（{judge}）の {len(jq)} 問は、係数を探しません。適用もしません。"
                     "（Jev・Kev・CLM の判断を確かめた記録だけが対象です）")
            continue
        current = profiles.effective(profiles.PROFILES[name])
        if judge == "Jev":
            # Jev's accuracy must not be published: a proposal is fine, the counts that describe accuracy are not printed
            if len(jq) < min_questions:
                print_fn(f"{tag}足りません: 正誤を付けた質問が {len(jq)} 問です（下限 {min_questions} 問）。何も変えません。")
                continue
            proposal, _ = propose(jq, current, min_questions, allow_wider)
            print_fn(f"{tag}判断モデルが Jev のため、件数の内訳は出しません（TypeSafe の利用規約）。")
            print_fn(f"{tag}提案: {proposal}" if proposal else f"{tag}変更は提案しません。")
        else:
            proposal, lines = propose(jq, current, min_questions, allow_wider)
            for l in lines:
                print_fn(tag + l)
        if proposal:
            proposals[name] = proposal
            if do_apply:
                print_fn(f"{tag}適用しました: {apply(name, proposal)}（元に戻すには kimeru calibrate --revert）")
            else:
                print_fn(f"{tag}適用するには: kimeru calibrate --apply")
    if do_apply and not proposals:
        print_fn("適用しませんでした: " + ("係数を探せる判断モデルの記録がありません（上を見てください）。" if skipped and len(skipped) == len(by_judge)
                                      else "適用できる提案がありません。"))
    return proposals or None
