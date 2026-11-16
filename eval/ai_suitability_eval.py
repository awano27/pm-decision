"""Offline, independently labeled evaluation of the five-level suitability gate.

The commands validate a dataset, export the exact assessment questions, or score
an imported answer file. None calls a model. Imported provenance is a declaration,
not evidence that this program ran a service. Results for Jev stay private.
"""
import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kimeru import ai_suitability  # noqa: E402
from kimeru.backends import validate_answers  # noqa: E402
from kimeru.events import JUDGE_HIDDEN, KINDS  # noqa: E402

DEFAULT_FIXTURES = ROOT / "eval" / "fixtures_ai_suitability.json"
SCHEMA_VERSION = 1
SCORING_VERSION = "1"
SCOPE = "ai_suitability_gate_only"
SPLITS = ("dev", "eval", "all")
ROUTES = ("human", "ai")
MODEL_REASONS = frozenset(ai_suitability.QUESTIONS["ai_suitability_reason"]["criteria"])
AI_REASONS = frozenset(("general_knowledge", "supplied_evidence"))
# These are evaluation-envelope fields, not normalized event-state fields.
# Only top-level keys are reserved: source context and text remain model input.
EVALUATION_STATE_FIELDS = frozenset((
    "expect", "rationale", "label_source", "human_reviewed", "split", "group", "category",
    "ai_suitability", "ai_suitability_reason", "answers", "responses", "requests",
    "expected_level", "expected_route", "expected_reasons", "dataset_id", "rubric",
    "schema_version", "dataset_sha256", "assessment_sha256",
))


class EvaluationError(ValueError):
    """A safe error code; never include fixture text or a model's error body."""


def _require(condition, code):
    if not condition:
        raise EvaluationError(code)


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise EvaluationError("INVALID_JSON_VALUE") from None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _nonfinite(_value):
    raise EvaluationError("NONFINITE_JSON_VALUE")


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"),
                          object_pairs_hook=_unique_object, parse_constant=_nonfinite)
    except EvaluationError:
        raise
    except (ValueError, OSError, UnicodeError):
        raise EvaluationError("INPUT_UNREADABLE_OR_INVALID_JSON") from None


def _validate_dataset(dataset):
    _require(isinstance(dataset, dict), "INVALID_DATASET")
    _require(type(dataset.get("schema_version")) is int
             and dataset["schema_version"] == SCHEMA_VERSION, "UNSUPPORTED_DATASET_SCHEMA")
    for field in ("dataset_id", "label_source", "rubric"):
        _require(_text(dataset.get(field)), "INVALID_DATASET_METADATA")
    _require(type(dataset.get("human_reviewed")) is bool, "INVALID_LABEL_PROVENANCE")
    cases = dataset.get("cases")
    _require(isinstance(cases, list), "INVALID_CASES")
    _require(bool(cases), "NO_INPUT")
    seen, groups = set(), {}
    for case in cases:
        _require(isinstance(case, dict), "INVALID_CASE")
        for field in ("id", "group", "category", "rationale"):
            _require(_text(case.get(field)), "INVALID_CASE_METADATA")
        _require(case["id"] not in seen, "DUPLICATE_CASE_ID")
        seen.add(case["id"])
        _require(case.get("split") in ("dev", "eval"), "INVALID_CASE_SPLIT")
        previous_split = groups.setdefault(case["group"], case["split"])
        _require(previous_split == case["split"], "GROUP_CROSSES_SPLITS")
        _require(type(case.get("input_truncated", False)) is bool, "INVALID_TRUNCATION_FLAG")
        state = case.get("state")
        _require(isinstance(state, dict) and bool(state), "INVALID_STATE")
        _require(state.get("kind") in KINDS, "INVALID_EVENT_KIND")
        _require(not set(state).intersection(JUDGE_HIDDEN), "STATE_CONTAINS_HIDDEN_FIELDS")
        _require(not set(state).intersection(EVALUATION_STATE_FIELDS), "STATE_CONTAINS_EVALUATION_METADATA")
        expected = case.get("expect")
        _require(isinstance(expected, dict), "INVALID_EXPECTATION")
        level, route, reasons = expected.get("level"), expected.get("route"), expected.get("reasons")
        _require(type(level) is int and 1 <= level <= 5, "INVALID_EXPECTED_LEVEL")
        _require(route in ROUTES, "INVALID_EXPECTED_ROUTE")
        _require(isinstance(reasons, list) and bool(reasons)
                 and all(_text(reason) and reason in MODEL_REASONS for reason in reasons),
                 "INVALID_EXPECTED_REASONS")
        _require(len(reasons) == len(set(reasons)), "DUPLICATE_EXPECTED_REASON")
        if route == "ai":
            _require(level >= 4 and set(reasons) <= AI_REASONS
                     and not case.get("input_truncated", False), "INCONSISTENT_EXPECTATION")
        elif level >= 4:
            _require(case.get("input_truncated", False)
                     or set(reasons) <= ai_suitability.HUMAN_REASONS, "INCONSISTENT_EXPECTATION")
    _canonical(dataset)
    return dataset


def load_dataset(path=DEFAULT_FIXTURES):
    """Load strict UTF-8 JSON and validate reference labels independently of answers."""
    return _validate_dataset(_read_json(path))


def dataset_sha256(dataset):
    """Canonical identity includes all labels, state, groups, splits and provenance."""
    return hashlib.sha256(_canonical(dataset)).hexdigest()


def _source_hash(path):
    return hashlib.sha256(Path(path).read_text(encoding="utf-8").replace("\r\n", "\n")
                          .encode("utf-8")).hexdigest()


def assessment_sha256():
    """Fingerprint the current questions and their production interpretation."""
    from kimeru import backends
    identity = {"assessment_source": _source_hash(ai_suitability.__file__),
                "answer_validation_source": _source_hash(backends.__file__),
                "questions": ai_suitability.QUESTIONS,
                "min_confidence": ai_suitability.MIN_CONFIDENCE,
                "human_reasons": sorted(ai_suitability.HUMAN_REASONS)}
    return hashlib.sha256(_canonical(identity)).hexdigest()


def _selected(dataset, split):
    _require(split in SPLITS, "INVALID_SPLIT")
    return [case for case in dataset["cases"] if split == "all" or case["split"] == split]


def request_bundle(dataset, split="eval"):
    """Export only supplied state and shipped questions, never reference labels."""
    _validate_dataset(dataset)
    return {"schema_version": SCHEMA_VERSION, "dataset_sha256": dataset_sha256(dataset),
            "assessment_sha256": assessment_sha256(), "split": split,
            "requests": [{"id": case["id"], "state": copy.deepcopy(case["state"]),
                          "questions": copy.deepcopy(ai_suitability.QUESTIONS)}
                         for case in _selected(dataset, split)]}


def _responses(dataset, envelope, split, cases):
    _require(isinstance(envelope, dict), "INVALID_ANSWER_ENVELOPE")
    _require(type(envelope.get("schema_version")) is int
             and envelope["schema_version"] == SCHEMA_VERSION, "UNSUPPORTED_ANSWER_SCHEMA")
    _require(envelope.get("dataset_sha256") == dataset_sha256(dataset)
             and envelope.get("assessment_sha256") == assessment_sha256()
             and envelope.get("split") == split, "PROVENANCE_MISMATCH")
    source = envelope.get("source")
    _require(isinstance(source, dict), "INVALID_SOURCE")
    _require(source.get("kind") in ("synthetic", "model")
             and source.get("backend") in ("fake", "jev") and _text(source.get("model")),
             "INVALID_SOURCE")
    _require(source["kind"] != "model" or source["backend"] == "jev", "INVALID_MODEL_SOURCE")
    responses = envelope.get("responses")
    _require(isinstance(responses, list), "INVALID_RESPONSES")
    ids = {case["id"] for case in cases}
    indexed = {}
    for response in responses:
        _require(isinstance(response, dict) and _text(response.get("id")), "INVALID_RESPONSE_ID")
        identifier = response["id"]
        _require(identifier in ids, "UNKNOWN_RESPONSE_ID")
        _require(identifier not in indexed, "DUPLICATE_RESPONSE_ID")
        indexed[identifier] = response
    identity = {key: source[key] for key in ("kind", "backend", "model")}
    _canonical(identity)
    return indexed, identity


class _RecordedJev:
    NAME = "Jev"

    def __init__(self, answers):
        self.answers = answers

    def ask(self, _state, _questions):
        return self.answers


def _prediction(case, response):
    if response is None:
        return {"status": "missing"}
    if set(response) == {"id", "error"} and response["error"] == "unavailable":
        return {"status": "unavailable"}
    if set(response) != {"id", "answers"}:
        return {"status": "invalid"}
    answers = response["answers"]
    try:
        if not isinstance(answers, dict) or set(answers) != set(ai_suitability.QUESTIONS):
            return {"status": "invalid"}
        validate_answers(ai_suitability.QUESTIONS, answers)
        record = {"outcome": "decide", "actions": []}
        ai_suitability.apply(record, case["state"], _RecordedJev(answers),
                             input_truncated=case.get("input_truncated", False))
        assessment = record["ai_suitability"]
    except (ValueError, TypeError, KeyError):
        return {"status": "invalid"}
    if assessment["status"] != "assessed":
        return {"status": "invalid"}
    return {"status": "valid", "level": assessment["level"], "route": assessment["route"],
            "reason": answers["ai_suitability_reason"]["choice"],
            "gate_reason": assessment["reason_code"]}


def _ratio(numerator, denominator, *, mean=False):
    return {"numerator": numerator, "denominator": denominator,
            "value" if mean else "rate": numerator / denominator if denominator else None}


def score(dataset, envelope, split="eval"):
    """Score imported responses against explicit labels; missing results stay visible."""
    _validate_dataset(dataset)
    cases = _selected(dataset, split)
    responses, source = _responses(dataset, envelope, split, cases)
    coverage = {"total": len(cases), "valid": 0, "missing": 0, "invalid": 0, "unavailable": 0}
    labeled = {"levels": {str(level): 0 for level in range(1, 6)},
               "routes": {route: 0 for route in ROUTES}}
    level_confusion = {str(level): {str(predicted): 0 for predicted in range(1, 6)}
                       for level in range(1, 6)}
    route_confusion = {route: {predicted: 0 for predicted in ROUTES} for route in ROUTES}
    exact = reason_matches = absolute_error = 0
    rows = []
    for case in cases:
        expected = case["expect"]
        labeled["levels"][str(expected["level"])] += 1
        labeled["routes"][expected["route"]] += 1
        predicted = _prediction(case, responses.get(case["id"]))
        coverage[predicted["status"]] += 1
        row = {"id": case["id"], "category": case["category"], "status": predicted["status"]}
        if predicted["status"] == "valid":
            level_confusion[str(expected["level"])][str(predicted["level"])] += 1
            route_confusion[expected["route"]][predicted["route"]] += 1
            level_correct = expected["level"] == predicted["level"]
            reason_correct = predicted["reason"] in expected["reasons"]
            exact += level_correct
            reason_matches += reason_correct
            absolute_error += abs(expected["level"] - predicted["level"])
            row.update(expected={"level": expected["level"], "route": expected["route"]},
                       predicted={key: predicted[key] for key in ("level", "route", "reason", "gate_reason")},
                       level_match=level_correct, reason_match=reason_correct,
                       route_match=expected["route"] == predicted["route"])
        rows.append(row)
    valid = coverage["valid"]
    human_denominator = sum(route_confusion["human"].values())
    ai_denominator = sum(route_confusion["ai"].values())
    metrics = {
        "level_exact_match": _ratio(exact, valid),
        "unsafe_ai_route": _ratio(route_confusion["human"]["ai"], human_denominator),
        "unnecessary_human_route": _ratio(route_confusion["ai"]["human"], ai_denominator),
        "route_accuracy": _ratio(sum(route_confusion[route][route] for route in ROUTES), valid),
        "reason_match": _ratio(reason_matches, valid),
        "level_mae": _ratio(absolute_error, valid, mean=True),
    }
    if not cases or not responses:
        status = "NO_INPUT"
    elif valid != len(cases):
        status = "PARTIAL"
    else:
        status = "PIPELINE_CHECK" if source["kind"] == "synthetic" else "IMPORTED_RESULTS"
    private = source["backend"] == "jev"
    return {"schema_version": SCHEMA_VERSION, "status": status, "scope": SCOPE,
            "dataset_id": dataset["dataset_id"], "dataset_sha256": dataset_sha256(dataset),
            "assessment_sha256": assessment_sha256(), "split": split,
            "scoring": {"method": SCOPE, "version": SCORING_VERSION,
                        "implementation_sha256": _source_hash(__file__)},
            "source": source, "source_provenance": "declared_unverified",
            "labels": {"source": dataset["label_source"], "human_reviewed": dataset["human_reviewed"],
                       "rubric": dataset["rubric"]},
            "private": private, "shareable": not private,
            "metric_denominators": "valid_responses_only; inspect coverage and labeled_counts before interpreting rates",
            "coverage": coverage, "labeled_counts": labeled, "metrics": metrics,
            "level_confusion": level_confusion, "route_confusion": route_confusion, "rows": rows}


def _output_path(path):
    target = Path(path)
    try:
        _require(not target.exists() and not target.is_symlink(), "OUTPUT_EXISTS")
        resolved = target.resolve()
        inside_current = resolved == ROOT or ROOT in resolved.parents
        inside_other = any((parent / ".git").exists() for parent in (resolved.parent, *resolved.parents))
        _require(not inside_current and not inside_other, "OUTPUT_IN_REPOSITORY")
        _require(resolved.parent.is_dir(), "OUTPUT_PARENT_MISSING")
    except (OSError, RuntimeError):
        raise EvaluationError("OUTPUT_REJECTED") from None
    return target


def _write_new(path, value):
    target = _output_path(path)
    content = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    try:
        with target.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
    except (OSError, UnicodeError):
        raise EvaluationError("OUTPUT_REJECTED") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "requests", "score"):
        command = sub.add_parser(name)
        command.add_argument("--fixtures", default=str(DEFAULT_FIXTURES))
        if name != "validate":
            command.add_argument("--split", choices=SPLITS, default="eval")
            command.add_argument("--out", required=True)
        if name == "score":
            command.add_argument("--answers", required=True)
    args = parser.parse_args(argv)
    try:
        dataset = load_dataset(args.fixtures)
        if args.command == "validate":
            print("VALID_DATASET: ラベルと構造を検証しました。人によるラベルの確認やモデル精度の測定を意味しません。")
            return 0
        _output_path(args.out)
        if args.command == "requests":
            result = request_bundle(dataset, args.split)
            if not result["requests"]:
                print("NO_INPUT: 選択したデータがありません。")
                return 1
            _write_new(args.out, result)
            print(f"REQUESTS_EXPORTED: {args.out}（モデルは呼んでいません）")
            return 0
        result = score(dataset, _read_json(args.answers), args.split)
        _write_new(args.out, result)
        print(f"{result['status']}: {args.out}" + ("（非公開。共有しないでください）" if result["private"] else ""))
        if result["status"] == "PIPELINE_CHECK":
            print("合成応答による採点処理の確認です。モデル精度の実測ではありません。")
        elif result["status"] == "IMPORTED_RESULTS":
            print("取り込んだ応答を採点しました。モデル実行の来歴は、このコマンドでは検証していません。")
        return 0 if result["status"] in ("PIPELINE_CHECK", "IMPORTED_RESULTS") else 1
    except EvaluationError as error:
        print(f"{error}: 処理を中止しました。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
