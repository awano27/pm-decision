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
  decide  {"actions": [...], "advice": "optional note to PM", "queue": true|false}   -> auto-executed (dry-run in MVP);
            queue=true also waits for the PM in a numbered post (an information request to the author, for one)
  advise  {"advice": "...", "queue": true|false}                  -> PM reads; queue=true needs a human
          terminals may add "per_step": [action templates] rendered once per plan step
          with {step.title} {step.due} {step.id}

Every path must end in decide or advise, and every judge must have an `unsure`
route, so a low-confidence judgment always degrades to advice instead of guessing.
A malformed judge answer takes the same `unsure` route.

A result reached through an unsure answer carries "via_unsure": [node ids]; when it also asked nobody (no notice, no
confirmation) the morning brief lists it so that the PM can check it.

A graph may set "severe": {"field", "pattern", "exempt": [node ids]}. When the event field matches (e.g.
severity Sev0/Sev1), every outcome outside `exempt` reaches the PM: a decide is notified, an advise is queued,
whichever path the model's answers took.
"""
import hashlib
import json
import re
from pathlib import Path

from . import asks, config, profiles
from .backends import BackendAnswerError

TERMINALS = ("decide", "advise")
MAX_STEPS = 32


class GraphError(ValueError):
    pass


def load(path):
    g = json.loads(Path(path).read_text(encoding="utf-8"))
    validate(g)
    return g


def version(g):
    """The version of a graph: 8 hex digits of a hash of its JSON. The key of a decision carries it (cli.process), so a record
    can be matched with the graph it was judged by (explain.py)."""
    return hashlib.sha1(json.dumps(g, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:8]


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
            if set(r) != ({"yes", "no", "mixed"} if n.get("mixed_if") or n.get("mixed_field") else {"yes", "no"}):
                raise GraphError(f"{nid}: match needs yes/no routes (plus mixed when mixed_if or mixed_field is set)")
            if not n.get("fields") or not n.get("patterns"):
                raise GraphError(f"{nid}: match needs fields and patterns")
            if "exclude_scope" in n and n["exclude_scope"] not in ("field", "sentence"):
                raise GraphError(f"{nid}: exclude_scope must be 'field' or 'sentence'")
            when = n.get("when", {})
            if not isinstance(when, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in when.items()):
                raise GraphError(f"{nid}: when must map a field name to a pattern")
            for key in ("fields", "patterns", "exclude", "exclude_sentence", "mixed_if", "mixed_field"):
                values = n.get(key, [])
                if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                    raise GraphError(f"{nid}: {key} must be a list of strings")
            for pat in n["patterns"] + n.get("exclude", []) + n.get("exclude_sentence", []) + n.get("mixed_if", []) + n.get("mixed_field", []) + list(when.values()):
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


# R38: how a question is written. `validate` prints these as warnings; a run never reads them and nothing is refused.
NOUL_WARNING = "noul has and/or in it, so one question asks two things; ask one condition per question"
CHOICE_WARNING = "choice has no 'none' option, so a message that fits no option is forced into one"
SCORE_WARNING = "score level {i} is a degree word, not a situation; describe the case the level stands for"
_COMPOUND = re.compile(r"\b(?:and|or)\b|または|かつ", re.IGNORECASE)
_DEGREE = re.compile(r"(?:(?:very|somewhat|slightly|quite|fairly|rather|extremely|a little|a bit) )?"
                     r"(?:low|medium|mid|moderate|high|small|large|minor|major|mild|severe|serious|critical|urgent|"
                     r"normal|important|strong|weak|some|little|much)"
                     r"(?: (?:level|priority|impact|severity|urgency|risk))?", re.IGNORECASE)
_DEGREE_JA = frozenset(("低", "低い", "中", "中程度", "高", "高い", "小", "小さい", "大", "大きい", "軽微", "重大", "少し", "やや",
                        "かなり", "普通", "多い", "少ない"))


def _is_degree(level):
    t = str(level).strip().rstrip(".。").strip()
    return bool(_DEGREE.fullmatch(t)) or t in _DEGREE_JA


def _has_none(options):
    return any(str(k).strip().lower() == "none" or "該当なし" in str(v) or "none of" in str(v).lower()
               for k, v in options.items())


def lint(g):
    """Warnings about how the judge questions are written, as "<node>: <text>" (ASCII only). Nothing is refused and the
    graph is not changed: a question's wording is the judgment, so changing it is a decision of its own (a new version)."""
    out = []
    for nid, n in (g.get("nodes") or {}).items():
        q = n.get("question") if isinstance(n, dict) and n.get("kind") == "judge" else None
        if not isinstance(q, dict):
            continue
        crit = q.get("criteria")
        if q.get("type") == "noul" and _COMPOUND.search(str(q.get("instructions") or "")):
            out.append(f"{nid}: {NOUL_WARNING}")
        if q.get("type") == "choice" and isinstance(crit, dict) and not _has_none(crit):
            out.append(f"{nid}: {CHOICE_WARNING}")
        if q.get("type") == "score" and isinstance(crit, list):
            out += [f"{nid}: {SCORE_WARNING.format(i=i)}" for i, level in enumerate(crit) if _is_degree(level)]
    return out


def _split_non_adjacent(probs, at):
    """True if two levels that are not neighbours both hold >= `at` probability."""
    heavy = sorted(int(k) for k, p in probs.items() if str(k).isdigit() and p >= at)
    return len(heavy) >= 2 and heavy[-1] - heavy[0] > 1


def _sentence_bounds(text, start, end):
    """The sentence(s) of `text` that a match at [start, end) lies in, as (from, to). A sentence ends at . ! ? 。 ！ ？ ; ； or a line break."""
    left = max((m.end() for m in re.finditer(r"[.!?。！？；;\r\n]", text[:start])), default=0)
    m = re.search(r"[.!?。！？；;\r\n]", text[end:])
    return left, (len(text) if not m else end + m.end())


def match_eval(node, event):
    """("yes" | "mixed" | "no", pattern). Optional "exclude" patterns veto a hit only in the
    same field (a crash reported only in a test environment); a test-environment note in
    another field does not cancel a production report. A vetoed hit whose field also
    matches "mixed_if" (e.g. 本番) routes "mixed" so a person looks at it.
    "exclude_sentence" patterns veto a hit only when they match the sentence the hit is in
    (an improvement request, a FAQ, a postmortem that merely names a failure); such a veto
    routes "no" (the model decides), never "mixed". With "exclude_scope": "sentence" the
    patterns, "exclude" and "mixed_if" are all evaluated one sentence at a time, and a
    sentence keeps its closing ? so a question can be excluded. "mixed_field" patterns are checked on
    the whole field once a hit stands: a recovery note in another sentence (本番で決済が通りません。復旧しました)
    routes "mixed" so a person looks, instead of paging for an outage that is already over. "when" ({field: pattern}) makes the
    node apply only to events whose fields match; any other event is "no"."""
    edge, pattern, _ = _match(node, event)
    return edge, pattern


def match_explain(node, event):
    """What match_eval decides (the same judgment: _match), with the words the rule read, for the explanation of a record
    (explain.py): {"edge", "pattern", "field", "word", "veto", "mixed"}. "word" is the text the pattern matched in the
    field, "veto" the text of the exclude pattern that rejected that hit, "mixed" the text of the recovery (mixed_field) or
    mixed_if pattern that sent it to a person; a key the rule did not read is None."""
    edge, pattern, why = _match(node, event)
    return {"edge": edge, "pattern": pattern, "field": why.get("field"), "word": why.get("word"),
            "veto": why.get("veto"), "mixed": why.get("mixed")}


def _first(patterns, text):
    """The text the first of `patterns` matches in `text` (case-insensitive), or None when none matches. An empty match is a match."""
    for x in patterns:
        m = re.search(x, text, re.IGNORECASE)
        if m is not None:
            return m.group(0)
    return None


def _match(node, event):
    """The judgment of a match node: (edge, pattern, why). match_eval and match_explain both read it, so the two cannot disagree.
    `why` names what the rule read (the keys of match_explain); it is {} when the node read nothing."""
    import unicodedata
    for f, pat in (node.get("when") or {}).items():   # eligibility: every listed field must match, else "no" (Epic/Task are not critical bugs)
        if not re.search(pat, unicodedata.normalize("NFKC", str(event.get(f) or "")), re.IGNORECASE):
            return "no", None, {}
    mixed, mixed_why = None, {}
    rejected = {}   # the first hit a veto rejected: named when the node says no
    sent_ex = node.get("exclude_sentence", [])
    for f in node["fields"]:
        text = unicodedata.normalize("NFKC", str(event.get(f) or ""))
        # Exclusions traditionally apply to the whole field. Some safety nets
        # need narrower scope so an unrelated future/healthy sentence cannot
        # veto a separate current-outage sentence.
        candidates = (re.split(r"(?<=[.!?。！？；;\r\n])", text)
                      if node.get("exclude_scope", "field") == "sentence" else [text])
        for candidate in candidates:
            if sent_ex:
                hits = [(p, m.start(), m.end()) for p in node["patterns"] for m in re.finditer(p, candidate, re.IGNORECASE)]
            else:
                hits = [(p, m.start(), m.end()) for p in node["patterns"]
                        for m in [re.search(p, candidate, re.IGNORECASE)] if m is not None][:1]
            if not hits:
                continue
            veto = _first(node.get("exclude", []), candidate)
            for hit, start, end in hits:
                word = candidate[start:end]
                if sent_ex and veto is None:
                    lo, hi = _sentence_bounds(candidate, start, end)
                    sentence_veto = _first(sent_ex, candidate[lo:hi])
                    if sentence_veto is not None:
                        rejected = rejected or {"field": f, "word": word, "veto": sentence_veto}
                        continue
                if veto is None:
                    recovery = _first(node.get("mixed_field", []), text)
                    if recovery is not None:
                        return "mixed", hit, {"field": f, "word": word, "mixed": recovery}
                    return "yes", hit, {"field": f, "word": word}
                vetoed = {"field": f, "word": word, "veto": veto}
                mixed_if = _first(node.get("mixed_if", []), candidate)
                if mixed_if is not None and not mixed:
                    mixed, mixed_why = hit, {**vetoed, "mixed": mixed_if}
                rejected = rejected or vetoed
                break
    if mixed:
        return "mixed", mixed, mixed_why
    return "no", None, rejected


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


HEAVIEST_FIRST = ("decision", "requirements", "none")   # R35: a status or fyi message takes its heaviest request, in this order


def combine_requests(body, labels):
    """R35: the route of a message from its own intent (`body`) and the intents of its requests (`labels`), only toward people:
    an unsure request makes the message unsure (a person is asked); an incident request makes a status, fyi, decision or incident
    message an incident. A status or fyi message takes its heaviest request (HEAVIEST_FIRST). Any other message keeps its own
    route: a none or requirements message may end at the PM's queue (ask_pm), which an incident route (a notice) would lower."""
    if body == "unsure" or "unsure" in labels:
        return "unsure"
    if "incident" in labels and body in ("status", "fyi", "decision", "incident"):
        return "incident"
    if body in ("status", "fyi"):
        for label in HEAVIEST_FIRST:
            if label in labels:
                return label
    return body


def _one_request(question, k):
    """The intent question for one request of the message: the same choices, asked about `asks[k]` alone."""
    return {**question, "instructions": f"Answer for the one request `asks[{k}]` (a sentence of `text`) only. " + question["instructions"]}


def _confidence(answer):
    c = answer.get("confidence") if isinstance(answer, dict) else None
    return round(float(c), 2) if isinstance(c, (int, float)) and not isinstance(c, bool) else 0.0


def _split_intent(n, nid, state, backend, requests, profile):
    """R35: the intent answer of the message and one answer per request, in ONE call. The call's state carries the requests
    (`asks`); the state the rest of the graph sees does not. Returns (body answer, body edge, combined edge, info) where info
    keeps only the number of requests and their confidences. A malformed answer for the message raises BackendAnswerError (the
    caller takes the unsure route, as for any intent answer); a malformed answer for one request makes only that request unsure."""
    istate = {**state, "asks": list(requests)}
    qs = {nid: with_context(n["question"], istate)}
    for k in range(len(requests)):
        qs[f"{nid}_{k}"] = with_context(_one_request(n["question"], k), istate)
    answered = backend.ask(istate, qs)
    body = answered.get(nid) if isinstance(answered, dict) else None
    body_edge, _ = route(n, body, profile)
    labels, conf = [], []
    for k in range(len(requests)):
        a = answered.get(f"{nid}_{k}") if isinstance(answered, dict) else None
        try:
            edge, _ = route(n, a, profile)
        except BackendAnswerError:
            edge, a = "unsure", {}
        labels.append(edge)
        conf.append(_confidence(a))
    combined = combine_requests(body_edge, labels)
    if combined not in n["routes"]:
        combined = body_edge
    return body, body_edge, combined, {"n": len(requests), "confidence": conf}


def run(g, event, backend, state=None, playbooks=None, batch=None, split_asks=None):
    """Walk the graph for one event. Returns a trace dict ending in an outcome.

    batch=True asks every judge question of the graph in ONE call the first time a question is needed (after the rules
    have had their say: a route decided by rules alone asks nothing). The plan questions are not part of it: they depend on
    the chosen playbook. The final action is the one a question-by-question walk reaches, provided the backend answers each
    question independently of the others.

    split_asks=True (R35, setting split_asks=1): a chat message with two or more requests (asks.find) has its intent question
    asked once for the message and once for each request, in one call; the routes are combined (combine_requests). A
    message with one request, and every other event, is judged as before. The record keeps only the number and the
    confidences (`asks`). None reads the setting."""
    import os
    from .events import state_of
    from . import plan as planner
    if batch is None:
        batch = config.value("batch_questions") == "1"
    if split_asks is None:
        split_asks = config.value("split_asks") == "1"
    state = state if state is not None else state_of(event)
    nodes, nid, trace, answers, plan = g["nodes"], g["start"], [], {}, None
    pre = {}
    asks_info, asks_changed = None, None   # R35: the numbers of a split message; the combined route when it differs from the body's
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
                   "needs_human": n["kind"] in ("advise", "decide") and bool(n.get("queue", False)),
                   "notify": n["kind"] == "decide" and bool(n.get("notify"))}
            severe = severe_hit(g, nid, event)
            reached = out["notify"] if n["kind"] == "decide" else out["needs_human"]   # a queued decide still needs its notice
            if severe and not reached:
                out["severe_guard"] = severe
                out["notify" if n["kind"] == "decide" else "needs_human"] = True
            elif asks_changed and not reached:   # R35: the requests changed the route, and this outcome would reach nobody
                out["asks_guard"] = asks_changed
                out["notify" if n["kind"] == "decide" else "needs_human"] = True
            if asks_info:
                out["asks"] = asks_info
            if plan:
                out["plan"] = plan
            unsure = [t["node"] for t in trace if t.get("edge") == "unsure"]
            if unsure:   # reached through an answer the model was unsure of: the brief lists it when nobody was asked (brief.unsure_section)
                out["via_unsure"] = unsure
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
        requests = asks.find(state.get("text")) if split_asks and nid == asks.NODE and event.get("kind") == "teams.chat" else []
        if len(requests) >= 2:   # R35: the requests of the message are judged in one call of their own (see _split_intent)
            try:
                ans, edge, combined, asks_info = _split_intent(n, nid, state, backend, requests,
                                                               profiles.for_node(getattr(backend, "profile", None), g, nid))
            except BackendAnswerError as e:   # as for any intent answer that is malformed: the safe route
                batch, pre = False, {}
                trace.append({"node": nid, "answer": {"invalid": str(e)[:200]}, "edge": "unsure"})
                nid = n["routes"]["unsure"]
                continue
            answers[nid] = ans
            trace.append({"node": nid, "answer": {k: v for k, v in ans.items() if k != "type"}, "edge": combined})
            asks_changed = combined if combined != edge else None
            nid = n["routes"][combined]
            continue
        try:
            if batch and not pre:
                pre = backend.ask(state, {k: with_context(nodes[k]["question"], state) for k in reachable_judges(nodes, nid, event)})
            ans = pre[nid] if nid in pre else backend.ask(state, {nid: with_context(n["question"], state)})[nid]
            edge, nxt = route(n, ans, profiles.for_node(getattr(backend, "profile", None), g, nid))   # R21: this question's own bar, if learned
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


EARLIER_NOTE = ("The state's `earlier` lists the messages this chat had before `text`, oldest first (excerpts of up to 120 "
                "characters). They are context only: judge `text`, the message the question is about.")
DEADLINE = re.compile(r"deadline|\bdue\b|\bsoon\b|\bwhen\b|\btoday\b|期限|締切|いつ|今日|今週|までに", re.I)


def with_context(question, state):
    """The judge question with the notes of R22 added to its instructions, when the state carries the earlier messages of a
    chat (`earlier`) or the date (`today`): only for a judge on this PC (see cli._judge_context). The note on the date goes
    to a question about a deadline. A copy: the graph and the playbooks are never changed. Any other state gives the question
    itself, so the judge is asked exactly what it was asked before."""
    if "earlier" not in state and "today" not in state:
        return question
    notes = [EARLIER_NOTE] if "earlier" in state else []
    if state.get("today") and DEADLINE.search(question.get("instructions") or ""):
        notes.append(f"Today is {state['today']}.")
    if not notes:
        return question
    return {**question, "instructions": " ".join([question["instructions"], *notes])}


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
