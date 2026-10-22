"""Final-action evaluation of a dataset made by `kimeru dataset split` (your own reviewed decisions), with its conditions recorded.

  python eval/dataset_eval.py run --dataset DIR --backend stub      # the path only: keyword stub, not an accuracy
  python eval/dataset_eval.py run --dataset DIR --backend kev       # Kev on this PC
  python eval/dataset_eval.py status --dataset DIR [--result FILE]

Only the eval split is scored, with eval/e2e.py (the same walk, labels and severe actions). Before scoring, the dataset files
must still have the hashes of the split and the graphs must still mean what they meant then; otherwise nothing is scored
(PROVENANCE_MISMATCH). The result (default: DIR/result-<backend>.json, outside the repository) records the data version,
the graph fingerprints, the model identity and the scoring method, and keeps six categories apart: correct,
human_confirmation, safe_fallback, wrong_decision, critical_miss, undeterminable. No input is NO_INPUT, never 0%.

Jev: the result is private and no counts are printed or summarized (TypeSafe's terms). The coefficients that
`kimeru calibrate --apply` wrote are not read.
"""
import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kimeru import dataset, profiles  # noqa: E402

_spec = importlib.util.spec_from_file_location("kimeru_eval_e2e", ROOT / "eval" / "e2e.py")
e2e = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(e2e)

SCORING_METHOD = "e2e-final-action"
SCORING_VERSION = "2026-10-20.1"   # C09 final-action scoring; critical misses and undeterminable rows kept apart


def scoring_identity():
    return {"method": SCORING_METHOD, "version": SCORING_VERSION,
            "implementation_sha256": hashlib.sha256((ROOT / "eval" / "e2e.py").read_bytes()).hexdigest(),
            "categories": list(dataset.CATEGORIES), "user_thresholds": False}


def current_fingerprints():
    return dataset.graph_fingerprints(e2e.GRAPHS, e2e.PBS)


def backend_for(name):
    from kimeru.backends import ClmBackend, JevBackend, KevBackend, StubBackend
    return {"stub": StubBackend, "kev": KevBackend, "clm": ClmBackend, "jev": JevBackend}[name]()


def score_rows(rows, backend):
    out = []
    for fx in rows:
        try:
            r = e2e.run_one(fx, backend)
        except Exception as e:   # noqa: BLE001 - an event that cannot be walked is undeterminable, never wrong or right
            out.append({"id": fx.get("id"), "category": "undeterminable", "reason": type(e).__name__})
            continue
        out.append({"id": r["id"], "graph": r["graph"], "category": dataset.category_of(r),
                    "model_node": r["model"], "ideal_node": r["ideal"]})
    return out


def run(dataset_dir, backend, out=None, print_fn=print):
    """Score the eval split. Returns (exit code, result or None)."""
    fps = current_fingerprints()
    problems = dataset.verify_dataset(dataset_dir, fps)
    if problems:
        code = dataset.NO_INPUT if problems[0].startswith(dataset.NO_INPUT) else dataset.MISMATCH
        print_fn(f"{code}: 採点しません。")
        for p in problems:
            print_fn(f"  - {p}")
        return 1, None
    identity = dataset.model_identity(backend)
    path = Path(out) if out else Path(dataset_dir) / f"result-{identity['judge'].lower()}.json"
    if path.exists() or path.is_symlink() or dataset.anonymize.inside_repository(path):
        print_fn("OUTPUT_REJECTED: リポジトリ外の新しい結果ファイルを指定してください（上書きしません）。")
        return 1, None
    rows = dataset.eval_rows(dataset_dir)
    with profiles.ignoring_overrides(True):
        scored = score_rows(rows, backend)
    result = dataset.build_result(dataset_dir, scored, identity, scoring_identity(), fps)
    # Exclusive creation also protects an input/result created while the judge was running.
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    except OSError:
        print_fn("OUTPUT_REJECTED: 新しい結果ファイルを保存できませんでした。既存ファイルは変更しません。")
        return 1, None
    code, lines = dataset.status(dataset_dir, path, fps, scoring_identity())
    for line in lines:
        print_fn(line)
    print_fn(f"結果: {path}" + ("（共有しないでください）" if not result["shareable"] else ""))
    return (0 if code in (dataset.RUN, dataset.PIPELINE_CHECK) else 1), result


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--dataset", required=True)
    p.add_argument("--backend", choices=["stub", "kev", "clm", "jev"], required=True)
    p.add_argument("--out")
    p = sub.add_parser("status")
    p.add_argument("--dataset")
    p.add_argument("--result")
    a = ap.parse_args(argv)
    if a.cmd == "run":
        return run(a.dataset, backend_for(a.backend), a.out)[0]
    code, lines = dataset.status(a.dataset, a.result, current_fingerprints(), scoring_identity())
    for line in lines:
        print(line)
    rm, why = dataset.roadmap_status(a.dataset, a.result, current_fingerprints(), scoring_identity())
    print(f"ロードマップ「実データ（匿名化したイベント）での評価」: {rm}（{why}）")
    return 0 if code in (dataset.RUN, dataset.PIPELINE_CHECK) else 1


if __name__ == "__main__":
    sys.exit(main())
