"""Per-backend threshold profiles.

Graph thresholds (min_conf, yes_at/no_at, plan confidences) were tuned on Jev. Other
backends report confidence on a different scale: Kev picks the right answer about as
often but with lower confidence, so Jev's thresholds send too much to a human.

Two global knobs, applied on top of each node's own threshold (few knobs = little
room to overfit the small eval set):
  conf_scale  multiplies every confidence threshold (choice/score min_conf, plan
              min_conf/first_min_conf/due_min_conf)
  noul_scale  shrinks the noul "unsure" band toward 0.5:
              yes_at' = 0.5 + (yes_at - 0.5) * noul_scale, no_at' likewise
Tune with: python eval/run_eval.py tune answers.jsonl
"""

PROFILES = {
    "jev": {"name": "jev", "conf_scale": 1.0, "noul_scale": 1.0},
    "stub": {"name": "stub", "conf_scale": 1.0, "noul_scale": 1.0},
    # Kev-4B on 99 labeled events (197 questions). 0.8/0.75 tuned on the first 39 events
    # added confidently-wrong decisions on 60 held-out ones (3 -> 7), so it was dropped.
    # After making the alert impact levels mutually exclusive, the best setting that adds
    # no confidently-wrong decision is 1.0/0.95 (correct 124 -> 126). Retune on real data.
    "kev": {"name": "kev", "conf_scale": 1.0, "noul_scale": 0.95},
    # CLM (experimental): Jev's thresholds until tuned on eval data
    "clm": {"name": "clm", "conf_scale": 1.0, "noul_scale": 1.0},
}

DEFAULT = PROFILES["jev"]

# The user's own coefficients (`kimeru calibrate --apply`) live in a local file, never in the graphs or in this module:
# changing a graph changes the key that marks an event as decided, and the same events would be decided again.
# They are applied here, below every route (daily, watch, run, demo, eval), on top of whatever profile the backend has.
OVERRIDE_FILE = "thresholds.json"
_cache = {"path": None, "mtime": None, "data": {}}


def override_path():
    from . import config
    return config.state_dir() / OVERRIDE_FILE


def overrides():
    """{profile name: {"conf_scale": x, "noul_scale": y}} from the local file ({} when there is none or it is unusable)."""
    import json
    p = override_path()
    try:
        m = p.stat().st_mtime_ns
    except OSError:
        _cache.update(path=str(p), mtime=None, data={})
        return _cache["data"]
    if _cache["path"] == str(p) and _cache["mtime"] == m:
        return _cache["data"]
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    data = {}
    for name, vals in (raw.items() if isinstance(raw, dict) else ()):
        if isinstance(vals, dict):
            ok = {k: float(v) for k, v in vals.items() if k in ("conf_scale", "noul_scale") and isinstance(v, (int, float))
                  and 0.1 <= float(v) <= 3}
            if ok:
                data[name] = ok
    _cache.update(path=str(p), mtime=m, data=data)
    return data


_ignore = {"on": False}


def ignoring_overrides(ignore=True):
    """Context manager: inside it the user's local coefficients are not laid over any profile. The eval scripts use it, so
    a number they print does not depend on what one PC has tuned (they read the file only when asked to)."""
    import contextlib

    @contextlib.contextmanager
    def cm():
        before = _ignore["on"]
        _ignore["on"] = bool(ignore)
        try:
            yield
        finally:
            _ignore["on"] = before
    return cm()


def overrides_note():
    """One line saying which local coefficients are in use ('' when there are none): printed by an eval that read them."""
    o = overrides()
    if not o:
        return ""
    return "注意: 利用者の係数の上書き（" + OVERRIDE_FILE + "）を使っています: " + ", ".join(
        f"{n} conf_scale={v.get('conf_scale', '-')} noul_scale={v.get('noul_scale', '-')}" for n, v in sorted(o.items()))


def effective(profile=None):
    """The profile with the user's local coefficients laid over it."""
    p = profile or DEFAULT
    o = None if _ignore["on"] else overrides().get(p.get("name", "jev"))
    return {**p, **o} if o else p


# R21: one question's bar, learned from the PM's OK on a card (learn.py). It lives under "nodes" in the same file, keyed by
# "<graph name>:<node id>", and scales the judge's coefficients for that question only. Only a question whose unsure route goes
# straight to a person (an advise, or a decide with queue) can have one: where unsure leads to another judge or to a plain
# decision, the entry is ignored, whatever the file says.
NODE_KEY = "nodes"
NODE_COEFFS = ("conf_scale", "noul_scale")
NODE_RANGE = (0.1, 3.0)
JUDGE_NAMES = ("jev", "kev", "clm")


def thresholds_raw():
    """The whole local file as read now (no cache: a card's OK must be seen by the next question). {} when unreadable."""
    import json
    try:
        raw = json.loads(override_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _usable(v):
    import math
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and NODE_RANGE[0] <= v <= NODE_RANGE[1]


def node_overrides():
    """{"<graph>:<node>": {"conf_scale": x, "noul_scale": y}} from the local file. A value that is not a number in range, a key
    that is not a coefficient, or an entry that is not an object is dropped (it changes nothing and never raises)."""
    nodes = thresholds_raw().get(NODE_KEY)
    out = {}
    if not isinstance(nodes, dict):
        return out
    for key, vals in nodes.items():
        if not isinstance(key, str) or not isinstance(vals, dict):
            continue
        ok = {k: float(v) for k, v in vals.items() if k in NODE_COEFFS and _usable(v)}
        if ok:
            out[key] = ok
    return out


def node_key(graph, nid):
    return f"{graph['name']}:{nid}"


def learnable(graph, nid):
    """A judge whose unsure route is an advise, or a decide that waits for the PM (queue), and no other judge or decision."""
    nodes = (graph or {}).get("nodes") or {}
    node = nodes.get(nid) or {}
    if node.get("kind") != "judge":
        return False
    target = nodes.get((node.get("routes") or {}).get("unsure")) or {}
    return target.get("kind") == "advise" or (target.get("kind") == "decide" and bool(target.get("queue")))


def for_node(profile, graph, nid):
    """The profile to route one question with: the judge's coefficients (local file included), scaled by the question's own entry
    when it has one. The name carries the question, so the file's judge-level coefficients are not laid over it a second time.
    Eval scripts (ignoring_overrides), stub and unknown judges, and every question without an entry get the plain profile."""
    base = effective(profile)
    if _ignore["on"] or base.get("name") not in JUDGE_NAMES or not learnable(graph, nid):
        return base
    o = node_overrides().get(node_key(graph, nid))
    if not o:
        return base
    return {**base, "conf_scale": base["conf_scale"] * o.get("conf_scale", 1.0),
            "noul_scale": base["noul_scale"] * o.get("noul_scale", 1.0),
            "name": f"{base['name']}|{node_key(graph, nid)}"}


# The judge that made a recorded answer -> the profile its coefficients belong to. Any other judge (the offline stub, an
# unknown one) has no profile of its own: nothing is tuned or applied for it.
JUDGE_PROFILE = {"Jev": "jev", "Kev": "kev", "CLM": "clm"}


def conf(node, key, default, profile=None):
    """A confidence threshold from the node, scaled by the profile."""
    return node.get(key, default) * effective(profile)["conf_scale"]


def noul_band(node, profile=None):
    s = effective(profile)["noul_scale"]
    return 0.5 + (node.get("yes_at", 0.7) - 0.5) * s, 0.5 - (0.5 - node.get("no_at", 0.3)) * s
