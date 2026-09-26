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
    "jev": {"conf_scale": 1.0, "noul_scale": 1.0},
    "stub": {"conf_scale": 1.0, "noul_scale": 1.0},
    # Kev-4B on 99 labeled events (197 questions). 0.8/0.75 tuned on the first 39 events
    # added confidently-wrong decisions on 60 held-out ones (3 -> 7), so it was dropped.
    # After making the alert impact levels mutually exclusive, the best setting that adds
    # no confidently-wrong decision is 1.0/0.95 (correct 124 -> 126). Retune on real data.
    "kev": {"conf_scale": 1.0, "noul_scale": 0.95},
}

DEFAULT = PROFILES["jev"]


def conf(node, key, default, profile=None):
    """A confidence threshold from the node, scaled by the profile."""
    return node.get(key, default) * (profile or DEFAULT)["conf_scale"]


def noul_band(node, profile=None):
    s = (profile or DEFAULT)["noul_scale"]
    return 0.5 + (node.get("yes_at", 0.7) - 0.5) * s, 0.5 - (0.5 - node.get("no_at", 0.3)) * s
