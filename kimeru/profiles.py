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


def effective(profile=None):
    """The profile with the user's local coefficients laid over it."""
    p = profile or DEFAULT
    o = overrides().get(p.get("name", "jev"))
    return {**p, **o} if o else p


def conf(node, key, default, profile=None):
    """A confidence threshold from the node, scaled by the profile."""
    return node.get(key, default) * effective(profile)["conf_scale"]


def noul_band(node, profile=None):
    s = effective(profile)["noul_scale"]
    return 0.5 + (node.get("yes_at", 0.7) - 0.5) * s, 0.5 - (0.5 - node.get("no_at", 0.3)) * s
