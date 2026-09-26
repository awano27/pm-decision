"""Playbook planner: Jev selects and orders pre-written steps; it never writes a plan.

Playbook JSON (playbooks/*.json):
  {"id", "title", "when": <English description used as a choice criterion>,
   "hints": [stub keywords],
   "steps": [{"id", "title": <shown to humans>, "desc": <English, sent to Jev>,
              "check": <noul: is this step needed here?> | omitted = always needed,
              "hints": [stub keywords]}]}

A `plan` node makes two Jev calls:
  1. choice over the allowed playbooks + "none"
  2. one batch: need_<step> (noul) for optional steps, first (choice over steps),
     due_<step> (score: today / this week / next sprint or later)
"""
import json
from pathlib import Path

DUE_LEVELS = ["Today", "Within this week", "Next sprint or later"]
DUE_LABELS = ["今日", "今週", "次スプリント以降"]
DUE_UNSURE = "期限要確認"
DUE_HINTS = {"0": ["至急", "今日", "本日", "urgent", "asap", "blocked", "止まって", "down"],
             "1": ["今週", "this week", "週内"]}


class PlaybookError(ValueError):
    pass


def load_playbooks(d):
    pbs = {}
    for p in sorted(Path(d).glob("*.json")):
        pb = json.loads(p.read_text(encoding="utf-8"))
        validate_playbook(pb)
        if pb["id"] in pbs:
            raise PlaybookError(f"duplicate playbook id {pb['id']}")
        pbs[pb["id"]] = pb
    return pbs


def validate_playbook(pb):
    for k in ("id", "title", "when", "steps"):
        if not pb.get(k):
            raise PlaybookError(f"playbook {pb.get('id')}: missing {k}")
    if pb["id"] == "none":
        raise PlaybookError("playbook id 'none' is reserved")
    ids = [s.get("id") for s in pb["steps"]]
    if len(ids) != len(set(ids)) or not all(ids):
        raise PlaybookError(f"playbook {pb['id']}: step ids must be unique and non-empty")
    if not 2 <= len(ids) <= 8:
        raise PlaybookError(f"playbook {pb['id']}: needs 2-8 steps")
    for s in pb["steps"]:
        if not s.get("title") or not s.get("desc"):
            raise PlaybookError(f"playbook {pb['id']}/{s.get('id')}: title and desc required")


def allowed(node, playbooks):
    ids = node.get("playbooks", "*")
    return list(playbooks) if ids == "*" else ids


def _earlier_adjacent(probs, at=0.3):
    """Earliest level if the probability mass sits on one level or two neighbouring levels
    (each >= `at`); None when it is spread across non-neighbouring levels."""
    heavy = sorted(int(k) for k, p in probs.items() if str(k).isdigit() and p >= at)
    if not heavy or heavy[-1] - heavy[0] > 1:
        return None
    return heavy[0]


def build(node, state, backend, playbooks):
    """Return (edge, plan_or_None, answers). edge is 'ok', 'none' or 'unsure'."""
    ids = allowed(node, playbooks)
    q1 = {"type": "choice",
          "instructions": node.get("instructions", "Which playbook fits the work the project manager must do next?"),
          "criteria": {**{i: playbooks[i]["when"] for i in ids}, "none": "None of these playbooks fits"},
          "hints": {i: playbooks[i].get("hints", []) for i in ids}}
    from .profiles import conf
    prof = getattr(backend, "profile", None)
    a1 = backend.ask(state, {"playbook": q1})["playbook"]
    answers = {"playbook": a1}
    if a1.get("confidence", 0) < conf(node, "min_conf", 0.6, prof):
        return "unsure", None, answers
    if a1["choice"] == "none":
        return "none", None, answers

    pb = playbooks[a1["choice"]]
    steps = pb["steps"]
    qs = {}
    for s in steps:
        if s.get("check"):
            qs[f"need_{s['id']}"] = {"type": "noul", "instructions": s["check"], "hints": s.get("hints", [])}
        qs[f"due_{s['id']}"] = {"type": "score", "criteria": DUE_LEVELS, "hints": DUE_HINTS,
                                "instructions": f"By when should the project manager finish this step: {s['desc']}"}
    qs["first"] = {"type": "choice", "instructions": "Which step should the project manager do first?",
                   "criteria": {s["id"]: s["desc"] for s in steps},
                   "hints": {s["id"]: s.get("hints", []) for s in steps}}
    a2 = backend.ask(state, qs)
    answers.update(a2)

    need_at = node.get("need_at", 0.5)
    due_min_conf = conf(node, "due_min_conf", 0.4, prof)
    chosen = []
    for s in steps:
        p = a2[f"need_{s['id']}"]["noul"] if s.get("check") else 1.0
        if p >= need_at:
            d = a2[f"due_{s['id']}"]
            if d.get("confidence", 0) >= due_min_conf:
                due = min(len(DUE_LEVELS) - 1, max(0, round(d["score"])))
                label = DUE_LABELS[due]
            elif (early := _earlier_adjacent(d.get("probabilities") or {})) is not None:
                # split between neighbouring levels (e.g. today 0.45 / this week 0.44): take the
                # earlier one, the safer mistake for a PM, and mark it as an estimate
                due, label = early, DUE_LABELS[early] + "（目安）"
            else:  # unsure: don't pretend; rank as "this week" and flag it for the PM
                due, label = 1, DUE_UNSURE
            chosen.append({"id": s["id"], "title": s["title"], "need": round(p, 2), "due": label, "due_level": due})
    if not chosen:
        return "unsure", None, answers
    first = a2["first"]
    if first.get("confidence", 0) >= conf(node, "first_min_conf", 0.5, prof):
        chosen.sort(key=lambda s: s["id"] != first["choice"])  # stable: first step, then playbook order
    plan = {"playbook": pb["id"], "title": pb["title"], "steps": chosen,
            "first": chosen[0]["title"],
            "summary": " → ".join(f"{i + 1}.{s['title']}（{s['due']}）" for i, s in enumerate(chosen))}
    return "ok", plan, answers
