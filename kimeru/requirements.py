"""Local, source-linked requirements drafts built from an existing human-queue case."""
import argparse
import hashlib
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from . import fsutil, notify

FIELDS = ("must_have", "optional", "out_of_scope", "acceptance_criteria", "open_questions")
ARTIFACT_FIELDS = {"schema_version", "case", "answers", "unknown_categories", "generated_open_questions",
                   "source_material", "sources", "approval_state"}
UNANSWERED = {
    "must_have": "What is required for the first usable result?",
    "optional": "Which improvements are optional?",
    "out_of_scope": "What should this work explicitly exclude?",
    "acceptance_criteria": "How will completion be verified?",
    "open_questions": "Are there other unresolved questions?",
}


def _positive(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("case number must be a positive integer") from None
    if number < 1:
        raise argparse.ArgumentTypeError("case number must be a positive integer")
    return number


def _answers(path):
    if path is None:
        return {key: [] for key in FIELDS}
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read answers JSON ({type(exc).__name__})") from None
    _validate_answers(raw)
    return {key: list(raw[key]) for key in FIELDS}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _paths(out, number):
    folder = Path(out) / "requirements"
    return folder, folder / f"{number}.json", folder / f"{number}.md", folder / "approvals.json"


def _generation_path(folder, number):
    return folder / f"{number}.generation.json"


def _generation_marker(path, number):
    """Read and validate the optional publication marker; old artifact pairs have none."""
    if not path.exists():
        return None
    try:
        marker = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        raise ValueError("requirements generation marker is unreadable or has duplicate keys") from None
    if (not isinstance(marker, dict) or set(marker) != {"schema_version", "case", "generation", "phase"}
            or type(marker.get("schema_version")) is not int or marker["schema_version"] != 1
            or type(marker.get("case")) is not int or marker["case"] != number
            or not isinstance(marker.get("generation"), str) or len(marker["generation"]) != 32
            or any(char not in "0123456789abcdef" for char in marker["generation"])
            or marker.get("phase") not in ("pending", "complete")):
        raise ValueError("requirements generation marker has an invalid schema or case number")
    return marker


def _require_complete_generation(path, number):
    marker = _generation_marker(path, number)
    if marker is not None and marker["phase"] != "complete":
        raise LookupError(f"case {number} の要件成果物は未完了の世代です。再生成するには requirements build {number} --force を実行してください")


def _write_generation_marker(path, number, generation, phase):
    """Durably replace the small marker without ever removing the current marker first."""
    payload = json.dumps({"schema_version": 1, "case": number, "generation": generation, "phase": phase},
                         ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_name(f".{path.name}.{generation}.{phase}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        fsutil._replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _write_staged(path, text):
    fsutil.write_atomic(path, text)


def _publish_staged(staged_path, destination):
    # Keep the established .bak behavior while atomically publishing from a fully staged file.
    fsutil.write_atomic(destination, staged_path.read_text(encoding="utf-8"))


def _case(out, number):
    # approvals.json is atomically replaced; a read sees either the old or new full snapshot.
    items = notify.Approvals(out).data.get("items", {})
    item = items.get(str(number)) if isinstance(items, dict) else None
    if not isinstance(item, dict) or not isinstance(item.get("record"), dict):
        return None
    return json.loads(json.dumps(item, ensure_ascii=False))


@contextmanager
def _locked(out):
    path = Path(out) / "requirements.lock"
    for attempt in range(100):
        with fsutil.exclusive(path) as locked:
            if locked:
                yield True
                return
        time.sleep(0.05)
    yield False


def _markdown(document):
    labels = {
        "must_have": "Must have", "optional": "Optional", "out_of_scope": "Out of scope",
        "acceptance_criteria": "Acceptance criteria", "open_questions": "Open questions",
    }
    lines = [f"# Requirements draft for case {document['case']}", "", "Status: draft; this artifact has no external write authority.", ""]
    answers = document["answers"]
    for key in FIELDS:
        lines.extend([f"## {labels[key]}", ""])
        values = answers[key]
        if key == "open_questions":
            values = values + document["generated_open_questions"]
        if values:
            lines.extend(f"- {value}" for value in values)
        else:
            lines.append("- Unknown")
        lines.append("")
    lines.extend(["## Source material", "", "```json",
                  json.dumps(document["source_material"], ensure_ascii=False, indent=2), "```", "",
                  "## Source references", ""])
    for source in document["sources"]:
        lines.append("- " + json.dumps(source, ensure_ascii=False, sort_keys=True))
    lines.extend(["", "The requirements draft approval recorded by this tool is local metadata only. It does not approve the case, change Teams state, or authorize an external write.", ""])
    return "\n".join(lines)


def _manifest(path):
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("requirements approval manifest is unreadable") from None
    if not isinstance(data, dict):
        raise ValueError("requirements approval manifest has an invalid shape")
    return data


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_artifacts(number, json_path, md_path):
    try:
        document = json.loads(json_path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
        markdown = md_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"requirements artifact is unreadable ({type(exc).__name__})") from None
    if not isinstance(document, dict) or set(document) != ARTIFACT_FIELDS:
        raise ValueError("requirements JSON has an invalid top-level schema")
    if (document.get("schema_version") != 1 or isinstance(document.get("schema_version"), bool)
            or document.get("case") != number or isinstance(document.get("case"), bool)):
        raise ValueError("requirements JSON schema version or case number does not match")
    answers = document.get("answers")
    if not isinstance(answers, dict) or set(answers) != set(FIELDS):
        raise ValueError("requirements JSON answers have an invalid schema")
    _validate_answers(answers)
    unknown = document.get("unknown_categories")
    if (not isinstance(unknown, list) or any(not isinstance(field, str) or field not in FIELDS for field in unknown)
            or len(unknown) != len(set(unknown))):
        raise ValueError("requirements JSON unknown_categories has an invalid schema")
    if (not isinstance(document.get("generated_open_questions"), list)
            or any(not isinstance(value, str) or not value.strip() for value in document["generated_open_questions"])):
        raise ValueError("requirements JSON generated_open_questions has an invalid schema")
    if not isinstance(document.get("source_material"), dict):
        raise ValueError("requirements JSON source_material must be an object")
    sources = document.get("sources")
    if (not isinstance(sources, list) or not sources or any(
            not isinstance(source, dict) or source.get("case") != number
            or isinstance(source.get("revision"), bool) or not isinstance(source.get("revision"), int)
            or source.get("revision") < 1 or not isinstance(source.get("revision_ref"), str)
            or not isinstance(source.get("read_refs"), list) or not source.get("read_refs")
            or any(not isinstance(ref, str) or not ref for ref in source.get("read_refs", []))
            for source in sources)):
        raise ValueError("requirements JSON sources have an invalid schema or case reference")
    if document.get("approval_state") != "draft_only" or not markdown.strip():
        raise ValueError("requirements artifact status or Markdown is invalid")
    return document


def _validate_answers(answers):
    if not isinstance(answers, dict) or set(answers) != set(FIELDS):
        raise ValueError("answers must have exactly these keys: " + ", ".join(FIELDS))
    for key in FIELDS:
        if not isinstance(answers[key], list) or any(not isinstance(v, str) or not v.strip() for v in answers[key]):
            raise ValueError(f"answers.{key} must be a list of non-empty strings")
        try:
            for value in answers[key]:
                value.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError(f"answers.{key} contains text that is not valid UTF-8") from None


def dispatch(argv, out, print_fn=print):
    """Run requirements commands; return 0 success, 1 missing/operational, 2 usage/schema errors."""
    parser = argparse.ArgumentParser(prog="requirements")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("case", type=_positive)
    build.add_argument("--answers")
    build.add_argument("--force", action="store_true")
    approve = sub.add_parser("approve")
    approve.add_argument("case", type=_positive)
    status = sub.add_parser("status")
    status.add_argument("case", type=_positive)
    try:
        args = parser.parse_args(list(argv))
    except SystemExit as exc:
        return int(exc.code)

    folder, json_path, md_path, manifest_path = _paths(out, args.case)
    generation_path = _generation_path(folder, args.case)
    try:
        if args.command == "build":
            answers = _answers(args.answers)  # validate before touching any outputs
            item = _case(out, args.case)
            if item is None:
                print_fn(f"失敗: case {args.case} はありません")
                return 1
            with _locked(out) as locked:
                if not locked:
                    print_fn("失敗: requirements 成果物が使用中です。再試行してください")
                    return 1
                item = _case(out, args.case)
                if item is None:
                    print_fn(f"失敗: case {args.case} はありません")
                    return 1
                if not args.force and generation_path.exists():
                    marker = _generation_marker(generation_path, args.case)
                    if marker is not None and marker["phase"] == "pending":
                        print_fn(f"失敗: case {args.case} の要件成果物の公開が未完了です。修復には requirements build {args.case} --force を実行してください")
                        return 1
                if not args.force and (json_path.exists() or md_path.exists()):
                    print_fn("失敗: 既存の成果物があります。上書きするには --force を指定してください")
                    return 1
                doc = _artifact_from_item(out, args.case, item, answers)
                rendered = _markdown(doc)
                folder.mkdir(parents=True, exist_ok=True)
                generation = uuid4().hex
                staged_json = folder / f".{args.case}.{generation}.json.stage"
                staged_md = folder / f".{args.case}.{generation}.md.stage"
                try:
                    _write_staged(staged_json, json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
                    _write_staged(staged_md, rendered)
                    _write_generation_marker(generation_path, args.case, generation, "pending")
                    _publish_staged(staged_json, json_path)
                    _publish_staged(staged_md, md_path)
                    _write_generation_marker(generation_path, args.case, generation, "complete")
                finally:
                    for staged in (staged_json, staged_md):
                        try:
                            staged.unlink()
                        except FileNotFoundError:
                            pass
            print_fn(f"要件ドラフトを保存しました: {md_path} と {json_path}")
            return 0
        if _case(out, args.case) is None:
            print_fn(f"失敗: case {args.case} はありません")
            return 1
        with _locked(out) as locked:
            if not locked:
                print_fn("失敗: requirements 成果物が使用中です。再試行してください")
                return 1
            _require_complete_generation(generation_path, args.case)
            if not json_path.is_file() or not md_path.is_file():
                print_fn(f"失敗: case {args.case} の要件成果物がありません")
                return 1
            if _case(out, args.case) is None:
                print_fn(f"失敗: case {args.case} はありません")
                return 1
            _validate_artifacts(args.case, json_path, md_path)
            manifest = _manifest(manifest_path)
            if args.command == "approve":
                entry = {"sha256": {json_path.name: _digest(json_path), md_path.name: _digest(md_path)}}
                manifest[str(args.case)] = entry
                folder.mkdir(parents=True, exist_ok=True)
                fsutil.write_atomic(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
                print_fn(f"ローカルの要件成果物を承認しました: case {args.case}（ケース承認・外部書き込み権限は変更しません）")
                return 0
            entry = manifest.get(str(args.case))
            current = {json_path.name: _digest(json_path), md_path.name: _digest(md_path)}
            approved = isinstance(entry, dict) and entry.get("sha256") == current
            print_fn(f"case {args.case}: {'approved (local hashes match)' if approved else 'unapproved (missing approval or content changed)'}")
            return 0
    except ValueError as exc:
        print_fn(f"失敗: {exc}")
        return 2
    except LookupError as exc:
        print_fn(f"失敗: {exc}")
        return 1
    except (OSError, RuntimeError) as exc:
        print_fn(f"失敗: {type(exc).__name__}: {exc}")
        return 1


def _artifact_from_item(out, number, item, answers):
    record = item["record"]
    material_event = record.get("material_event")
    if isinstance(material_event, dict) and material_event:
        material = dict(material_event)
        read_refs = [f'approval items["{number}"].record.material_event.{key}' for key in material_event]
    else:
        event = record.get("event")
        material = dict(event) if isinstance(event, dict) else {}
        read_refs = [f'approval items["{number}"].record.event.{key}' for key in material]
    summary = record.get("summary")
    if isinstance(summary, str) and summary:
        material["summary"] = summary
        read_refs.append(f'approval items["{number}"].record.summary')
    full_available = False
    try:
        from . import fulltext
        full = fulltext.load(out, item.get("key")) if item.get("key") else None
    except (OSError, ValueError, TypeError):
        full = None
    if isinstance(full, dict):
        full_available = True
        if isinstance(full.get("material"), dict):
            material.update(full["material"])
            read_refs.extend(f'full_text.json[{item.get("key")}].material.{key}' for key in full["material"])
        if isinstance(full.get("text"), str) and full["text"]:
            material["text"] = full["text"]
            read_refs.append(f'full_text.json[{item.get("key")}].text')
        if isinstance(full.get("thread"), list) and full["thread"]:
            material["thread"] = full["thread"]
            read_refs.append(f'full_text.json[{item.get("key")}].thread')
    unknown = [field for field in FIELDS if not answers[field]]
    generated_questions = [UNANSWERED[field] for field in unknown if UNANSWERED[field] not in answers["open_questions"]]
    return {
        "schema_version": 1, "case": number, "answers": answers, "unknown_categories": unknown,
        "generated_open_questions": generated_questions,
        "source_material": material,
        "sources": [{"case": number, "case_file": "approvals.json", "revision": item.get("revision", record.get("revision", 1)),
                     "revision_ref": (f'approval items["{number}"].revision' if item.get("revision") is not None else
                                      f'approval items["{number}"].record.revision' if record.get("revision") is not None else "default revision 1"),
                     "event_kind": record.get("event_kind"), "event_id": record.get("event_id"), "graph": record.get("graph"),
                     "read_refs": read_refs,
                     "full_text_included": full_available,
                     "full_text_ref": "full_text.json (copied only during this explicit build)" if full_available else None}],
        "approval_state": "draft_only",
    }
