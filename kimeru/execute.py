"""Carry out an approved action for real: ADO comments only, and only for the kinds the PM switched on.

Default: nothing is executed (approving records a plan, as before). `config set execute ado.comment` turns on the one kind that is
implemented here. The rules:

  - a switched-on kind is never executed by itself: even a decision the graph made automatically waits for an approval;
  - what is written is the text the approval post showed, word for word, signature line included: it is fixed when the post is made
    (`freeze`, saved as exec_text) and a later change of the settings does not change it. An item posted before execution was
    switched on has no such text: an OK does not write, the post is shown again with the text and the target;
  - only the daily and approvals paths execute (`real=True`); the demo, eval scripts, `run` and `watch` only record;
  - the comment is sent as plain text (`<` and `&` escaped); the check for the same text compares what ADO stored, whether it wrapped
    the comment in tags or kept the escapes, with the approved text
  - each action has an idempotency key and a state saved BEFORE the call ("running") and after it: "done" (with the external id),
    "failed" (sure that nothing was written) or "unknown" (a timeout, a dropped connection, 502-504, or a failure to record after
    the write: it may have been written). "running"/"unknown" is never executed again on its own: the PM checks ADO, then answers
    `済 N` (it is there: closed) or `再実行 N` (run again; ADO is asked first whether the same text is already there);
  - a failure never cancels the approval; running it again is the PM's command, and one `再実行 N` reply acts once;
  - the target (organization / project) recorded when the work item was pulled must equal the settings, or nothing is written;
    an item with no recorded origin is shown as "not written" in the post, and OK records the plan only (state "skipped": not a failure);
  - a lock file lets one run at a time change approvals.json (collect, notify, the merge of a follow-up message, purge); its owner is
    known by its process id: a lock of a live process is never taken over, that of a dead one is; the check for the same text reads every page of comments;
    when that read fails on a redo, an unknown result stays unknown (`済 N` still works);
  - the result goes back to the self chat through an outbox (kept until it is really posted), and every attempt, `running`
    included, is written to executions.jsonl;
  - teams.reply / teams.post are never sent: approving one returns a post with the text alone, ready to copy.

Authentication is the PM's own `az` sign-in, the token in memory only; organization and project come from the settings.
Tokens, tenant ids and URL queries never reach a record or a message.
"""
import hashlib
import html
import http.client
import json
import re
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from . import config, fsutil, pull

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
    body = f"{item_key}|{index}|{action.get('type')}|{action.get('id')}|{action.get('exec_text') or action.get(FIELD.get(action.get('type'), 'text'), '')}"
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


def current_target():
    """(organization, project) the settings name now."""
    return pull.ado_names(config.value("ado_org"), config.value("ado_project"))


NO_ORIGIN = "取り込み元が記録されていない"


def origin_of(rec):
    """(organization, project) recorded when the work item was pulled, or ("", "") when it is not recorded."""
    o = (rec.get("event") or {}).get("origin") or {}
    return pull.ado_names(o.get("org"), o.get("project"))


def where_of(item, action):
    """{"org", "project"} for a record of executions.jsonl: the origin of the work item, else the target that was fixed / set."""
    o_org, o_project = origin_of(item["record"])
    if o_org and o_project:
        return {"org": o_org, "project": o_project}
    tg = action.get("exec_target") or {}
    if not tg.get("org"):
        org, project = current_target()
        tg = {"org": org, "project": project}
    return {"org": tg.get("org") or "", "project": tg.get("project") or ""}


def freeze(rec, real):
    """Called when an approval post is made. Fixes, per switched-on action, the exact text that OK will write and the target it
    will be written to (both are shown in the post). Without `real` (demo, eval, run, watch) nothing is fixed and nothing is
    shown: a stale one is removed."""
    o_org, o_project = origin_of(rec)
    for a in rec.get("actions", []):
        if real and is_gated(a):
            org, project = current_target()
            a["exec_target"] = {"org": org, "project": project}
            if o_org and o_project:
                a["exec_text"] = comment_text(a)
                a["exec_origin"] = {"org": o_org, "project": o_project}
                a.pop("exec_skip", None)
            else:   # nothing says where the work item came from: OK records the plan and writes nothing (and nothing needs closing)
                a.pop("exec_text", None)
                a.pop("exec_origin", None)
                a["exec_skip"] = NO_ORIGIN
        else:
            for k in ("exec_text", "exec_target", "exec_origin", "exec_skip"):
                a.pop(k, None)


def _same_target(a, b):
    return [str(x).strip().lower() for x in a] == [str(x).strip().lower() for x in b]


def _target(action):
    return f"作業項目 {action.get('id')}"


class _Refused(RuntimeError):
    """Stopped before anything was sent: a definite failure."""


def ambiguous(e):
    """True when the write may have reached ADO although the call did not come back cleanly."""
    if isinstance(e, (TimeoutError, ConnectionError, http.client.HTTPException, OSError)):   # URLError and socket.timeout are OSErrors
        return True
    msg = str(e)
    m = re.match(r"HTTP (\d{3})\b", msg)
    if m:
        return m.group(1) in ("502", "503", "504")
    if msg.startswith("network error for"):
        return True
    m = re.search(r"not a JSON answer \(HTTP (\d+)\)", msg)
    if m:
        return m.group(1) != "203"     # 203 is the sign-in page: nothing was written
    return False


# a tag opens with a name (`<div>`, `</p>`, `<br/>`, `<a href="…">`): a `<` followed by a space, a digit or a symbol is text, so a
# sentence such as "a < 3 or b > 5" is never cut between its `<` and a later `>`
_TAG = re.compile(r"</?[A-Za-z][A-Za-z0-9-]*(?:\s[^<>]*)?/?>")


def _fold(text):
    return " ".join(str(text or "").split())


def _plain(text):
    """The form of a comment AS ADO STORES IT, for the comparison: tags removed (ADO may wrap the comment in `<div>`…), HTML escapes
    resolved (`&lt;` is a `<`), white space folded. The text we send is escaped first (see `_escaped`), so every `<` of the
    approved text is an escape here and is never taken for a tag: `<b>` and `List<String>` keep what they say, and two sentences that
    differ between a `<` and a later `>` stay different."""
    return _fold(html.unescape(_TAG.sub(" ", str(text or ""))))


def _plain_approved(text):
    """The approved text for the same comparison: escapes resolved and white space folded, and no tag removed (a `<b>` the PM
    approved is text)."""
    return _fold(html.unescape(str(text or "")))


def _wanted(text):
    """The forms of the approved text that a stored comment may have after _plain: as typed, or with its escapes resolved."""
    return {_fold(text), _plain_approved(text)}


def _escaped(text):
    """What is sent to ADO: the comment is shown as plain text, so a `<` and a `&` are escaped (a `<b>` must not become bold text,
    or vanish as a tag). Line breaks are sent as they are."""
    return html.escape(str(text), quote=False)


MAX_PAGES = 100


def _existing_comment(base, wid, text, http, token):
    """The id of a comment on the work item that already has this text (read only, every page), or None."""
    want = _wanted(text)
    cont, seen = None, set()
    for _ in range(MAX_PAGES):
        fsutil.heartbeat()
        url = f"{base}/{wid}/comments?api-version=7.1-preview.4&$top=200"
        if cont:
            url += "&continuationToken=" + urllib.parse.quote(str(cont), safe="")
        res = http("GET", url, token, None) or {}
        for c in res.get("comments", res.get("value", [])) or []:
            if _plain(c.get("text")) in want:
                return c.get("id") or "?"
        cont = res.get("continuationToken")
        if not cont or cont in seen:
            return None
        seen.add(cont)
    raise RuntimeError("too many pages of comments")


def inspect_comment(wid, marker, text, http=None, token_fn=None):
    """For the check on a real PC (tools/check.ps1 T22): how ADO stored the comment that was written (read only, every page).
    The comment is found by `marker`, a phrase of the text. Returns counts and booleans only, never text:
    {"found": n comments with the marker, "wrapped_in_tags": ADO put tags around it, "escaped": it kept the `<` as `&lt;`,
    "same_text_check": the check for the same text (_existing_comment) finds it}."""
    http = http or pull.http_json
    org, project = current_target()
    token = (token_fn or _token)(org)
    base = f"https://dev.azure.com/{urllib.parse.quote(org)}/{urllib.parse.quote(project)}/_apis/wit/workItems"
    stored, cont, seen = [], None, set()
    for _ in range(MAX_PAGES):
        url = f"{base}/{wid}/comments?api-version=7.1-preview.4&$top=200"
        if cont:
            url += "&continuationToken=" + urllib.parse.quote(str(cont), safe="")
        res = http("GET", url, token, None) or {}
        stored += [str(c.get("text") or "") for c in (res.get("comments", res.get("value", [])) or []) if marker in str(c.get("text") or "")]
        cont = res.get("continuationToken")
        if not cont or cont in seen:
            break
        seen.add(cont)
    return {"found": len(stored), "wrapped_in_tags": any(_TAG.search(t) for t in stored),
            "escaped": any("&lt;" in t or "&amp;" in t for t in stored),
            "same_text_check": any(_plain(t) in _wanted(text) for t in stored)}


class _CheckFailed(_Refused):
    """The read that asks whether the same text is already there failed: nothing was sent, and nothing more is known."""


def _write_ado_comment(action, item, http, token_fn, check_existing=False):
    org, project = current_target()
    if not org or not project:
        raise _Refused("ADO の組織とプロジェクトが設定されていません（config set ado_org / ado_project）")
    wid = str(action.get("id") or "").strip()
    if not wid.isdigit():
        raise _Refused("作業項目の番号が分かりません")
    text = action.get("exec_text")
    if not text:
        raise _Refused("承認の投稿で確定した文面がありません（書きません）")
    o_org, o_project = origin_of(item["record"])
    if not o_org or not o_project:
        raise _Refused("この作業項目の取り込み元（組織・プロジェクト）が記録されていないため、書きません")
    if not _same_target((o_org, o_project), (org, project)):
        raise _Refused(f"作業項目の取り込み元（{o_org}/{o_project}）と、設定の宛先（{org}/{project}）が違うため、書きません")
    fz = action.get("exec_target") or {}
    if fz and not _same_target((fz.get("org"), fz.get("project")), (org, project)):
        raise _Refused("承認の投稿に示した宛先と、今の設定が違うため、書きません")
    try:
        token = token_fn(org)
    except Exception as e:
        if check_existing:
            raise _CheckFailed("同じ文面のコメントがすでにあるか確かめられなかったため、書きません: " + _clean(e)) from None
        raise
    base = f"https://dev.azure.com/{urllib.parse.quote(org)}/{urllib.parse.quote(project)}/_apis/wit/workItems"
    if check_existing:   # before running it again: is the same text already there? (a read)
        try:
            found = _existing_comment(base, wid, text, http, token)
        except Exception as e:
            raise _CheckFailed("同じ文面のコメントがすでにあるか確かめられなかったため、書きません: " + _clean(e)) from None
        if found:
            return {"comment_id": found, "existing": True, "org": org, "project": project}
    url = f"{base}/{wid}/comments?api-version=7.1-preview.4"
    fsutil.heartbeat()
    res = http("POST", url, token, {"text": _escaped(text)}, retries=1)   # one try: a retry could write it twice
    return {"comment_id": (res or {}).get("id"), "org": org, "project": project}


def _token(org):
    return pull.az_token(pull.ADO_RESOURCE, pull.ado_tenant(org))


def _log(out, rec):
    p = Path(out) / "executions.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": _now(), **rec}, ensure_ascii=False) + "\n")


def unresolved(st):
    """An execution state that still waits for the PM: a failure (`再実行`), or a result that is not known (`済` / `再実行`)."""
    return (st or {}).get("state") in ("failed", "running", "unknown")


def not_known(st):
    return (st or {}).get("state") in ("running", "unknown")


def _unknown_text(num, action):
    return (f"[kimeru 実行 #{num}] {_target(action)} へのコメントは、書けたかどうか分かりません。"
            f"ADO を確かめてください（作業項目を開いて、コメントがあるかどうか）。"
            f"あれば「済 {num}」、無ければ「再実行 {num}」と返信してください"
            "（再実行の前に、kimeru も同じ文面がすでに書かれていないか確かめます）")


def run_approved(out, ap, num, item, post, http=None, token_fn=None, retry_only=False):
    """Execute the switched-on actions of an approved item. `post(text)` returns messages to the self chat.
    Returns [{"index", "type", "state"}]. Each action is saved before and after its call; one failing never stops the others.
    retry_only: the PM's `再実行 N` (failed / unknown ones run again, after ADO was asked whether the text is already there)."""
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
        if cur and cur.get("state") in ("done", "closed", "skipped"):
            continue                                   # written (or closed by the PM, or never to be written) already: never again
        where = where_of(item, action)
        if not all(origin_of(item["record"])):         # nothing says where it came from: recorded only, nothing to close
            state[str(i)] = {"key": key, "state": "skipped", "at": _now()}
            _log(out, {"id": num, "key": key, "type": t, "target": str(action.get("id")), "state": "skipped", "reason": NO_ORIGIN, **where})
            ap.save()
            _tell(post, f"[kimeru 実行 #{num}] {_target(action)} には書いていません（{NO_ORIGIN}ため）。承認は記録しました")
            results.append({"index": i, "type": t, "state": "skipped"})
            continue
        was_unknown = bool(cur and not_known(cur))
        if cur and not_known(cur) and not retry_only:
            if not cur.get("told"):
                cur["state"], cur["told"] = "unknown", True
                _tell(post, _unknown_text(num, action))
                ap.save()
            results.append({"index": i, "type": t, "state": "unknown"})
            continue
        if cur and cur.get("state") == "failed" and not retry_only:
            continue                                   # a failure waits for the PM's command
        state[str(i)] = {"key": key, "state": "running", "at": _now()}
        ap.save()                                      # before the call: a crash leaves a trace that says "not sure"
        _log(out, {"id": num, "key": key, "type": t, "target": str(action.get("id")), "state": "running", "retry": bool(retry_only), **where})
        try:
            res = _write_ado_comment(action, item, http, token_fn, check_existing=bool(retry_only))
        except _Refused as e:                          # nothing was sent (the check failed, or the target changed): if the earlier try may
            # have written, that stays unknown (`済 N` keeps working); only a run that had no earlier try is a sure failure
            _finish(out, ap, state, i, num, key, t, action, post, "unknown" if was_unknown else "failed", error=_clean(e),
                    where=where, unchecked=was_unknown)
        except Exception as e:                         # any other failure: a sure failure, or one whose result is not known
            _finish(out, ap, state, i, num, key, t, action, post, "unknown" if ambiguous(e) else "failed", error=_clean(e), where=where)
        else:
            try:                                       # the write is done: a failure to record it must not look like a failure to write
                _finish(out, ap, state, i, num, key, t, action, post, "done", result=res, where=where)
            except Exception:
                state[str(i)] = {"key": key, "state": "unknown", "at": _now(), "told": True, "result": res}
                try:
                    ap.save()
                except Exception:
                    pass
                _tell(post, _unknown_text(num, action) + "（書き込みは通りましたが、記録に失敗しました）")
        results.append({"index": i, "type": t, "state": state[str(i)]["state"]})
    return results


def _finish(out, ap, state, i, num, key, t, action, post, outcome, error=None, result=None, where=None, unchecked=False):
    rec = {"key": key, "state": outcome, "at": _now()}
    if error:
        rec["error"] = error
    if result:
        rec["result"] = result
    if outcome == "unknown":
        rec["told"] = True
    state[str(i)] = rec
    log = {"id": num, "key": key, "type": t, "target": str(action.get("id")), "state": outcome}
    log.update(where or {})
    if error:
        log["error"] = error
    if result:
        log["result"] = result
    _log(out, log)
    ap.save()
    if outcome == "done":
        if (result or {}).get("existing"):
            _tell(post, f"[kimeru 実行 #{num}] {_target(action)} には、同じ文面のコメント（{result.get('comment_id')}）がすでにあったため、書いていません")
        else:
            _tell(post, f"[kimeru 実行 #{num}] 実行しました。{_target(action)} にコメント"
                        + (f"（コメント {result['comment_id']}）" if (result or {}).get("comment_id") else "")
                        + (f"（{result['org']}/{result['project']}）" if (result or {}).get("org") else ""))
    elif outcome == "unknown" and unchecked:
        _tell(post, f"[kimeru 実行 #{num}] {_target(action)} に、前の試行が書いたかどうか（同じ文面のコメントがすでにあるか）、確かめられませんでした（{error}）。"
                    f"結果は分からないままです。ADO を確かめて、あれば「済 {num}」、無ければ「再実行 {num}」と返信してください")
    elif outcome == "unknown":
        _tell(post, _unknown_text(num, action) + f"（{error}）")
    else:
        _tell(post, f"[kimeru 実行 #{num}] 実行できませんでした（書けていません）。{_target(action)}: {error}。直したら「再実行 {num}」と返信してください（承認は残っています）")


def report_unknown(ap, post):
    """An approved item whose execution was left "running" (the program stopped in the middle) or "unknown": tell the PM once that
    the result is not known. Nothing is executed again."""
    told = 0
    for num, item in ap.data["items"].items():
        for i, st in (item.get("exec") or {}).items():
            if not_known(st) and not st.get("told"):
                action = item["record"].get("actions", [])[int(i)]
                st["state"] = "unknown"
                st["told"] = True
                _tell(post, _unknown_text(num, action))
                told += 1
    if told:
        ap.save()
    return told


def redo(out, ap, num, item, post, http=None, token_fn=None):
    """`再実行 N`: run again the actions that failed or whose result is unknown (only on the PM's command)."""
    return run_approved(out, ap, num, item, post, http=http, token_fn=token_fn, retry_only=True)


def close_unknown(out, ap, num, item):
    """`済 N`: the PM saw the comment in ADO. The actions whose result was not known are closed: nothing is executed, and
    Teams is not read for them any more. Returns how many were closed."""
    n = 0
    for i, st in (item.get("exec") or {}).items():
        if not_known(st):
            st["state"], st["closed_at"] = "closed", _now()
            act = item["record"]["actions"][int(i)]
            _log(out, {"id": num, "key": st.get("key"), "type": "ado.comment", "target": str(act.get("id")), "state": "closed",
                       **where_of(item, act)})
            n += 1
    if n:
        ap.save()
    return n


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
