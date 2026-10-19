"""T10 (P5): a sieve before the judge is asked. Three rules, each named in the decision's record when it hits:

  bot            a Teams post whose sender is in the setting `bot_names` (empty: the rule is off)
  ado_template   an ADO work item whose description, repro steps and acceptance criteria are all still the empty template
  minutes_none   a minutes line that says only 特になし / なし / 該当なし

It runs after the graph's own safety rules (the `severe` pattern and the match nodes whose id starts with `critical`), so
a serious item is never sieved. prefilter=0 (the default) only records the hit (`prefilter` in the decision: new field, only
on a hit); prefilter=1 also skips the judge: the item goes to the graph's record-only node (an ADO template goes to the
request-for-information nodes). Teams text is never sieved, only the sender is read. Numbers only reach the digest."""
import re
import unicodedata

from . import config, graph

# rule -> (event kind, the graph node a hit goes to at prefilter=1)
RULES = {"bot": ("teams.chat", "log_fyi"),
         "ado_template": ("ado.workitem.created", "info_steps"),
         "minutes_none": ("meeting.item", "log_info")}
_NONE = re.compile(r"^(?:特になし|なし|該当なし)$")


def enabled():
    return config.value("prefilter") == "1"


def _norm(s):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(s or ""))).casefold()


def bot_names():
    """The names in the setting (separated by a comma, 、 or a line break), normalized; empty when none."""
    raw = config.value("bot_names")
    return {n for n in (_norm(p) for p in re.split(r"[,、，\n]", raw or "")) if n}


def rule_of(ev):
    """The rule that names this event, or None. The event alone; whether a safety rule comes first is protected()."""
    kind = ev.get("kind")
    if kind == "teams.chat":
        names = bot_names()
        return "bot" if names and _norm(ev.get("author")) in names else None
    if kind == "ado.workitem.created":
        if ev.get("test_steps") or any(str(ev.get(k) or "").strip() for k in ("description", "repro_steps", "acceptance_criteria")):
            return None
        return "ado_template"
    if kind == "meeting.item":
        item = unicodedata.normalize("NFKC", str(ev.get("item") or "")).strip().rstrip("。.、 ")
        return "minutes_none" if _NONE.match(item) else None
    return None


def protected(g, ev):
    """Does the graph's own safety net take this event (the severe pattern, or a match node `critical*` saying yes or mixed)?"""
    if graph.severe_hit(g, None, ev):
        return True
    for nid, n in g["nodes"].items():
        if n.get("kind") == "match" and nid.startswith("critical") and graph.match_eval(n, ev)[0] in ("yes", "mixed"):
            return True
    return False


def check(g, ev):
    """None, or {"rule": name, "applied": bool, "start": node}. `applied` is true only at prefilter=1 and only when the graph
    has the node the hit goes to (and an ADO item the graph reads as a planning item, an Epic or Feature, is left alone)."""
    rule = rule_of(ev)
    if not rule or RULES[rule][0] != ev.get("kind") or protected(g, ev):
        return None
    nodes = g["nodes"]
    if rule == "ado_template" and "plan_level" in nodes and graph.match_eval(nodes["plan_level"], ev)[0] == "yes":
        return None
    start = RULES[rule][1]
    return {"rule": rule, "applied": enabled() and start in nodes, "start": start}


def counts(rows):
    """{rule: n} over decision records that carry a hit."""
    out = {}
    for r in rows:
        p = r.get("prefilter")
        if isinstance(p, dict) and isinstance(p.get("rule"), str):
            out[p["rule"]] = out.get(p["rule"], 0) + 1
    return out
