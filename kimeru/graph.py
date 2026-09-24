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
  plan    {"playbooks": "*" | [ids], "routes": {"ok", "none", "unsure"}}   (see plan.py)
            picks a playbook and orders its steps; the result is available to
            later nodes as {plan.title} {plan.summary} {plan.first}
  decide  {"actions": [...], "advice": "optional note to PM"}   -> auto-executed (dry-run in MVP)
  advise  {"advice": "...", "queue": true|false}                  -> PM reads; queue=true needs a human
          terminals may add "per_step": [action templates] rendered once per plan step
          with {step.title} {step.due} {step.id}

Every path must end in decide or advise, and every judge must have an `unsure`
route, so a low-confidence judgment always degrades to advice instead of guessing.
"""
import json
import re
from pathlib import Path

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
    for nid, n in nodes.items():
        k = n.get("kind")
        if k == "judge":
            q = n.get("question") or {}
            if q.get("type") not in ("noul", "choice", "score"):
                raise GraphError(f"{nid}: bad question type")
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
            for t in _targets(n):
                if t not in nodes:
                    raise GraphError(f"{nid}: route to unknown node {t}")
        elif k == "plan":
            r = n.get("routes") or {}
            if set(r) != {"ok", "none", "unsure"}:
                raise GraphError(f"{nid}: plan needs exactly ok/none/unsure routes")
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
    # reachability + no cycles
    seen, stack = set(), [(g["start"], ())]
    while stack:
        nid, path = stack.pop()
        if nid in path:
            raise GraphError(f"cycle at {nid}")
        seen.add(nid)
        stack += [(t, path + (nid,)) for t in _targets(nodes[nid])]
    unreachable = set(nodes) - seen
    if unreachable:
        raise GraphError(f"unreachable nodes: {sorted(unreachable)}")


def _split_non_adjacent(probs, at):
    """True if two levels that are not neighbours both hold >= `at` probability."""
    heavy = sorted(int(k) for k, p in probs.items() if str(k).isdigit() and p >= at)
    return len(heavy) >= 2 and heavy[-1] - heavy[0] > 1


def route(node, ans):
    """Return (edge_label, next_node_id) for a judge answer."""
    q, r = node["question"], node["routes"]
    if q["type"] == "noul":
        p = ans["noul"]
        if p >= node.get("yes_at", 0.7):
            return "yes", r["yes"]
        if p <= node.get("no_at", 0.3):
            return "no", r["no"]
        return "unsure", r["unsure"]
    if ans.get("confidence", 0) < node.get("min_conf", 0.6 if q["type"] == "choice" else 0.5):
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


def run(g, event, backend, state=None, playbooks=None):
    """Walk the graph for one event. Returns a trace dict ending in an outcome."""
    from .events import state_of
    from . import plan as planner
    state = state if state is not None else state_of(event)
    nodes, nid, trace, answers, plan = g["nodes"], g["start"], [], {}, None
    for _ in range(MAX_STEPS):
        n = nodes[nid]
        if n["kind"] in TERMINALS:
            ctx = {"event": event, "answers": answers, "plan": plan or {}}
            acts = render(n.get("actions", []), ctx)
            for step in (plan or {}).get("steps", []) if n.get("per_step") else []:
                acts += render(n["per_step"], {**ctx, "step": step})
            out = {"outcome": n["kind"], "node": nid,
                   "advice": render(n.get("advice"), ctx),
                   "actions": acts,
                   "needs_human": n["kind"] == "advise" and n.get("queue", False)}
            if plan:
                out["plan"] = plan
            return {"graph": g["name"], "event_id": event.get("id"), "event_kind": event.get("kind"),
                    "path": trace, **out}
        if n["kind"] == "plan":
            if not playbooks:  # no playbooks loaded: degrade to the pre-plan path
                trace.append({"node": nid, "answer": {}, "edge": "unsure"})
                nid = n["routes"]["unsure"]
                continue
            edge, plan, ans = planner.build(n, state, backend, playbooks)
            answers[nid] = ans
            trace.append({"node": nid, "answer": {"playbook": ans["playbook"].get("choice"),
                                                   "confidence": ans["playbook"].get("confidence"),
                                                   "steps": [s["id"] for s in (plan or {}).get("steps", [])]},
                          "edge": edge})
            nid = n["routes"][edge]
            continue
        ans = backend.ask(state, {nid: n["question"]})[nid]
        edge, nxt = route(n, ans)
        answers[nid] = ans
        trace.append({"node": nid, "answer": {k: v for k, v in ans.items() if k != "type"}, "edge": edge})
        nid = nxt
    raise GraphError("max steps exceeded")


def load_dir(d, playbooks=None):
    graphs = {}
    for p in sorted(Path(d).glob("*.json")):
        g = json.loads(p.read_text(encoding="utf-8"))
        validate(g, playbooks)
        graphs.setdefault(g["event"], []).append(g)
    return graphs
