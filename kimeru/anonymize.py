"""An anonymization *candidate* of labeled events, for a person to review. Never a claim that the data is anonymous.

The source file (usually `<state folder>/fixtures_user.jsonl`, written by `kimeru review`) is only read. The output folder
holds three files:

  candidate.jsonl       the rows with detected identifiers replaced by stable placeholders (<PERSON_1>, <EMAIL_2>, ...)
  manifest.json         status "candidate" + human_review_required, hashes of the source, the candidate and the review list
  review_PRIVATE.txt    every replacement (original -> placeholder) and the spots that may still identify someone.
                        It holds the original values: keep it on this PC, never share it, delete it when done.

Detection is by patterns and by the names in the record's own author fields. Secrets and proper nouns written in free text
(a customer, a product, a colleague named only in the body) can remain. The status stays "candidate" until a person marks it
reviewed (`mark_reviewed`), and that mark records the hashes it was given for: if the candidate or the review list changes
afterwards, the review is void and the status is "candidate" again.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

from . import config, paths

CANDIDATE = "candidate.jsonl"
MANIFEST = "manifest.json"
REVIEW = "review_PRIVATE.txt"
FORMAT = "kimeru-anonymize-candidate/v1"

STATUS_REQUIRED = "candidate_human_review_required"
STATUS_REVIEWED = "reviewed_by_human"
STATUS_VOID = "review_void"
STATUS_NOT_ANONYMIZED = "not_anonymized"

WARNING = ("これは匿名化済みデータではありません（status: candidate, human_review_required）。"
           "自由記述の欄に、秘密情報・固有名詞（顧客・製品・同僚の名前など）が残る可能性があります。")

# Event fields kept as they are: the graphs route on them and they name no one.
STRUCTURAL = {"kind", "source", "severity", "condition", "type", "priority", "index", "mentions_me", "ts"}
# Fields whose whole value identifies someone or something: replaced as one placeholder.
WHOLE_FIELD = {
    "author": "PERSON", "created_by": "PERSON", "assigned_to": "PERSON", "owner": "PERSON", "from": "PERSON",
    "to": "PERSON", "participants": "PERSON", "attendees": "PERSON", "reviewer": "PERSON",
    "id": "EVENT_ID", "chat_id": "CHAT_ID", "area": "AREA", "resources": "RESOURCE", "origin": "ORG",
    "chat_title": "CHAT_TITLE",
}
PERSON_FIELDS = {k for k, v in WHOLE_FIELD.items() if v == "PERSON"}
# Payloads the judge never sees (events.JUDGE_HIDDEN): the evaluation does not need them, so they are not copied.
DROPPED = ("raw", "thread", "full")
ROW_KEYS = ("id", "event", "expect")

# Applied in this order. Structural tokens first (a name inside an e-mail address is part of the address), then the
# names known from the record, then the rest.
_L, _R = r"(?<![0-9A-Za-z_])", r"(?![0-9A-Za-z_])"   # word edges for ASCII tokens (\b fails next to Japanese text)
_SECRET_PATTERNS = [
    re.compile(r"(?i)(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|account[_-]?key|sharedaccesskey"
               r"|client[_-]?secret|パスワード|トークン)\s*[:=：]\s*([^\s\"'、。,;<>]+)"),
    re.compile(r"(?i)" + _L + r"bearer\s+([A-Za-z0-9._~+/-]{8,}=*)"),
    re.compile(_L + r"((?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,})"),
    re.compile(_L + r"(sk-[A-Za-z0-9_-]{16,})"),
    re.compile(_L + r"(AKIA[0-9A-Z]{16})" + _R),
    re.compile(_L + r"(xox[abprs]-[A-Za-z0-9-]{10,})"),
    re.compile(_L + r"(eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})"),
    re.compile(r"(?i)" + _L + r"sig=([A-Za-z0-9%+/=]{16,})"),
]
_URL_CHARS = r"[A-Za-z0-9\-._~:/?#\[\]@!$&*+,;=%]*[A-Za-z0-9\-_~/#=%]"   # ASCII only: Japanese text often follows a URL directly
_EARLY = [
    ("URL", re.compile(r"(?i)" + _L + r"(?:https?|ftp)://" + _URL_CHARS)),
    ("URL", re.compile(r"(?i)" + _L + r"www\." + _URL_CHARS)),
    ("EMAIL", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("GUID", re.compile(r"(?i)(?<![0-9a-f])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![0-9a-f])")),
    ("IP", re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")),
    ("IP", re.compile(r"(?i)(?<![0-9a-f:])(?:[0-9a-f]{1,4}:){3,7}[0-9a-f]{1,4}(?![0-9a-f:])")),
    ("SECRET", re.compile(r"(?<![\w-])(?=[A-Za-z0-9+/_=-]*\d)(?=[A-Za-z0-9+/_=-]*[A-Za-z])[A-Za-z0-9+/_=-]{32,}(?![\w-])")),
]
_LATE = [
    ("MENTION", re.compile(r"(?<![\w.])@([^\W_][\w.\-]*(?<![.\-]))")),   # "@" is kept: graphs read it as "has an owner"
    ("WORK_ITEM", re.compile(r"(?i)(?<![\w&])(?:AB)?#\d{2,}(?!\d)")),
    ("WORK_ITEM", re.compile(r"(?i)(?:work ?item|workitem|ワークアイテム|作業項目|チケット|バグ|bug|task|pbi|user story)\s*[#:：]?\s*(\d{3,})(?!\d)")),
    ("TICKET", re.compile(_L + r"(?!(?:SEV|ISO|UTF|TLS|HTTP|HTTPS|CVE|SHA|AES|RFC)-)[A-Z][A-Z0-9]{1,9}-\d{1,7}(?!\d)")),
    ("PHONE", re.compile(r"(?<![\w.+])(?:\+\d{1,3}[-\s.]?)?(?:\(\d{1,4}\)[-\s.]?|\d{2,4}[-\s.])\d{2,4}[-\s.]\d{3,4}(?![\d.])")),
    ("NUMBER", re.compile(r"(?<!\d)\d{7,}(?!\d)")),
]
_RISKS = [
    ("honorific", re.compile(r"[一-龥々ァ-ヴーA-Za-z]{1,6}(?:さん|(?<![仕同模多異各])様|氏|殿|くん|君|部長|課長|社長|先生)")),
    ("organization", re.compile(r"(?:株式会社|有限会社|合同会社|\(株\)|（株）|㈱)[^\s、。,，「」()（）]{0,12}"
                                r"|[^\s、。,，「」()（）]{1,12}(?:株式会社|有限会社|合同会社)|[A-Za-z]+\s(?:Inc|Corp|LLC|Ltd)" + _R + r"\.?")),
    ("capitalized_word", re.compile(r"(?<![A-Za-z<_])[A-Z][a-z]{2,}(?:\s[A-Z][a-z]{2,})*")),
    ("katakana_name", re.compile(r"[ァ-ヴー]{2,}(?:・[ァ-ヴー]{2,})+")),
]
_COMMON_CAPITALIZED = {
    "The", "This", "That", "When", "What", "Please", "Thanks", "Error", "Errors", "Warning", "Alert", "Resolved", "Fired",
    "Sev", "Severity", "Failed", "Failure", "Test", "Tests", "Availability", "Service", "Server", "Request", "Requests",
    "Response", "High", "Low", "Medium", "Critical", "Task", "Bug", "Feature", "User", "Users", "Story", "Epic", "Done",
    "New", "Active", "Closed", "None", "True", "False", "Login", "Sign", "Page", "Order", "Orders", "Customer", "Customers",
}
_NAME_STOP = {"team", "admin", "bot", "the", "and", "for", "with", "pm", "qa", "dev"}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def sha256_file(path):
    return sha256_bytes(Path(path).read_bytes())


def _key_path():
    return config.state_dir() / "anonymize.key"


def _local_key():
    """A random key kept in the state folder (never in the output): row ids are keyed hashes of the original ids, so the
    split stays the same across runs on this PC, and an id cannot be reversed by trying every work-item number."""
    p = _key_path()
    if p.exists():
        k = p.read_bytes()
        if len(k) >= 32:
            return k
    p.parent.mkdir(parents=True, exist_ok=True)
    k = secrets.token_bytes(32)
    p.write_bytes(k)
    return k


def _repo_root():
    here = paths.home()
    return here if (here / "pyproject.toml").exists() or (here / ".git").exists() else None


def inside_repository(path):
    root = _repo_root()
    if root is None:
        return False
    try:
        Path(path).resolve().relative_to(root)
        return True
    except ValueError:
        return False


class Replacer:
    """Same original -> same placeholder, across the whole dataset."""

    def __init__(self):
        self.maps = {}        # category -> {original: placeholder}
        self.people = {}      # lowercased name or name part -> placeholder
        self.records = []     # (row id, field path, category, original, placeholder)

    def placeholder(self, cat, original):
        m = self.maps.setdefault(cat, {})
        if original not in m:
            m[original] = f"<{cat}_{len(m) + 1}>"
        return m[original]

    def person(self, name):
        key = " ".join(str(name).split()).casefold()
        if key not in self.people:
            self.people[key] = self.placeholder("PERSON", " ".join(str(name).split()))
        return self.people[key]

    def note(self, rid, path, cat, original, ph):
        self.records.append((rid, path, cat, original, ph))


def _name_parts(name):
    full = " ".join(str(name).split())
    parts = [p for p in re.split(r"[\s,、・()（）\[\]/]+", full) if p]
    out = []
    for p in parts:
        latin = re.fullmatch(r"[A-Za-z.'-]+", p) is not None
        if p.casefold() in _NAME_STOP or (latin and len(p) < 3) or (not latin and len(p) < 2):
            continue
        out.append(p)
    return full, out


def _collect_people(rows):
    """Names written in the author-like fields of every row (and of the earlier messages of a thread)."""
    names = []

    def add(v):
        if isinstance(v, str) and v.strip():
            names.append(v.strip())
        elif isinstance(v, list):
            for x in v:
                add(x.get("name") or x.get("displayName") if isinstance(x, dict) else x)
        elif isinstance(v, dict):
            add(v.get("displayName") or v.get("name"))

    for r in rows:
        ev = r.get("event") if isinstance(r, dict) else None
        if not isinstance(ev, dict):
            continue
        for f in PERSON_FIELDS:
            add(ev.get(f))
        thread = ev.get("thread")
        for m in thread if isinstance(thread, list) else []:
            if isinstance(m, dict):
                add(m.get("author"))
    return names


class Anonymizer:
    def __init__(self, people=(), terms=()):
        self.r = Replacer()
        self.name_patterns = []    # (compiled, placeholder, original)
        for name in people:
            full, parts = _name_parts(name)
            ph = self.r.person(full)
            for p in [full] + parts:
                self.r.people.setdefault(p.casefold(), ph)
        for t in terms:
            t = " ".join(str(t).split())
            if t:
                self.r.placeholder("TERM", t)
        entries = [(k, v) for k, v in self.r.people.items()] + [(k.casefold(), v) for k, v in self.r.maps.get("TERM", {}).items()]
        for low, ph in sorted(entries, key=lambda e: -len(e[0])):
            esc = re.escape(low).replace(r"\ ", r"\s+")
            pat = (r"(?<![A-Za-z0-9])" + esc + r"(?![A-Za-z0-9])") if re.fullmatch(r"[a-z0-9 .'-]+", low) else esc
            self.name_patterns.append((re.compile(pat, re.IGNORECASE), ph))

    def text(self, s, rid, path):
        def sub(cat, rx, s, group=0):
            def repl(m):
                orig = m.group(group)
                ph = self.r.placeholder(cat, orig)
                self.r.note(rid, path, cat, orig, ph)
                return m.group(0).replace(orig, ph) if group else ph
            return rx.sub(repl, s)

        for rx in _SECRET_PATTERNS:
            s = sub("SECRET", rx, s, 1)
        for cat, rx in _EARLY:
            s = sub(cat, rx, s)
        for rx, ph in self.name_patterns:
            def repl(m, ph=ph):
                self.r.note(rid, path, ph[1:].rsplit("_", 1)[0], m.group(0), ph)
                return ph
            s = rx.sub(repl, s)
        for cat, rx in _LATE:
            s = sub(cat, rx, s, 1 if rx.groups else 0)
        return s

    def whole(self, value, cat, rid, path):
        if isinstance(value, list):
            return [self.whole(v, cat, rid, f"{path}[{i}]") for i, v in enumerate(value)]
        if isinstance(value, dict):
            return {k: self.whole(v, cat, rid, f"{path}.{k}") for k, v in value.items()}
        if value is None or isinstance(value, bool) or value == "":
            return value
        orig = str(value)
        if cat in ("PERSON", "CHAT_TITLE") and " ".join(orig.split()).casefold() in self.r.people:
            ph = self.r.people[" ".join(orig.split()).casefold()]
        else:
            ph = self.r.placeholder(cat, orig)
        self.r.note(rid, path, ph[1:].rsplit("_", 1)[0], orig, ph)
        return ph

    def free(self, value, rid, path, free_fields):
        if isinstance(value, str):
            if value.strip():
                free_fields.append((rid, path))
            return self.text(value, rid, path)
        if isinstance(value, list):
            return [self.free(v, rid, f"{path}[{i}]", free_fields) for i, v in enumerate(value)]
        if isinstance(value, dict):
            return {k: self.free(v, rid, f"{path}.{k}", free_fields) for k, v in value.items()}
        return value

    def event(self, ev, rid, free_fields, dropped):
        out = {}
        for k, v in ev.items():
            path = f"event.{k}"
            if k in DROPPED:
                dropped.append((rid, path))
            elif k in STRUCTURAL:
                out[k] = v
            elif k in WHOLE_FIELD:
                out[k] = self.whole(v, WHOLE_FIELD[k], rid, path)
            else:
                out[k] = self.free(v, rid, path, free_fields)
        return out


def anon_id(original_id, key):
    return "anon-" + hmac.new(key, str(original_id).encode("utf-8"), hashlib.sha256).hexdigest()[:16]


def _strings(v):
    if isinstance(v, str):
        yield v
    elif isinstance(v, list):
        for x in v:
            yield from _strings(x)
    elif isinstance(v, dict):
        for x in v.values():
            yield from _strings(x)


def _risk_spots(rows):
    """[(row id, kind, snippet)]: what a pattern cannot decide for a person to look at."""
    out = []
    for r in rows:
        seen = set()
        for s in _strings(r.get("event", {})):
            for kind, rx in _RISKS:
                for m in rx.finditer(s):
                    hit = m.group(0)
                    if kind == "capitalized_word" and all(w in _COMMON_CAPITALIZED for w in hit.split()):
                        continue
                    if (kind, hit) not in seen:
                        seen.add((kind, hit))
                        out.append((r["id"], kind, hit))
    return out


def _route_changes(pairs, graphs, playbooks):
    """Rows whose rule/stub route differs once anonymized: a replacement touched something a graph reads."""
    if not graphs:
        return []
    from . import graph as graph_mod
    from .backends import StubBackend
    changed = []
    for rid, before, after in pairs:
        g = graphs.get(before.get("kind"))
        if g is None:
            continue
        try:
            a = graph_mod.run(g, before, StubBackend(), playbooks=playbooks)
            b = graph_mod.run(g, after, StubBackend(), playbooks=playbooks)
        except Exception as e:   # noqa: BLE001 - reported to the person, never hidden
            changed.append((rid, f"経路を確かめられませんでした: {type(e).__name__}"))
            continue
        pa = [(s.get("node"), s.get("edge")) for s in a.get("path", [])] + [a.get("node")]
        pb = [(s.get("node"), s.get("edge")) for s in b.get("path", [])] + [b.get("node")]
        if pa != pb:
            changed.append((rid, f"stub での経路が変わりました: {a.get('node')} -> {b.get('node')}"))
    return changed


def _read_rows(src_bytes):
    rows, bad = [], []
    text = src_bytes.decode("utf-8-sig")
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except ValueError:
            bad.append(n)
            continue
        if not isinstance(r, dict):
            bad.append(n)
            continue
        rows.append((n, r))
    return rows, bad


def make_candidate(src, dest, terms=(), graphs=None, playbooks=None, now=None):
    """Write the candidate, the manifest and the review list into `dest` (a new or empty folder). Returns the manifest."""
    src, dest = Path(src), Path(dest)
    if not src.is_file():
        raise FileNotFoundError(f"NO_INPUT: {src} がありません")
    if dest.exists() and any(dest.iterdir()):
        raise ValueError(f"{dest} は空ではありません。新しいフォルダを指定してください（上書きしません）")
    if inside_repository(dest):
        raise ValueError(f"{dest} はリポジトリの中です。評価データは状態フォルダなど、リポジトリの外に置いてください")
    src_bytes = src.read_bytes()
    src_hash = sha256_bytes(src_bytes)
    rows, bad_lines = _read_rows(src_bytes)
    if not rows:
        raise ValueError(f"NO_INPUT: {src} に行がありません")
    key = _local_key()
    anon = Anonymizer(_collect_people([r for _, r in rows]), terms)
    out_rows, free_fields, dropped, extra_keys, pairs = [], [], [], [], []
    user_ids = 0
    for n, r in rows:
        oid = r.get("id")
        rid = anon_id(oid, key) if isinstance(oid, str) and oid else f"line-{n}"
        user_ids += isinstance(oid, str) and oid.startswith("user-")
        new = {"id": rid}
        if isinstance(r.get("event"), dict):
            new["event"] = anon.event(r["event"], rid, free_fields, dropped)
            pairs.append((rid, {k: v for k, v in r["event"].items() if k not in DROPPED}, new["event"]))
        elif "event" in r:
            new["event"] = r["event"] if not isinstance(r["event"], str) else anon.text(r["event"], rid, "event")
        if "expect" in r:
            new["expect"] = r["expect"]
        for k in r:
            if k not in ROW_KEYS:
                extra_keys.append((rid, k))
        out_rows.append(new)
    originals = {orig for _, _, cat, orig, _ in anon.r.records if len(orig) >= (4 if cat in ("EVENT_ID", "CHAT_ID") else 3)}
    leftovers = []
    for row in out_rows:
        blob = json.dumps(row.get("event", {}), ensure_ascii=False)
        for o in sorted(originals):
            if o in blob or json.dumps(o, ensure_ascii=False)[1:-1] in blob:
                leftovers.append((row["id"], o))
    risks = _risk_spots(out_rows)
    routes = _route_changes(pairs, graphs, playbooks)
    dest.mkdir(parents=True, exist_ok=True)
    cand_bytes = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in out_rows).encode("utf-8")
    (dest / CANDIDATE).write_bytes(cand_bytes)
    counts = {}
    for _, _, cat, _, _ in anon.r.records:
        counts[cat] = counts.get(cat, 0) + 1
    review_text = _review_text(src.name, sha256_bytes(cand_bytes), anon.r.records, risks, leftovers, routes, free_fields,
                               dropped, extra_keys, bad_lines)
    (dest / REVIEW).write_text(review_text, encoding="utf-8")
    manifest = {
        "format": FORMAT,
        "status": "candidate",
        "human_review_required": True,
        "warning": WARNING,
        "created_at": now or _now(),
        "source": {"name": src.name, "sha256": src_hash, "rows": len(rows), "unreadable_lines": len(bad_lines),
                   "origin": "user_reviews" if rows and user_ids == len(rows) else "other"},
        "candidate": {"file": CANDIDATE, "sha256": sha256_bytes(cand_bytes), "rows": len(out_rows)},
        "review_list": {"file": REVIEW, "sha256": sha256_file(dest / REVIEW), "private": True},
        "replacements": dict(sorted(counts.items())),
        "residual_risks": {"spots": len(risks), "leftover_originals": len(leftovers), "route_changes": len(routes),
                           "free_text_fields": len(free_fields)},
        "human_review": None,
    }
    _write_manifest(dest, manifest)
    if sha256_file(src) != src_hash:   # the source must be exactly as it was
        raise RuntimeError("元のファイルが処理中に変わりました")
    return manifest


def _write_manifest(dest, manifest):
    p = Path(dest) / MANIFEST
    tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def _review_text(src_name, cand_hash, records, risks, leftovers, routes, free_fields, dropped, extra_keys, bad_lines):
    L = ["# kimeru 匿名化候補のレビュー表（ローカル専用・共有禁止: 元の値を含みます。確認が済んだら削除してください）",
         "", "状態: candidate / human_review_required", WARNING,
         "置換は、パターンと、記録の作成者欄にある名前だけで行っています。本文だけに出てくる名前・社名・製品名・秘密は検出できないことがあります。",
         f"元のファイル: {src_name}（変更していません）", f"候補 candidate.jsonl の sha256: {cand_hash}", "",
         f"== 置換の一覧（{len(records)} 件）==", "行 / 欄 / 種類 / 元の値 -> 置き換え"]
    for rid, path, cat, orig, ph in records:
        L.append(f"[{rid}] {path} {cat}: {json.dumps(orig, ensure_ascii=False)} -> {ph}")
    L += ["", f"== 残るリスク（人が読んで判断する箇所）=="]
    if leftovers:
        L.append(f"-- 置換したはずの値が候補に残っています（{len(leftovers)} 件。必ず直してください）")
        L += [f"[{rid}] {json.dumps(o, ensure_ascii=False)}" for rid, o in leftovers]
    if routes:
        L.append(f"-- 置換で判断の経路が変わった行（{len(routes)} 件。評価結果が元のデータと変わります）")
        L += [f"[{rid}] {msg}" for rid, msg in routes]
    L.append(f"-- 固有名詞・名前らしい語（{len(risks)} 件）")
    L += [f"[{rid}] {kind}: {json.dumps(hit, ensure_ascii=False)}" for rid, kind, hit in risks]
    L.append(f"-- パターンでしか確かめていない自由記述の欄（{len(free_fields)} 件。秘密・固有名詞が残る可能性）")
    L += [f"[{rid}] {path}" for rid, path in free_fields]
    if dropped:
        L.append(f"-- 判断に使わないので写さなかった欄（{len(dropped)} 件）")
        L += [f"[{rid}] {path}" for rid, path in dropped]
    if extra_keys:
        L.append(f"-- 評価に使わないので写さなかった行の項目（{len(extra_keys)} 件）")
        L += [f"[{rid}] {k}" for rid, k in extra_keys]
    if bad_lines:
        L.append(f"-- 読めなかった行（写していません）: {', '.join(map(str, bad_lines))}")
    L += ["", "== 確認のしかた ==",
          "1. 上の一覧と candidate.jsonl を読み、残っている名前・社名・秘密を candidate.jsonl で直接直す",
          "   （直したら、この一覧は古くなります。直したあとも確認済みにできますが、記録されるのは確認した時点の内容のハッシュです）",
          "2. 確認できたら: kimeru dataset mark-reviewed <このフォルダ> --i-reviewed",
          "   その後に candidate.jsonl かこの一覧が変わると、確認は無効になり、状態は candidate に戻ります。",
          "3. この一覧（review_PRIVATE.txt）は元の値を含むので、共有せず、確認が済んだら削除してください。", ""]
    return "\n".join(L)


def read_manifest(dest):
    p = Path(dest) / MANIFEST
    if not p.is_file():
        return None
    try:
        m = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return None
    return m if isinstance(m, dict) and m.get("format") == FORMAT else None


def status(dest):
    """(status, reasons). Recomputed from the files every time: a review counts only for the exact bytes it was given for."""
    m = read_manifest(dest)
    if m is None:
        return STATUS_NOT_ANONYMIZED, ["匿名化候補のマニフェストがありません"]
    cand = Path(dest) / CANDIDATE
    if not cand.is_file():
        return STATUS_VOID, ["candidate.jsonl がありません"]
    review = m.get("human_review")
    if not isinstance(review, dict):
        return STATUS_REQUIRED, ["人による確認がまだです"]
    reasons = []
    if review.get("candidate_sha256") != sha256_file(cand):
        reasons.append("確認したあとに candidate.jsonl が変わりました")
    rl = Path(dest) / REVIEW
    if review.get("review_list_sha256") and rl.is_file() and review["review_list_sha256"] != sha256_file(rl):
        reasons.append("確認したあとにレビュー表が変わりました")
    if reasons:
        return STATUS_VOID, reasons + ["確認は無効です。もう一度確認してください（status: candidate, human_review_required）"]
    return STATUS_REVIEWED, [f"{review.get('at')} に確認済み（確認した時点の内容と一致）"]


def mark_reviewed(dest, confirmed, by="", now=None):
    """Record that a person read the review list and the candidate. `confirmed` must be True (an explicit act)."""
    if confirmed is not True:
        raise ValueError("確認したことを明示してください（--i-reviewed）")
    m = read_manifest(dest)
    if m is None:
        raise ValueError(f"{dest} は匿名化候補のフォルダではありません")
    cand = Path(dest) / CANDIDATE
    if not cand.is_file():
        raise ValueError("candidate.jsonl がありません")
    rl = Path(dest) / REVIEW
    m["human_review"] = {"by": str(by or "")[:40], "at": now or _now(), "candidate_sha256": sha256_file(cand),
                         "review_list_sha256": sha256_file(rl) if rl.is_file() else None}
    m["status"] = STATUS_REVIEWED
    m["human_review_required"] = False
    _write_manifest(dest, m)
    return m
