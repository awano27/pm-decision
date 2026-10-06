"""Point a Kev checkpoint at the base model stored next to it, so the bundle runs offline.

Kev keeps its settings in <checkpoint>/head.pt; `base` names the Hugging Face repo of the
base model. This rewrites it to the local folder models/<base name> (wherever the bundle
was unpacked) and clears base_revision. Idempotent; run by start-kev.cmd on every start.
"""
import sys
from pathlib import Path

import torch

here = Path(__file__).resolve().parent
ckpt = here / "models" / sys.argv[1]
head = ckpt / "head.pt"
meta = torch.load(head, map_location="cpu")
repo = meta.get("base_repo") or meta["base"]            # remember the original hub name
local = here / "models" / repo.split("/")[-1]
if not (local / "config.json").exists():
    sys.exit(f"base model not found: {local}")
if meta.get("base") != str(local):
    meta.update(base_repo=repo, base=str(local), base_revision=None)
    torch.save(meta, head)
print(f"{ckpt.name}: base -> {local}")
