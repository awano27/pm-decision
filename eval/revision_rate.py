"""How often the PM asked for a rewrite (修正 N) of a draft, per writer, from the state folder of a real run.

  python eval/revision_rate.py --out <state folder>

numerator   rows of approvals.log.jsonl whose status is the one autorun.REPLY_NAMES calls 修正, of an item a writer drafted
denominator items of approvals.json that carry drafted_by (one per item and writer, however many actions it has)
The files are only read. Neither the drafts nor the instructions are read into the output: it is a table of writer names, counts and rates.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from kimeru.autorun import REPLY_NAMES  # noqa: E402

REVISED = next(status for status, name in REPLY_NAMES.items() if name == "修正")


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _rows(path):
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            yield row


def writers_of(item):
    """The writer names that drafted an action of this item."""
    rec = item.get("record") if isinstance(item, dict) and isinstance(item.get("record"), dict) else {}
    return {a["drafted_by"] for a in rec.get("actions") or [] if isinstance(a, dict) and a.get("drafted_by")}


def rates(out):
    """{writer: {"drafted": n, "revised": m, "rate": m / n}} for a state folder."""
    out = Path(out)
    data = _read_json(out / "approvals.json")
    items = data.get("items") if isinstance(data.get("items"), dict) else {}
    by_writer = {}
    for item in items.values():
        for name in writers_of(item):
            by_writer.setdefault(name, {"drafted": 0, "revised": 0})["drafted"] += 1
    for row in _rows(out / "approvals.log.jsonl"):
        if row.get("status") != REVISED:
            continue
        names = writers_of(items.get(str(row.get("id"))))
        if not names:   # a template the PM revised, or an item no longer in approvals.json: no writer to blame
            continue
        for name in names:
            by_writer.setdefault(name, {"drafted": 0, "revised": 0})["revised"] += 1
    for v in by_writer.values():
        v["rate"] = v["revised"] / v["drafted"] if v["drafted"] else None
    return by_writer


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="the state folder (approvals.json and approvals.log.jsonl are read)")
    a = ap.parse_args()
    by_writer = rates(a.out)
    if not by_writer:
        print("下書きの記録がありません")
        return
    print(f"{'writer':14} {'下書き':>6} {'修正':>6} {'修正率':>7}")
    for name, v in sorted(by_writer.items()):
        rate = "-" if v["rate"] is None else f"{v['rate']:.0%}"
        print(f"{name:14} {v['drafted']:>6} {v['revised']:>6} {rate:>7}")


if __name__ == "__main__":
    main()
