"""Decision graph engine.

A graph is JSON:
  {"name", "event": <kind>, "start": <node>, "nodes": {id: node}}

Node kinds
  judge   {"question": {...Jev question, optional "hints"}, "routes": {...}}
            noul : routes {"yes", "no", "unsure"}, optional "yes_at" (0.7), "no_at" (0.3)
            choice: routes {<option>: node, "unsure": node}, optional "min_conf" (0.6)
            score : routes {"bands": [[upper_exclusive, node], ...], "unsure": node},
                    optional "min_conf" (0.5)
  decide  {"actions": [...], "advice": "optional note to PM"}   -> auto-executed (dry-run in MVP)
  advise  {"advice": "...", "queue": true|false}                  -> PM reads; queue=true needs a human

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


def validate(g):
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


def run(g, event, backend, state=None):
    """Walk the graph for one event. Returns a trace dict ending in an outcome."""
    from .events import state_of
    state = state if state is not None else state_of(event)
    nodes, nid, trace, answers = g["nodes"], g["start"], [], {}
    for _ in range(MAX_STEPS):
        n = nodes[nid]
        if n["kind"] in TERMINALS:
            ctx = {"event": event, "answers": answers}
            out = {"outcome": n["kind"], "node": nid,
                   "advice": render(n.get("advice"), ctx),
                   "actions": render(n.get("actions", []), ctx),
                   "needs_human": n["kind"] == "advise" and n.get("queue", False)}
            return {"graph": g["name"], "event_id": event.get("id"), "event_kind": event.get("kind"),
                    "path": trace, **out}
        ans = backend.ask(state, {nid: n["question"]})[nid]
        edge, nxt = route(n, ans)
        answers[nid] = ans
        trace.append({"node": nid, "answer": {k: v for k, v in ans.items() if k != "type"}, "edge": edge})
        nid = nxt
    raise GraphError("max steps exceeded")


def load_dir(d):
    graphs = {}
    for p in sorted(Path(d).glob("*.json")):
        g = load(p)
        graphs.setdefault(g["event"], []).append(g)
    return graphs
