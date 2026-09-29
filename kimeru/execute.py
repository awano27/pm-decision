"""Carry out an approved action for real: ADO comments only, and only for the kinds the PM switched on.

Default: nothing is executed (approving records a plan, as before). `config set execute ado.comment` turns on the one kind that is
implemented here. The rules:

  - a switched-on kind is never executed by itself: even a decision the graph made automatically waits for an approval;
  - what is written is the text the approval post showed, word for word (plus one signature line, which can be switched off);
  - each action has an idempotency key and a state saved BEFORE the call ("running") and after it ("done" with the external id,
    or "failed"); a "running" state left by a crash is never executed again on its own: the PM is told the result is unknown;
  - a failure never cancels the approval; running it again is the PM's command (`再実行 N`);
  - the result goes back to the self chat, and every attempt is written to executions.jsonl;
  - teams.reply / teams.post are never sent: approving one returns a post with the text alone, ready to copy.

Authentication is the PM's own `az` sign-in, the token in memory only; organization and project come from the settings.
Tokens, tenant ids and URL queries never reach a record or a message.
"""
import hashlib
import json
import re
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from . import config, pull

SUPPORTED = ("ado.comment",)
SIGNATURE = "\n\n（kimeru が下書きし、本人が承認した文面です）"
SEND_READY = ("teams.reply", "teams.post")
FIELD = {"teams.reply": "text", "teams.post": "text", "ado.comment": "text"}


def requested():
    return [t.strip() for t in config.value("execute").split(",") if t.strip()]


def enabled_types():
    """The kinds that are switched on AND implemented."""
    return {t for t in requested() if t in SUPPORTED}


def unsupported():
    return [t for t in requested() if t not in SUPPORTED]


def signature_on():
    return config.value("execute_signature") != "0"


def is_gated(action):
    return action.get("type") in enabled_types()


def idem_key(item_key, index, action):
    body = f"{item_key}|{index}|{action.get('type')}|{action.get('id')}|{action.get(FIELD.get(action.get('type'), 'text'), '')}"
    return hashlib.sha1(body.encode("utf-8")).hexdigest()[:16]


def _clean(text):
    """No token, tenant/guid or URL in anything that is stored or shown."""
    t = re.sub(r"https?://\S+", "<url>", str(text))
    t = re.sub(r"Bearer\s+\S+", "Bearer <token>", t)
    t = re.sub(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", "<id>", t)
    return t[:160]


def comment_text(action):
    text = str(action.get("text", ""))
    return text + (SIGNATURE if signature_on() else "")


def _target(action):
    return f"作業項目 {action.get('id')}"


def _write_ado_comment(action, http, token_fn):
    org, project = pull.ado_names(config.value("ado_org"), config.value("ado_project"))
    if not org or not project:
        raise RuntimeError("ADO の組織とプロジェクトが設定されていません（config set ado_org / ado_project）")
    wid = str(action.get("id") or "").strip()
    if not wid.isdigit():
        raise RuntimeError("作業項目の番号が分かりません")
    token = token_fn(org)
    url = (f"https://dev.azure.com/{urllib.parse.quote(org)}/{urllib.parse.quote(project)}/_apis/wit/workItems/{wid}"
           "/comments?api-version=7.1-preview.4")
    res = http("POST", url, token, {"text": comment_text(action)}, retries=1)   # one try: a retry could write it twice
    return {"comment_id": (res or {}).get("id")}


def _token(org):
    return pull.az_token(pull.ADO_RESOURCE, pull.ado_tenant(org))


def _log(out, rec):
    p = Path(out) / "executions.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": _now(), **rec}, ensure_ascii=False) + "\n")


def run_approved(out, ap, num, item, post, http=None, token_fn=None, retry_only=False):
    """Execute the switched-on actions of an approved item. `post(text)` returns messages to the self chat.
    Returns [{"index", "type", "state"}]. Each action is saved before and after its call; one failing never stops the others."""
    token_fn = token_fn or _token
    http = http or pull.http_json   # looked up now, so a test (or a caller) can replace it
    state = item.setdefault("exec", {})
    results = []
    for i, action in enumerate(item["record"].get("actions", [])):
        t = action.get("type")
        if not is_gated(action):
            continue
        key = idem_key(item.get("key"), i, action)
        cur = state.get(str(i))
        if cur and cur.get("state") == "done":
            continue                                   # written already: never again
        if cur and cur.get("state") == "running" and not retry_only:
            _tell(post, f"[kimeru 実行 #{num}] {_target(action)} へのコメントは、結果が分かりません（実行の途中で止まった可能性があります）。"
                        f"ADO を確かめて、書かれていなければ「再実行 {num}」と返信してください")
            results.append({"index": i, "type": t, "state": "unknown"})
            continue
        if cur and cur.get("state") == "failed" and not retry_only:
            continue                                   # a failure waits for the PM's command
        state[str(i)] = {"key": key, "state": "running", "at": _now()}
        ap.save()                                      # before the call: a crash leaves a trace that says "not sure"
        try:
            res = _write_ado_comment(action, http, token_fn)
            state[str(i)] = {"key": key, "state": "done", "at": _now(), "result": res}
            _log(out, {"id": num, "key": key, "type": t, "target": str(action.get("id")), "state": "done", "result": res})
            _tell(post, f"[kimeru 実行 #{num}] 実行しました。{_target(action)} にコメント"
                        + (f"（コメント {res['comment_id']}）" if res.get("comment_id") else ""))
        except Exception as e:                        # any failure: recorded, told, the approval stays
            reason = _clean(e)
            state[str(i)] = {"key": key, "state": "failed", "at": _now(), "error": reason}
            _log(out, {"id": num, "key": key, "type": t, "target": str(action.get("id")), "state": "failed", "error": reason})
            _tell(post, f"[kimeru 実行 #{num}] 実行できませんでした。{_target(action)}: {reason}。直したら「再実行 {num}」と返信してください（承認は残っています）")
        ap.save()
        results.append({"index": i, "type": t, "state": state[str(i)]["state"]})
    return results


def report_unknown(ap, post):
    """An approved item whose execution was left "running" (the program stopped in the middle): tell the PM once that the
    result is not known. Nothing is executed again."""
    told = 0
    for num, item in ap.data["items"].items():
        for i, st in (item.get("exec") or {}).items():
            if st.get("state") == "running" and not st.get("told"):
                action = item["record"].get("actions", [])[int(i)]
                _tell(post, f"[kimeru 実行 #{num}] {_target(action)} へのコメントは、結果が分かりません（実行の途中で止まった可能性があります）。"
                            f"ADO を確かめて、書かれていなければ「再実行 {num}」と返信してください")
                st["told"] = True
                told += 1
    if told:
        ap.save()
    return told


def redo(out, ap, num, item, post, http=None, token_fn=None):
    """`再実行 N`: run again the actions that failed or whose result is unknown (only on the PM's command)."""
    state = item.setdefault("exec", {})
    for k, v in list(state.items()):
        if v.get("state") in ("failed", "running"):
            del state[k]
    ap.save()
    return run_approved(out, ap, num, item, post, http=http, token_fn=token_fn, retry_only=True)


def send_ready_posts(num, item, post, link=False):
    """teams.reply / teams.post are not sent. The text goes back to the self chat, alone, ready to copy."""
    for action in item["record"].get("actions", []):
        if action.get("type") in SEND_READY and str(action.get("text", "")).strip():
            body = f"[kimeru 送信用 #{num}]\n{action['text']}"
            if link and action.get("chat_id"):
                body += "\n\n相手とのチャットを開く（実験的）: https://teams.microsoft.com/l/chat/" \
                        + urllib.parse.quote(str(action["chat_id"]), safe="") + "/conversations"
            _tell(post, body)


def _tell(post, text):
    try:
        post(text)
    except Exception:   # a message that cannot be posted is still in executions.jsonl
        pass


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
