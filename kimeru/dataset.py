"""`kimeru dataset`: from your own labeled decisions to an evaluation set whose conditions can be traced. Local files only.

The labeled rows come from `kimeru review` (`<state folder>/fixtures_user.jsonl`, the same {"id", "event", "expect"} shape as
eval/fixtures.jsonl). This module adds what that file does not have:

  anonymize      an anonymization *candidate* and a private review list (kimeru/anonymize.py); never called anonymous
  mark-reviewed  a person's explicit review, bound to the exact bytes reviewed
  split          a deterministic tune (calibration) / eval split, with the checks below, a data version and graph fingerprints
  check          leakage between two files you split yourself
  status         what has and has not been run. "No dataset" is NO_INPUT and "no result" is NOT_RUN, never 0%

Checks, each reported by row id and line (never by content): missing fields, rows that cannot be scored with the current graphs,
exact duplicates, the same event labeled two ways, and near-duplicates (normalized text). Near-duplicates are kept in the same
split, so the split itself cannot leak; `check` finds leakage between files made some other way.

The scoring itself is eval/dataset_eval.py (it reuses eval/e2e.py): it records the data version, the graph fingerprints, the
model identity and the scoring method in the result, and a result whose claims do not match is reported as
PROVENANCE_MISMATCH, not as a score. Jev results stay private: no counts are printed for Jev (TypeSafe's terms).
"""
import argparse
import hashlib
import json
import os
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from . import anonymize, config

FORMAT = "kimeru-dataset/v1"
RESULT_FORMAT = "kimeru-eval-result/v1"
MANIFEST = "dataset.json"
TUNE, EVAL = "tune.jsonl", "eval.jsonl"
SPLIT_SALT = "kimeru-split-v1"
NEAR_DUP = 0.9

# Outcome categories of a final-action evaluation. They are never merged: a critical miss is not counted as an ordinary
# wrong decision, and an event that could not be judged is not counted as wrong (or right).
CATEGORIES = ("correct", "human_confirmation", "safe_fallback", "wrong_decision", "critical_miss", "undeterminable")
LABELS_JA = {"correct": "正しい行動", "human_confirmation": "人の確認へ", "safe_fallback": "安全側の代替行動",
             "wrong_decision": "誤った行動", "critical_miss": "重大な取りこぼし", "undeterminable": "判定できない"}
_E2E_OUTCOME = {"correct": "correct", "human": "human_confirmation", "fallback": "safe_fallback", "wrong": "wrong_decision"}

NO_INPUT, NOT_RUN, MISMATCH = "NO_INPUT", "NOT_RUN", "PROVENANCE_MISMATCH"
PIPELINE_CHECK, RUN = "PIPELINE_CHECK", "RUN"
STUB_JUDGES = ("StubBackend", "stub")

REQUIRED_EVENT_FIELDS = {"teams.chat": ("text",), "ado.workitem.created": ("title",), "monitor.alert": ("rule", "severity"),
                         "meeting.item": ("item",)}
_IGNORED_FOR_TEXT = {"id", "chat_id", "ts", "index", "kind"}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_graphs(graphs_dir=None, playbooks_dir=None):
    """({event kind: graph}, playbooks) as the judge uses them (the first graph of each kind)."""
    from . import graph as graph_mod, plan, resources
    pbs = plan.load_playbooks(playbooks_dir or resources.resource_dir("playbooks"))
    gs = graph_mod.load_dir(graphs_dir or resources.resource_dir("graphs"), pbs)
    return {k: v[0] for k, v in gs.items()}, pbs


def graph_fingerprints(graphs, playbooks):
    """Evaluation depends on routing thresholds as well as graph and playbook meaning.

    Calibration deliberately ignores thresholds; its key must not be reused for evaluation provenance.
    """
    return {k: "eval-v2:" + hashlib.sha256(_canon({"graph": g, "playbooks": playbooks}).encode("utf-8")).hexdigest()
            for k, g in sorted(graphs.items())}


def model_identity(backend):
    """{"judge", "model", "model_key"} of a backend, seen through wrappers; the key is the hash of the full model id."""
    from .backends import judge_name
    inner, seen = backend, 0
    while hasattr(inner, "inner") and seen < 5:
        inner, seen = inner.inner, seen + 1
    model = getattr(inner, "model", None)
    model = model if isinstance(model, str) and model.strip() and len(model) < 200 else None
    return {"judge": judge_name(backend), "model": model,
            "model_key": hashlib.sha256(model.encode("utf-8")).hexdigest() if model else None}


def is_jev_identity(identity):
    return str((identity or {}).get("judge", "")).strip().casefold() == "jev"


def read_rows(path):
    """([(line number, row dict)], [unreadable line numbers])."""
    rows, bad = [], []
    text = Path(path).read_bytes().decode("utf-8-sig")
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except ValueError:
            bad.append(n)
            continue
        if isinstance(r, dict):
            rows.append((n, r))
        else:
            bad.append(n)
    return rows, bad


def _issue(kind, line, rid, detail):
    return {"kind": kind, "line": line, "id": rid if isinstance(rid, str) else None, "detail": detail}


def _label_problem(node, label, playbooks):
    """None when the label has the shape this node's answers are scored against, else why not."""
    from . import plan
    if node["kind"] == "plan":
        ok = set(plan.allowed(node, playbooks)) | {"none"}
        vals = label if isinstance(label, list) else [label]
        return None if vals and all(isinstance(v, str) and v in ok for v in vals) else "playbook not allowed here"
    q = node.get("question") or {}
    t = q.get("type")
    if t == "choice":
        vals = label if isinstance(label, list) else [label]
        return None if vals and all(isinstance(v, str) and v in (q.get("criteria") or {}) for v in vals) else "not an option"
    if t == "noul":
        vals = label if isinstance(label, list) else [label]
        return None if vals and all(isinstance(v, bool) for v in vals) else "not true/false"
    if t == "score":
        if (isinstance(label, list) and len(label) == 2 and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in label)
                and label[0] <= label[1]):
            return None
        return "not a [low, high] range"
    return "unknown question type"


def validate_rows(rows, graphs, playbooks):
    """(rows that can be scored, issues). Missing fields and unscorable rows are excluded, each with a reason."""
    good, issues = [], []
    for n, r in rows:
        rid = r.get("id")
        if not isinstance(rid, str) or not rid.strip():
            issues.append(_issue("missing_field", n, None, "id"))
            continue
        ev, exp = r.get("event"), r.get("expect")
        if not isinstance(ev, dict):
            issues.append(_issue("missing_field", n, rid, "event"))
            continue
        if not isinstance(ev.get("kind"), str) or not ev["kind"]:
            issues.append(_issue("missing_field", n, rid, "event.kind"))
            continue
        if not isinstance(exp, dict) or not exp:
            issues.append(_issue("missing_field", n, rid, "expect"))
            continue
        missing = [f for f in REQUIRED_EVENT_FIELDS.get(ev["kind"], ()) if ev.get(f) in (None, "")]
        if missing:
            issues.append(_issue("missing_field", n, rid, "event." + ",".join(missing)))
            continue
        g = graphs.get(ev["kind"])
        if g is None:
            issues.append(_issue("unscorable", n, rid, f"no graph for {ev['kind']}"))
            continue
        scorable, bad = 0, []
        for nid, label in exp.items():
            node = g["nodes"].get(nid)
            if not node or node.get("kind") not in ("judge", "plan"):
                continue   # a label for a node this graph no longer asks: not scored (as in eval/run_eval.py)
            problem = _label_problem(node, label, playbooks)
            if problem:
                bad.append(f"{nid}: {problem}")
            else:
                scorable += 1
        if bad:
            issues.append(_issue("unscorable", n, rid, "; ".join(bad)))
            continue
        if not scorable:
            issues.append(_issue("unscorable", n, rid, "no label for a judge or plan node of the current graph"))
            continue
        good.append((n, r))
    return good, issues


def _canon(v):
    return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalized_text(event):
    """The words of an event without ids, placeholder numbers, digits, spaces and punctuation."""
    parts = [str(event.get("kind", ""))]

    def walk(v):
        if isinstance(v, str):
            parts.append(v)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, dict):
            for k in sorted(v):
                if k not in _IGNORED_FOR_TEXT:
                    walk(v[k])
    walk({k: v for k, v in event.items() if k not in _IGNORED_FOR_TEXT})
    s = unicodedata.normalize("NFKC", " ".join(parts)).casefold()
    s = re.sub(r"<([a-z_]+?)_\d+>", r"<\1>", s)
    s = re.sub(r"\d", "0", s)
    return re.sub(r"[^\w<>]", "", s)


def _grams(s):
    return {s[i:i + 3] for i in range(max(1, len(s) - 2))}


def _similar(a, b, ga, gb):
    if a == b:
        return True
    if min(len(a), len(b)) < 10:
        return False
    inter = len(ga & gb)
    return inter / max(1, len(ga | gb)) >= NEAR_DUP


def dedupe(rows):
    """(kept rows, issues): an exact repeat is dropped, an id used for two different rows or one event labeled two ways is
    dropped entirely (which label is right cannot be decided here)."""
    issues, by_id, order = [], {}, []
    for n, r in rows:
        by_id.setdefault(r["id"], []).append((n, r))
        if len(by_id[r["id"]]) == 1:
            order.append(r["id"])
    kept = []
    for rid in order:
        group = by_id[rid]
        first = group[0]
        if any(_canon(r) != _canon(first[1]) for _, r in group[1:]):
            issues += [_issue("id_conflict", n, rid, "the same id holds different rows") for n, _ in group]
            continue
        issues += [_issue("duplicate", n, rid, f"exact repeat of line {first[0]}") for n, _ in group[1:]]
        kept.append(first)
    by_event = {}
    for n, r in kept:
        by_event.setdefault(_canon(r["event"]), []).append((n, r))
    out = []
    for n, r in kept:
        group = by_event[_canon(r["event"])]
        if len(group) == 1:
            out.append((n, r))
            continue
        if len({_canon(x["expect"]) for _, x in group}) > 1:
            issues.append(_issue("label_conflict", n, r["id"], "the same event is labeled differently in another row"))
            continue
        winner = min(group, key=lambda x: x[1]["id"])
        if winner[1]["id"] == r["id"]:
            out.append((n, r))
        else:
            issues.append(_issue("duplicate", n, r["id"], f"same event and labels as {winner[1]['id']}"))
    return out, issues


def clusters(rows):
    """Union of near-duplicates: {row id: cluster key (the smallest id in the cluster)}."""
    ids = [r["id"] for _, r in rows]
    texts = [normalized_text(r["event"]) for _, r in rows]
    grams = [_grams(t) for t in texts]
    parent = list(range(len(ids)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    exact = {}
    for i, t in enumerate(texts):
        if t in exact:
            parent[find(i)] = find(exact[t])
        else:
            exact[t] = i
    if len(ids) <= 3000:   # pairwise; beyond that only exact normalized text is grouped
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                if find(i) != find(j) and _similar(texts[i], texts[j], grams[i], grams[j]):
                    parent[find(j)] = find(i)
    members = {}
    for i in range(len(ids)):
        members.setdefault(find(i), []).append(ids[i])
    return {rid: min(m) for m in members.values() for rid in m}


def split_of(cluster_key, tune_percent):
    bucket = int(hashlib.sha256(f"{SPLIT_SALT}:{cluster_key}".encode("utf-8")).hexdigest(), 16) % 100
    return "tune" if bucket < tune_percent else "eval"


def check_leakage(tune_rows, eval_rows):
    """Issues for eval rows that repeat a tune row: the same id, the same event, or near-duplicate text."""
    issues = []
    t_ids = {r.get("id") for _, r in tune_rows}
    t_events = {_canon(r.get("event")) for _, r in tune_rows}
    t_texts = [(r.get("id"), normalized_text(r["event"])) for _, r in tune_rows if isinstance(r.get("event"), dict)]
    t_grams = [(rid, t, _grams(t)) for rid, t in t_texts]
    for n, r in eval_rows:
        rid = r.get("id")
        if rid in t_ids:
            issues.append(_issue("leakage", n, rid, "the same id is in the tune data"))
            continue
        if _canon(r.get("event")) in t_events:
            issues.append(_issue("leakage", n, rid, "the same event is in the tune data"))
            continue
        if not isinstance(r.get("event"), dict):
            continue
        t = normalized_text(r["event"])
        g = _grams(t)
        hit = next((tid for tid, tt, tg in t_grams if _similar(t, tt, g, tg)), None)
        if hit is not None:
            issues.append(_issue("leakage", n, rid, f"near-duplicate of tune row {hit}"))
    return issues


def _write_jsonl(path, rows):
    data = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows).encode("utf-8")
    Path(path).write_bytes(data)
    return anonymize.sha256_bytes(data)


def _write_json(path, obj):
    p = Path(path)
    tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def _data_version(files, split):
    return "v1:" + hashlib.sha256(_canon({"files": {k: v["sha256"] for k, v in sorted(files.items())},
                                          "split": split}).encode("utf-8")).hexdigest()


def _source(src):
    """(path of the rows, anonymization status, reasons, origin) for a candidate folder or a plain JSONL file."""
    src = Path(src)
    if src.is_dir():
        m = anonymize.read_manifest(src)
        if m is None:
            raise ValueError(f"{src} は匿名化候補のフォルダではありません")
        st, reasons = anonymize.status(src)
        return src / anonymize.CANDIDATE, st, reasons, (m.get("source") or {}).get("origin", "other")
    if not src.is_file():
        raise FileNotFoundError(f"{NO_INPUT}: {src} がありません")
    rows, _ = read_rows(src)
    origin = "user_reviews" if rows and all(str(r.get("id", "")).startswith("user-") for _, r in rows) else "other"
    return src, anonymize.STATUS_NOT_ANONYMIZED, ["匿名化していない元のデータです（このPCの中だけで使ってください）"], origin


def split(src, dest, tune_percent=50, graphs=None, playbooks=None, now=None):
    """Validate, dedupe, cluster and split `src` into `dest` (a new or empty folder). Returns the manifest."""
    if not 0 <= int(tune_percent) <= 100:
        raise ValueError("tune_percent must be 0..100")
    dest = Path(dest)
    if dest.exists() and any(dest.iterdir()):
        raise ValueError(f"{dest} は空ではありません。新しいフォルダを指定してください（上書きしません）")
    if anonymize.inside_repository(dest):
        raise ValueError(f"{dest} はリポジトリの中です。評価データはリポジトリの外に置いてください")
    path, anon_status, anon_reasons, origin = _source(src)
    if graphs is None:
        graphs, playbooks = load_graphs()
    src_hash = anonymize.sha256_file(path)
    rows, bad = read_rows(path)
    issues = [_issue("unreadable", n, None, "not a JSON object") for n in bad]
    good, more = validate_rows(rows, graphs, playbooks)
    issues += more
    kept, more = dedupe(good)
    issues += more
    groups = clusters(kept)
    near = sum(1 for rid, key in groups.items() if rid != key)
    tune, ev = [], []
    for n, r in kept:
        (tune if split_of(groups[r["id"]], int(tune_percent)) == "tune" else ev).append((n, r))
    leaks = check_leakage(tune, ev)   # by construction none; kept as a guard against a future change of the split
    issues += leaks
    leaked = {i["id"] for i in leaks}
    ev = [(n, r) for n, r in ev if r["id"] not in leaked]
    dest.mkdir(parents=True, exist_ok=True)
    files = {"tune": {"file": TUNE, "rows": len(tune), "sha256": _write_jsonl(dest / TUNE, [r for _, r in tune])},
             "eval": {"file": EVAL, "rows": len(ev), "sha256": _write_jsonl(dest / EVAL, [r for _, r in ev])}}
    split_params = {"method": "sha256(salt:cluster min id) % 100 < tune_percent", "salt": SPLIT_SALT,
                    "tune_percent": int(tune_percent), "near_duplicate": f"char 3-gram Jaccard >= {NEAR_DUP} on normalized text"}
    counts = {}
    for i in issues:
        counts[i["kind"]] = counts.get(i["kind"], 0) + 1
    manifest = {
        "format": FORMAT, "created_at": now or _now(),
        "source": {"name": Path(src).name, "sha256": src_hash, "rows": len(rows)},
        "anonymization": {"status": anon_status, "reasons": anon_reasons},
        "origin": origin,
        "split": split_params,
        "files": files,
        "data_version": _data_version(files, split_params),
        "graphs": graph_fingerprints(graphs, playbooks),
        "near_duplicate_rows": near,
        "issue_counts": dict(sorted(counts.items())),
        "issues": issues,
    }
    _write_json(dest / MANIFEST, manifest)
    return manifest


def read_manifest(dest):
    if dest is None:
        return None
    p = Path(dest) / MANIFEST
    if not p.is_file():
        return None
    try:
        m = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return None
    return m if isinstance(m, dict) and m.get("format") == FORMAT else None


def verify_dataset(dest, fingerprints=None):
    """Problems that make the dataset's claims untrue now: files changed since the split, graphs changed, leakage."""
    m = read_manifest(dest)
    if m is None:
        return [f"{NO_INPUT}: データセットのマニフェストがありません"]
    problems = []
    files = m.get("files")
    if not isinstance(files, dict) or set(files) != {"tune", "eval"}:
        return ["データセットの tune / eval の来歴が欠けています"]
    for part, expected in (("tune", TUNE), ("eval", EVAL)):
        f = files[part]
        if (not isinstance(f, dict) or f.get("file") != expected
                or type(f.get("rows")) is not int or f["rows"] < 0
                or not isinstance(f.get("sha256"), str)):
            return ["データセットのファイル名・行数・ハッシュが不正です"]
    if not isinstance(m.get("graphs"), dict) or not isinstance(m.get("split"), dict):
        return ["データセットのグラフ・分割条件が欠けています"]
    if any(not isinstance(v, str) or not v for v in m["graphs"].values()):
        return ["データセットのグラフ fingerprint が不正です"]
    counts = m.get("issue_counts")
    if not isinstance(counts, dict) or any(type(v) is not int or v < 0 for v in counts.values()):
        return ["データセットの除外件数が不正です"]
    if not isinstance(m.get("anonymization"), dict):
        return ["データセットの匿名化確認の来歴が欠けています"]
    for part, f in files.items():
        p = Path(dest) / str(f.get("file"))
        if not p.is_file():
            problems.append(f"{part}: {f.get('file')} がありません")
        elif anonymize.sha256_file(p) != f.get("sha256"):
            problems.append(f"{part}: {f.get('file')} が分割したあとに変わりました（データ版が一致しません）")
        else:
            rows, bad = read_rows(p)
            if bad or len(rows) != f["rows"]:
                problems.append(f"{part}: 行数または JSONL 形式が来歴と一致しません")
    if _data_version(files, m.get("split")) != m.get("data_version"):
        problems.append("データ版がファイルのハッシュと一致しません")
    if fingerprints is not None and fingerprints != m.get("graphs"):
        changed = sorted(k for k in set(fingerprints) | set(m.get("graphs") or {})
                         if fingerprints.get(k) != (m.get("graphs") or {}).get(k))
        problems.append("グラフ（質問・分岐の意味）が分割したときと違います: " + ", ".join(changed) + "。split し直してください")
    if not problems:
        tune_rows, _ = read_rows(Path(dest) / TUNE)
        eval_rows, _ = read_rows(Path(dest) / EVAL)
        leaks = check_leakage(tune_rows, eval_rows)
        if leaks:
            problems.append(f"eval に tune と重複・近似重複する行があります（{len(leaks)} 件）")
    return problems


def eval_rows(dest):
    p = Path(dest) / EVAL
    return [r for _, r in read_rows(p)[0]] if p.is_file() else []


def category_of(e2e_row):
    """One of CATEGORIES for a row of eval/e2e.py run_one (critical miss kept apart from an ordinary wrong decision)."""
    if not isinstance(e2e_row, dict) or e2e_row.get("outcome") not in _E2E_OUTCOME:
        return "undeterminable"
    if e2e_row.get("severe_miss"):
        return "critical_miss"
    return _E2E_OUTCOME[e2e_row["outcome"]]


def tally(rows):
    counts = {c: 0 for c in CATEGORIES}
    for r in rows:
        counts[r["category"] if r.get("category") in counts else "undeterminable"] += 1
    return counts


def build_result(dest, rows, identity, scoring, fingerprints, now=None):
    """The result file's content. Jev: no counts (rows stay for this PC only); stub: a pipeline check, not an accuracy."""
    m = read_manifest(dest) or {}
    jev = is_jev_identity(identity)
    stub = identity.get("judge") in STUB_JUDGES
    if not rows:
        summary = NO_INPUT
    elif jev:
        summary = "WITHHELD_JEV"
    else:
        summary = tally(rows)
    return {
        "format": RESULT_FORMAT, "created_at": now or _now(),
        "run_kind": "pipeline_check" if stub else "evaluation",
        "shareable": not jev,
        "private_reason": "Jev の評価結果は公開・共有できません（TypeSafe の利用規約）" if jev else None,
        "provenance": {
            "data_version": m.get("data_version"),
            "eval_sha256": (m.get("files") or {}).get("eval", {}).get("sha256"),
            "graphs": fingerprints,
            "model": identity,
            "scoring": scoring,
            "anonymization_status": (m.get("anonymization") or {}).get("status"),
            "origin": m.get("origin"),
        },
        "summary": summary,
        "rows": rows,
    }


def check_result(result, dest, fingerprints=None, scoring=None, identity=None):
    """Mismatches between what a result claims and the dataset, graphs, model and scoring it names. [] when consistent."""
    if not isinstance(result, dict) or result.get("format") != RESULT_FORMAT:
        return ["評価結果のファイルではありません"]
    m = read_manifest(dest)
    if m is None:
        return ["結果が指すデータセットがありません"]
    problems = verify_dataset(dest, None)
    if problems:
        return problems
    prov = result.get("provenance")
    rows = result.get("rows")
    if (not isinstance(prov, dict) or not isinstance(prov.get("model"), dict)
            or not isinstance(prov.get("scoring"), dict) or not isinstance(rows, list)
            or any(not isinstance(r, dict) or not isinstance(r.get("id"), str)
                   or r.get("category") not in CATEGORIES for r in rows)):
        return ["結果の来歴・行 ID・採点分類が欠けているか不正です"]
    summary = result.get("summary")
    if not isinstance(summary, dict) and summary not in ("WITHHELD_JEV", NO_INPUT):
        return ["結果の集計が欠けているか不正です"]
    if ((summary == NO_INPUT and rows)
            or (summary == "WITHHELD_JEV" and not is_jev_identity(prov["model"]))):
        return ["結果の集計状態が行・モデルと一致しません"]
    out = []
    if prov.get("data_version") != m.get("data_version"):
        out.append("データ版が違います（別のデータセット、または分割し直す前の結果）")
    if prov.get("eval_sha256") != (m.get("files") or {}).get("eval", {}).get("sha256"):
        out.append("eval データのハッシュが違います")
    if fingerprints is not None and prov.get("graphs") != fingerprints:
        out.append("グラフの fingerprint が今のグラフと違います")
    if prov.get("graphs") != m.get("graphs"):
        out.append("グラフの fingerprint がデータセットを作ったときと違います")
    model = prov.get("model") or {}
    if model.get("model") and model.get("model_key") != hashlib.sha256(str(model["model"]).encode("utf-8")).hexdigest():
        out.append("モデル ID とそのハッシュが一致しません")
    if identity is not None and (identity.get("judge"), identity.get("model_key")) != (model.get("judge"), model.get("model_key")):
        out.append("結果のモデルが指定したモデルと違います")
    if scoring is not None and prov.get("scoring") != scoring:
        out.append("採点方法（方式・版・実装のハッシュ）が今と違います")
    if (prov.get("scoring") or {}).get("categories") not in (None, list(CATEGORIES)):
        out.append("結果の分類が今の分類と違います")
    rows = result.get("rows") or []
    want = sorted(r.get("id") for r in eval_rows(dest))
    if sorted(r.get("id") for r in rows) != want:
        out.append("結果の行が eval データの行と一致しません")
    summary = result.get("summary")
    if isinstance(summary, dict) and summary != tally(rows):
        out.append("集計が行の分類と一致しません")
    if is_jev_identity(model) and summary not in ("WITHHELD_JEV", NO_INPUT):
        out.append("Jev の結果に件数の集計が含まれています（出してはいけません）")
    return out


def load_result(path):
    if path is None or not Path(path).is_file():
        return None
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except ValueError:
        return {"format": "unreadable"}


def status(dest, result_path=None, fingerprints=None, scoring=None):
    """(code, lines). Codes: NO_INPUT, NOT_RUN, PROVENANCE_MISMATCH, PIPELINE_CHECK (stub), RUN. No code prints 0% for
    missing data, and none prints counts for Jev."""
    m = read_manifest(dest)
    if m is None:
        return NO_INPUT, [f"{NO_INPUT}: 評価用データセットがありません（kimeru dataset split で作ります）。精度は表示しません。"]
    problems = verify_dataset(dest, fingerprints)
    if problems:
        return MISMATCH, [f"{MISMATCH}: データセットの来歴が一致しないため、数字は出しません。"] + problems
    head = [f"データ版: {m.get('data_version')}",
            f"匿名化: {(m.get('anonymization') or {}).get('status')}（匿名化済みとは扱いません。人の確認の状態です）",
            f"tune {m['files']['tune']['rows']} 行 / eval {m['files']['eval']['rows']} 行 / 除外 {sum((m.get('issue_counts') or {}).values())} 件"
            + (f"（{', '.join(f'{k} {v}' for k, v in m['issue_counts'].items())}）" if m.get("issue_counts") else "")]
    if not m["files"]["eval"]["rows"]:
        return NO_INPUT, head + [f"{NO_INPUT}: eval データが 0 行です。精度は表示しません（0% ではありません）。"]
    result = load_result(result_path)
    if result is None:
        return NOT_RUN, head + [f"{NOT_RUN}: このデータセットの評価結果はありません。精度は表示しません（0% ではありません）。"]
    problems = check_result(result, dest, fingerprints, scoring)
    if problems:
        return MISMATCH, head + [f"{MISMATCH}: 結果の条件がデータ・グラフ・モデル・採点方法と一致しないため、数字は出しません。"] + [
            f"  - {p}" for p in problems]
    model = (result.get("provenance") or {}).get("model") or {}
    if result.get("summary") == NO_INPUT:
        return NO_INPUT, head + [f"{NO_INPUT}: 採点できた行がありません。精度は表示しません。"]
    if is_jev_identity(model):
        return RUN, head + ["判断モデルが Jev のため、件数の内訳は出しません（TypeSafe の利用規約）。結果は共有しないでください。"]
    s = result["summary"]
    n = sum(s.values())
    line = " / ".join(f"{LABELS_JA[c]} {s[c]}" for c in CATEGORIES) + f"（計 {n} 件）"
    if result.get("run_kind") == "pipeline_check":
        return PIPELINE_CHECK, head + [f"{PIPELINE_CHECK}: stub（キーワード規則）での経路確認です。判断精度ではありません。", "  " + line]
    return RUN, head + [f"判断モデル: {model.get('judge')}", "  " + line]


def roadmap_status(dest, result_path=None, fingerprints=None, scoring=None):
    """(code, reason) for the roadmap item "evaluation on real (anonymized) data". It is DONE only with a consistent,
    non-stub result on rows that came from `kimeru review`, from a candidate a person marked reviewed."""
    code, _ = status(dest, result_path, fingerprints, scoring)
    if code in (NO_INPUT, NOT_RUN):
        return code, "実データでの評価結果がありません"
    if code != RUN:
        return NOT_RUN, f"有効な評価結果がありません（{code}）"
    if fingerprints is None or scoring is None:
        return NOT_RUN, "現在のグラフ・採点実装との照合がありません。eval/dataset_eval.py status で確認してください"
    m = read_manifest(dest) or {}
    if m.get("origin") != "user_reviews":
        return NOT_RUN, "データが kimeru review の記録から作られていません"
    if (m.get("anonymization") or {}).get("status") != anonymize.STATUS_REVIEWED:
        return NOT_RUN, "匿名化候補が人に確認されていません"
    result = load_result(result_path) or {}
    if all(r.get("category") == "undeterminable" for r in result.get("rows", [])):
        return NOT_RUN, "全行が判定不能のため、実データ評価の完了とは扱いません"
    return "DONE", "人が確認した匿名化候補での評価結果があります"


def _default_dir(prefix):
    return config.state_dir() / "datasets" / f"{prefix}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"


def dispatch(argv, out=None, print_fn=print):
    """`kimeru dataset ...`. 0 done, 1 nothing to work on / problems found, 2 usage."""
    from . import review
    ap = argparse.ArgumentParser(prog="kimeru dataset")
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("anonymize", help="anonymization candidate + private review list (the source is not changed)")
    p.add_argument("source", nargs="?", help="labeled rows (default: <state folder>/fixtures_user.jsonl)")
    p.add_argument("--to", help="new or empty folder (default: <state folder>/datasets/candidate-<time>)")
    p.add_argument("--terms", help="file with one name / customer / product per line to replace as well")
    p = sub.add_parser("mark-reviewed", help="record that you read the review list and the candidate")
    p.add_argument("folder")
    p.add_argument("--i-reviewed", action="store_true", help="required: you read every replacement and risk spot")
    p.add_argument("--by", default="", help="initials (optional)")
    p = sub.add_parser("split", help="tune / eval split with checks and a data version")
    p.add_argument("source", help="a candidate folder, or a JSONL file of labeled rows")
    p.add_argument("--to", help="new or empty folder (default: <state folder>/datasets/split-<time>)")
    p.add_argument("--tune-percent", type=int, default=50)
    p = sub.add_parser("check", help="leakage between a tune file and an eval file")
    p.add_argument("tune")
    p.add_argument("eval")
    p = sub.add_parser("status", help="what has been run on a dataset (NO_INPUT / NOT_RUN / ...)")
    p.add_argument("folder", nargs="?")
    p.add_argument("--result", help="a result file from eval/dataset_eval.py")
    try:
        a = ap.parse_args(list(argv))
    except SystemExit as e:
        return int(e.code or 0)
    try:
        if a.command == "anonymize":
            src = Path(a.source) if a.source else review.fixtures_path()
            if not src.is_file():
                print_fn(f"{NO_INPUT}: {src} がありません（kimeru review で正誤を付けると作られます）")
                return 1
            terms = []
            if a.terms:
                terms = [l.strip() for l in Path(a.terms).read_text(encoding="utf-8-sig").splitlines() if l.strip()]
            try:
                graphs, pbs = load_graphs()
            except (OSError, ValueError):
                graphs, pbs = None, None
            dest = Path(a.to) if a.to else _default_dir("candidate")
            m = anonymize.make_candidate(src, dest, terms=terms, graphs=graphs, playbooks=pbs)
            print_fn(f"匿名化の候補を作りました: {dest}（status: candidate, human_review_required）")
            print_fn(anonymize.WARNING)
            r = m["residual_risks"]
            print_fn(f"置換 {sum(m['replacements'].values())} 件 / 残るリスク {r['spots']} 箇所 / 置換したはずの値の残り "
                     f"{r['leftover_originals']} 件 / 経路が変わった行 {r['route_changes']} 件")
            print_fn(f"確認: {dest / anonymize.REVIEW} を読んでください（元の値を含みます。共有しないでください）")
            return 0
        if a.command == "mark-reviewed":
            if not a.i_reviewed:
                print_fn("確認したことを明示してください: --i-reviewed（レビュー表と candidate.jsonl を読んだあとで）")
                return 2
            anonymize.mark_reviewed(a.folder, True, by=a.by)
            st, reasons = anonymize.status(a.folder)
            print_fn(f"記録しました: {st}（{'; '.join(reasons)}）。この後に内容が変わると確認は無効になります。")
            print_fn("これは「人が確認した候補」です。匿名化を保証するものではありません。")
            return 0
        if a.command == "split":
            dest = Path(a.to) if a.to else _default_dir("split")
            m = split(a.source, dest, a.tune_percent)
            print_fn(f"分割しました: {dest}")
            for line in status(dest)[1]:
                print_fn(line)
            return 0
        if a.command == "check":
            leaks = check_leakage(read_rows(a.tune)[0], read_rows(a.eval)[0])
            for i in leaks:
                print_fn(f"leakage: line {i['line']} {i['id']}: {i['detail']}")
            print_fn(f"重複・近似重複: {len(leaks)} 件" if leaks else "tune と eval の重複・近似重複はありません")
            return 1 if leaks else 0
        graphs, pbs = load_graphs()
        fingerprints = graph_fingerprints(graphs, pbs)
        code, lines = status(a.folder, a.result, fingerprints)
        for line in lines:
            print_fn(line)
        print_fn("採点実装は未照合です。現在の採点実装との一致は eval/dataset_eval.py status で確認してください。")
        rm, why = roadmap_status(a.folder, a.result, fingerprints)
        print_fn(f"ロードマップ「実データ（匿名化したイベント）での評価」: {rm}（{why}）")
        return 0 if code in (RUN, PIPELINE_CHECK) else 1
    except FileNotFoundError as e:
        print_fn(str(e))
        return 1
    except (ValueError, OSError) as e:
        print_fn(f"kimeru dataset: {e}")
        return 2
