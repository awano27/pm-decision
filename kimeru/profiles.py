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
    # tuned on eval/fixtures.jsonl with Kev-4B (bf16 and fp32 agree): correct 52 -> 59 of 81,
    # to-human 26 -> 19, confidently wrong unchanged at 3. Lower values add wrong decisions.
    "kev": {"conf_scale": 0.8, "noul_scale": 0.75},
}

DEFAULT = PROFILES["jev"]


def conf(node, key, default, profile=None):
    """A confidence threshold from the node, scaled by the profile."""
    return node.get(key, default) * (profile or DEFAULT)["conf_scale"]


def noul_band(node, profile=None):
    s = (profile or DEFAULT)["noul_scale"]
    return 0.5 + (node.get("yes_at", 0.7) - 0.5) * s, 0.5 - (0.5 - node.get("no_at", 0.3)) * s
