"""Decision graph engine.

A graph is JSON:
  {"name", "event": <kind>, "start": <node>, "nodes": {id: node}}

Node kinds
  judge   {"question": {...Jev question, optional "hints"}, "routes": {...}}
            noul : routes {"yes", "no", "unsure"}, optional "yes_at" (0.7), "no_at" (0.3)
            choice: routes {<option>: node, "unsure": node}, optional "min_conf" (0.6)
            score : routes {"bands": [[upper_exclusive, node], ...], "unsure": node},
                    optional "min_conf" (0.5); "adjacent_only": true sends splits across
                    non-neighbouring levels (each >= "split_at", 0.2) to unsure
  match   {"fields": [event fields], "patterns": [regex, ...], "routes": {"yes", "no"}}
            deterministic guard, no model: "yes" if any pattern matches any field
            (case-insensitive, NFKC-normalized). For safety nets such as outages that
            must never be under-scored by a model.
  plan    {"playbooks": "*" | [ids], "routes": {"ok", "none", "unsure"}}   (see plan.py)
            picks a playbook and orders its steps; the result is available to
            later nodes as {plan.title} {plan.summary} {plan.first}
  decide  {"actions": [...], "advice": "optional note to PM"}   -> auto-executed (dry-run in MVP)
  advise  {"advice": "...", "queue": true|false}                  -> PM reads; queue=true needs a human
          terminals may add "per_step": [action templates] rendered once per plan step
          with {step.title} {step.due} {step.id}

Every path must end in decide or advise, and every judge must have an `unsure`
route, so a low-confidence judgment always degrades to advice instead of guessing.
A malformed judge answer takes the same `unsure` route.

A graph may set "severe": {"field", "pattern", "exempt": [node ids]}. When the event field matches (e.g.
severity Sev0/Sev1), every outcome outside `exempt` reaches the PM: a decide is notified, an advise is queued,
whichever path the model's answers took.
"""
import json
import re
from pathlib import Path

from . import config
from .backends import BackendAnswerError

TERMINALS = ("decide", "advise")
MAX_STEPS = 32


class GraphError(ValueError):
    pass


def load(path):
    g = json.loads(Path(path).read_text(encoding="utf-8"))
    validate(g)
    return g


def _targets(node):
    r = node.get("routes", {})
    out = [v for k, v in r.items() if k != "bands"]
    out += [t for _, t in r.get("bands", [])]
    return out


def validate(g, playbooks=None):
    nodes = g.get("nodes") or {}
    if g.get("start") not in nodes:
        raise GraphError(f"{g.get('name')}: start node missing")
    sev = g.get("severe")
    if sev is not None:
        if (not isinstance(sev, dict) or not isinstance(sev.get("field"), str) or not isinstance(sev.get("pattern"), str)
                or not isinstance(sev.get("exempt", []), list)):
            raise GraphError(f"{g.get('name')}: severe needs a field and a pattern (text) and an optional exempt list")
        re.compile(sev["pattern"])
        unknown = [x for x in sev.get("exempt", []) if x not in nodes]
        if unknown:
            raise GraphError(f"{g.get('name')}: severe exempts unknown nodes {unknown}")
    from .actions import KNOWN
    for nid, n in nodes.items():
        k = n.get("kind")
        for a in list(n.get("actions", [])) + list(n.get("per_step", [])) if k in TERMINALS else []:
            if a.get("type") not in KNOWN:
                raise GraphError(f"{nid}: unknown action type {a.get('type')!r} (known: {', '.join(sorted(KNOWN))})")
        if k == "judge":
            q = n.get("question") or {}
            if q.get("type") not in ("noul", "choice", "score"):
                raise GraphError(f"{nid}: bad question type")
            if not str(q.get("instructions") or "").strip():
                raise GraphError(f"{nid}: question needs instructions")
            crit = q.get("criteria")
            if q["type"] == "choice" and not (isinstance(crit, dict) and len(crit) >= 2
                                              and all(str(v).strip() for v in crit.values())):
                raise GraphError(f"{nid}: choice needs at least 2 described options")
            if q["type"] == "score" and not (isinstance(crit, list) and len(crit) >= 2 and all(str(v).strip() for v in crit)):
                raise GraphError(f"{nid}: score needs at least 2 described levels")
            for kname in [x for x in n if x.endswith("_conf") or x.startswith("noul_")]:
                if not isinstance(n[kname], (int, float)) or not 0 <= n[kname] <= 1:
                    raise GraphError(f"{nid}: {kname} must be between 0 and 1")
            _check_thresholds(nid, n)
            r = n.get("routes") or {}
            if "unsure" not in r:
                raise GraphError(f"{nid}: judge needs an 'unsure' route")
            if q["type"] == "noul" and not {"yes", "no"} <= set(r):
                raise GraphError(f"{nid}: noul needs yes/no routes")
            if q["type"] == "choice":
                missing = set(q.get("criteria", {})) - set(r)
                if missing:
                    raise GraphError(f"{nid}: no route for {sorted(missing)}")
            if q["type"] == "score" and not r.get("bands"):
                raise GraphError(f"{nid}: score needs bands")
            if q["type"] == "score":
                cuts = [b[0] for b in r["bands"]]
                if any(b >= a for a, b in zip(cuts[1:], cuts)) or cuts[-1] < len(q["criteria"]) - 1:
                    raise GraphError(f"{nid}: bands must rise and the last must cover the top level "
                                     f"({len(q['criteria']) - 1})")
            for gd in n.get("guards", []):
                if q["type"] != "score" or not {"field", "pattern", "below"} <= set(gd):
                    raise GraphError(f"{nid}: guards need a score question and field/pattern/below")
                re.compile(gd["pattern"])
            for t in _targets(n):
                if t not in nodes:
                    raise GraphError(f"{nid}: route to unknown node {t}")
        elif k == "match":
            r = n.get("routes") or {}
            if set(r) != ({"yes", "no", "mixed"} if n.get("mixed_if") else {"yes", "no"}):
                raise GraphError(f"{nid}: match needs yes/no routes (plus mixed when mixed_if is set)")
            if not n.get("fields") or not n.get("patterns"):
                raise GraphError(f"{nid}: match needs fields and patterns")
            for pat in n["patterns"] + n.get("exclude", []) + n.get("mixed_if", []):
                try:
                    re.compile(pat)
                except re.error as e:
                    raise GraphError(f"{nid}: bad pattern {pat!r}: {e}") from None
            for t in r.values():
                if t not in nodes:
                    raise GraphError(f"{nid}: route to unknown node {t}")
        elif k == "plan":
            r = n.get("routes") or {}
            if set(r) != {"ok", "none", "unsure"}:
                raise GraphError(f"{nid}: plan needs exactly ok/none/unsure routes")
            _check_thresholds(nid, n)
            for t in r.values():
                if t not in nodes:
                    raise GraphError(f"{nid}: route to unknown node {t}")
            ids = n.get("playbooks", "*")
            if ids != "*" and (not isinstance(ids, list) or not ids):
                raise GraphError(f"{nid}: playbooks must be '*' or a non-empty list")
            if playbooks is not None and ids != "*":
                missing = set(ids) - set(playbooks)
                if missing:
                    raise GraphError(f"{nid}: unknown playbooks {sorted(missing)}")
        elif k not in TERMINALS:
            raise GraphError(f"{nid}: unknown kind {k}")
    # Reachability and cycle checks use a bounded traversal plus a topological
    # order, so diamonds and shared tails are processed once per edge.
    targets = {nid: _targets(node) for nid, node in nodes.items()}
    seen, stack = set(), [g["start"]]
    while stack:
        nid = stack.pop()
        if nid in seen:
            continue
        seen.add(nid)
        stack.extend(targets[nid])
    unreachable = set(nodes) - seen
    if unreachable:
        raise GraphError(f"unreachable nodes: {sorted(unreachable)}")

    indegree = {nid: 0 for nid in nodes}
    for outgoing in targets.values():
        for target in outgoing:
            indegree[target] += 1
    ready = [nid for nid, degree in indegree.items() if degree == 0]
    order = []
    while ready:
        nid = ready.pop()
        order.append(nid)
        for target in targets[nid]:
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
    if len(order) != len(nodes):
        cycle_node = next(nid for nid, degree in indegree.items() if degree)
        raise GraphError(f"cycle at {cycle_node}")

    depth = {}
    for nid in reversed(order):
        depth[nid] = 1 + max((depth[target] for target in targets[nid]), default=0)
        if depth[nid] > MAX_STEPS:
            raise GraphError(f"path from {nid} exceeds the {MAX_STEPS}-step execution limit")


def _split_non_adjacent(probs, at):
    """True if two levels that are not neighbours both hold >= `at` probability."""
    heavy = sorted(int(k) for k, p in probs.items() if str(k).isdigit() and p >= at)
    return len(heavy) >= 2 and heavy[-1] - heavy[0] > 1


def match_eval(node, event):
    """("yes" | "mixed" | "no", pattern). Optional "exclude" patterns veto a hit only in the
    same field (a crash reported only in a test environment); a test-environment note in
    another field does not cancel a production report. A vetoed hit whose field also
    matches "mixed_if" (e.g. 本番) routes "mixed" so a person looks at it."""
    import unicodedata
    mixed = None
    for f in node["fields"]:
        text = unicodedata.normalize("NFKC", str(event.get(f) or ""))
        hit = next((p for p in node["patterns"] if re.search(p, text, re.IGNORECASE)), None)
        if not hit:
            continue
        if not any(re.search(x, text, re.IGNORECASE) for x in node.get("exclude", [])):
            return "yes", hit
        if any(re.search(x, text, re.IGNORECASE) for x in node.get("mixed_if", [])):
            mixed = mixed or hit
    return ("mixed", mixed) if mixed else ("no", None)


def guard_hit(node, event, ans, edge):
    """Score-node guards: {"field", "pattern", "below"}. When the event field matches (e.g. severity
    Sev0/Sev1) but the model's score is under `below`, the answer is overruled to "unsure"."""
    import unicodedata
    if edge == "unsure" or "score" not in ans:
        return None
    for gd in node.get("guards", []):
        text = unicodedata.normalize("NFKC", str(event.get(gd["field"]) or ""))
        if re.search(gd["pattern"], text, re.IGNORECASE) and ans["score"] < gd["below"]:
            return f"{gd['field']}={text} but score {ans['score']:.2f} < {gd['below']}"
    return None


def severe_hit(g, nid, event):
    """Why this outcome must reach the PM whatever the model said (see "severe" in the module doc), or None."""
    import unicodedata
    sev = g.get("severe")
    if not sev or nid in sev.get("exempt", []):
        return None
    text = unicodedata.normalize("NFKC", str(event.get(sev["field"]) or ""))
    return f"{sev['field']}={text}" if re.search(sev["pattern"], text, re.IGNORECASE) else None


def match_node(node, event):
    """Pattern that matched cleanly (for the trace), or None."""
    edge, hit = match_eval(node, event)
    return hit if edge == "yes" else None


def route(node, ans, profile=None):
    """Return (edge_label, next_node_id) for a judge answer. `profile` adjusts thresholds
    for the backend that produced the answer (see profiles.py)."""
    from .profiles import conf, noul_band
    from .backends import validate_answer
    q, r = node["question"], node["routes"]
    validate_answer(q, ans)
    if q["type"] == "noul":
        p = ans["noul"]
        yes_at, no_at = noul_band(node, profile)
        if p >= yes_at:
            return "yes", r["yes"]
        if p <= no_at:
            return "no", r["no"]
        return "unsure", r["unsure"]
    if ans.get("confidence", 0) < conf(node, "min_conf", 0.6 if q["type"] == "choice" else 0.5, profile):
        return "unsure", r["unsure"]
    if q["type"] == "choice":
        return ans["choice"], r[ans["choice"]]
    if node.get("adjacent_only") and _split_non_adjacent(ans.get("probabilities") or {}, node.get("split_at", 0.2)):
        return "unsure", r["unsure"]
    for upper, target in r["bands"]:
        if ans["score"] < upper:
            return f"<{upper}", target
    return "top", r["bands"][-1][1]


_TPL = re.compile(r"\{([a-zA-Z_][\w.]*)\}")


def one_line(s, limit=None):
    """Collapse line breaks and runs of blanks; cut at `title_max` characters (setting) with an ellipsis."""
    import os
    try:
        limit = limit or max(20, config.int_value("title_max"))
    except ValueError:
        limit = 100
    s = " ".join(str(s).split())
    return s if len(s) <= limit else s[:limit - 1] + "…"


THRESHOLD_KEYS = ("yes_at", "no_at", "split_at", "need_at", "min_conf", "due_min_conf", "first_min_conf")


def _check_thresholds(nid, n):
    """Every threshold is a number from 0 to 1, and a yes needs a higher probability than a no."""
    for k in THRESHOLD_KEYS:
        if k in n and (isinstance(n[k], bool) or not isinstance(n[k], (int, float)) or not 0 <= n[k] <= 1):
            raise GraphError(f"{nid}: {k} must be a number between 0 and 1")
    if n.get("yes_at", 0.7) <= n.get("no_at", 0.3):
        raise GraphError(f"{nid}: yes_at ({n.get('yes_at', 0.7)}) must be greater than no_at ({n.get('no_at', 0.3)})")


def render(s, ctx):
    def get(m):
        cur = ctx
        for part in m.group(1).split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        return "" if cur is None else str(cur)
    if isinstance(s, str):
        return _TPL.sub(get, s)
    if isinstance(s, dict):
        return {k: render(v, ctx) for k, v in s.items()}
    if isinstance(s, list):
        return [render(v, ctx) for v in s]
    return s


def run(g, event, backend, state=None, playbooks=None, batch=None):
    """Walk the graph for one event. Returns a trace dict ending in an outcome.

    batch=True asks every judge question of the graph in ONE call the first time a question is needed (after the rules
    have had their say: a route decided by rules alone asks nothing). The plan questions are not part of it: they depend on
    the chosen playbook. The final action is the one a question-by-question walk reaches, provided the backend answers each
    question independently of the others."""
    import os
    from .events import state_of
    from . import plan as planner
    if batch is None:
        batch = config.value("batch_questions") == "1"
    state = state if state is not None else state_of(event)
    nodes, nid, trace, answers, plan = g["nodes"], g["start"], [], {}, None
    pre = {}
    for _ in range(MAX_STEPS):
        n = nodes[nid]
        if n["kind"] in TERMINALS:
            ctx = {"event": event, "answers": answers, "plan": plan or {}}
            acts = render(n.get("actions", []), ctx)
            for step in (plan or {}).get("steps", []) if n.get("per_step") else []:
                acts += render(n["per_step"], {**ctx, "step": step})
            for a in acts:   # a title is one short line, whatever text it was made from (a long chat message, a pasted list)
                if isinstance(a.get("title"), str):
                    a["title"] = one_line(a["title"])
            out = {"outcome": n["kind"], "node": nid,
                   "advice": render(n.get("advice"), ctx),
                   "actions": acts,
                   "needs_human": n["kind"] == "advise" and n.get("queue", False),
                   "notify": n["kind"] == "decide" and bool(n.get("notify"))}
            severe = severe_hit(g, nid, event)
            if severe and not (out["notify"] or out["needs_human"]):
                out["severe_guard"] = severe
                out["notify" if n["kind"] == "decide" else "needs_human"] = True
            if plan:
                out["plan"] = plan
            return {"graph": g["name"], "event_id": event.get("id"), "event_kind": event.get("kind"),
                    "path": trace, **out}
        if n["kind"] == "match":
            edge, hit = match_eval(n, event)
            trace.append({"node": nid, "answer": {"matched": hit}, "edge": edge})
            nid = n["routes"][edge]
            continue
        if n["kind"] == "plan":
            if not playbooks:  # no playbooks loaded: degrade to the pre-plan path
                trace.append({"node": nid, "answer": {}, "edge": "unsure"})
                nid = n["routes"]["unsure"]
                continue
            try:
                edge, plan, ans = planner.build(n, state, backend, playbooks)
            except BackendAnswerError as e:
                trace.append({"node": nid, "answer": {"invalid": str(e)[:200]}, "edge": "unsure"})
                nid = n["routes"]["unsure"]
                continue
            answers[nid] = ans
            trace.append({"node": nid, "answer": {"playbook": ans["playbook"].get("choice"),
                                                   "confidence": ans["playbook"].get("confidence"),
                                                   "steps": [s["id"] for s in (plan or {}).get("steps", [])],
                                                   "answers": _plan_answers(ans)},
                          "edge": edge})
            nid = n["routes"][edge]
            continue
        try:
            if batch and not pre:
                pre = backend.ask(state, {k: nodes[k]["question"] for k in reachable_judges(nodes, nid, event)})
            ans = pre[nid] if nid in pre else backend.ask(state, {nid: n["question"]})[nid]
            edge, nxt = route(n, ans, getattr(backend, "profile", None))
        except BackendAnswerError as e:   # a malformed answer is not a judgment: the safe route, never a lost event
            batch, pre = False, {}   # a batch with a bad answer is not trusted: ask the remaining questions one by one
            trace.append({"node": nid, "answer": {"invalid": str(e)[:200]}, "edge": "unsure"})
            nid = n["routes"]["unsure"]
            continue
        guard = guard_hit(n, event, ans, edge)
        if guard:   # the source already rated it severe but the model scored it lower: a person decides
            edge, nxt = "unsure", n["routes"]["unsure"]
            ans = {**ans, "guard": guard}
        answers[nid] = ans
        trace.append({"node": nid, "answer": {k: v for k, v in ans.items() if k != "type"}, "edge": edge})
        nid = nxt
    raise GraphError("max steps exceeded")


def reachable_judges(nodes, start, event):
    """The judge nodes a walk from `start` can still reach, in the graph's order. A match node has one outcome for this
    event (no model), so only the branch it takes counts; after a model question or a plan every route stays open.
    Used by batch mode: a question on a branch the rules have closed is not asked."""
    seen, stack = set(), [start]
    while stack:
        nid = stack.pop()
        if nid in seen:
            continue
        seen.add(nid)
        n = nodes[nid]
        if n["kind"] == "match":
            stack.append(n["routes"][match_eval(n, event)[0]])
        elif n["kind"] not in TERMINALS:
            stack += _targets(n)
    return [k for k, v in nodes.items() if k in seen and v["kind"] == "judge"]


def _plan_answers(ans):
    """need_* / due_* / first from the plan's second call, kept as small numbers (no text)."""
    keep = ("noul", "score", "choice", "confidence")
    return {k: {f: v[f] for f in keep if f in v} for k, v in ans.items() if k != "playbook" and isinstance(v, dict)}


def load_dir(d, playbooks=None):
    graphs = {}
    for p in sorted(Path(d).glob("*.json")):
        g = json.loads(p.read_text(encoding="utf-8"))
        validate(g, playbooks)
        graphs.setdefault(g["event"], []).append(g)
    return graphs
