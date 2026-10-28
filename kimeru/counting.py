"""One way to count decisions, shared by the demo summary, `digest --week` and report.html.

human: the item went to the PM (needs_human; an approval to execute or a draft waiting for approval counts too).
rule:  no judgment model was called at all (every step of the path is a rule's match).
safe:  decided on a low-confidence step, with no rule having hit on the way.
auto:  everything else that was decided without the PM.
"""


def is_human(rec):
    return bool(rec.get("needs_human"))


def is_rule_only(rec):
    path = rec.get("path") or []
    return bool(path) and all("matched" in (s.get("answer") or {}) for s in path)


def _rule_hit(rec):
    return any("matched" in (s.get("answer") or {}) and s.get("edge") == "yes" for s in rec.get("path") or [])


def kind(rec):
    """'human', 'safe' or 'auto' (a record is in exactly one of them)."""
    if is_human(rec):
        return "human"
    if any(s.get("edge") == "unsure" for s in rec.get("path") or []) and not _rule_hit(rec) and rec.get("outcome") == "decide":
        return "safe"
    return "auto"


def tally(rows):
    out = {"n": 0, "auto": 0, "safe": 0, "human": 0, "rule": 0}
    for r in rows:
        out["n"] += 1
        out[kind(r)] += 1
        out["rule"] += is_rule_only(r)
    return out
