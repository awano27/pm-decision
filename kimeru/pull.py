"""Poll sources with the user's own `az login` (no app registration, no admin consent)
and drop raw payloads into the inbox that `kimeru watch` processes.

  ado    : WIQL "created since last poll" -> work items -> service-hook-shaped payloads
  alerts : Alerts Management API (fired, not closed)  -> common-alert-schema payloads
  teams  : Teams chat list on screen (UI Automation, no API/consent) -> teams.chat events
           for 1:1 chats and mentions whose preview/time changed since the last poll

Tokens come from `az account get-access-token`; the token is kept in memory only.
State (last poll time, seen ids) lives in out/pull_state.json.

NOTE: tested against recorded/fixture API shapes only; not yet run against a live tenant.
"""
import json
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ADO_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798"   # Azure DevOps
ARM_RESOURCE = "https://management.azure.com/"
ADO_FIELDS = ["System.Id", "System.WorkItemType", "System.Title", "System.AreaPath", "System.CreatedBy",
              "System.CreatedDate", "System.Description", "Microsoft.VSTS.TCM.ReproSteps",
              "Microsoft.VSTS.Common.AcceptanceCriteria", "Microsoft.VSTS.Common.Priority"]


AZ_FALLBACKS = (r"C:\az\bin\az.cmd",)   # the official no-install ZIP unpacked to C:\az


def find_az():
    """KIMERU_AZ, then az on PATH (az.cmd on Windows), then the ZIP location."""
    import os
    for cand in (os.environ.get("KIMERU_AZ"), shutil.which("az"), *AZ_FALLBACKS):
        if cand and Path(cand).exists():
            return cand
    return None


def az_token(resource):
    az = find_az()
    if not az:
        raise RuntimeError("Azure CLI (az) not found: unpack the ZIP to C:\\az or set KIMERU_AZ, then run `az login`")
    r = subprocess.run([az, "account", "get-access-token", "--resource", resource, "--query", "accessToken", "-o", "tsv"],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError("az get-access-token failed (run `az login`): " + r.stderr.strip()[:200])
    return r.stdout.strip()


class PullError(RuntimeError):
    pass


def http_json(method, url, token, body=None, retries=3):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    delay = 1.0
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": "Bearer " + token, "Content-Type": "application/json", "User-Agent": "kimeru/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(float(e.headers.get("Retry-After") or delay))
                delay *= 2
                continue
            hint = " (token expired? run `az login`)" if e.code in (401, 403) else ""
            raise PullError(f"HTTP {e.code} for {url.split('?')[0]}{hint}") from None
        except urllib.error.URLError as e:
            if attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise PullError(f"network error for {url.split('?')[0]}: {e.reason}") from None


class State:
    def __init__(self, out):
        self.path = Path(out) / "pull_state.json"
        self.data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def section(self, name):
        return self.data.setdefault(name, {"since": None, "seen": []})

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _drop(inbox, name, payload):
    inbox = Path(inbox)
    inbox.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
    tmp = inbox / (safe + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(inbox / (safe + ".json"))  # atomic: watch never sees a half-written file


def pull_ado(org, project, inbox, out, http=http_json, token=None, now=None, first_lookback_h=24):
    """Return number of new work items dropped into the inbox."""
    st = State(out)
    sec = st.section(f"ado:{org}/{project}")
    now = now or datetime.now(timezone.utc)
    since = sec["since"] or _iso(now - timedelta(hours=first_lookback_h))
    token = token or az_token(ADO_RESOURCE)
    base = f"https://dev.azure.com/{urllib.parse.quote(org)}/{urllib.parse.quote(project)}/_apis/wit"
    wiql = ("SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = @project "
            f"AND [System.CreatedDate] >= '{since}' ORDER BY [System.CreatedDate] ASC")
    res = http("POST", f"{base}/wiql?timePrecision=true&api-version=7.1", token, {"query": wiql})
    ids = [w["id"] for w in res.get("workItems", []) if w["id"] not in sec["seen"]]
    n = 0
    for i in range(0, len(ids), 200):  # workitems batch limit
        chunk = ids[i:i + 200]
        items = http("GET", f"{base}/workitems?ids={','.join(map(str, chunk))}&fields={','.join(ADO_FIELDS)}&api-version=7.1", token)
        for wi in items.get("value", []):
            _drop(inbox, f"ado-{org}-{wi['id']}", {"eventType": "workitem.created",
                                                   "resource": {"id": wi["id"], "fields": wi.get("fields", {})}})
            sec["seen"].append(wi["id"])
            n += 1
        sec["seen"] = sec["seen"][-2000:]
        st.save()  # a later failure must not re-deliver what is already in the inbox
    sec["since"] = _iso(now)
    st.save()
    return n


def alert_payload(a):
    """Alerts Management `alert` resource -> Azure Monitor common alert schema."""
    ess = (a.get("properties") or {}).get("essentials") or {}
    return {"schemaId": "azureMonitorCommonAlertSchema", "data": {"essentials": {
        "alertId": a.get("id"),
        "alertRule": ess.get("alertRule") or a.get("name"),
        "severity": ess.get("severity"),
        "monitorCondition": ess.get("monitorCondition"),
        "firedDateTime": ess.get("startDateTime"),
        "alertTargetIDs": [t for t in [ess.get("targetResource")] if t],
        "description": ess.get("description") or "",
    }, "alertContext": {"signalType": ess.get("signalType"), "alertState": ess.get("alertState")}}}


def pull_alerts(subscription, inbox, out, http=http_json, token=None, time_range="1h"):
    """Return number of new fired alerts dropped into the inbox (each alert id once)."""
    st = State(out)
    sec = st.section(f"alerts:{subscription}")
    token = token or az_token(ARM_RESOURCE)
    url = (f"https://management.azure.com/subscriptions/{urllib.parse.quote(subscription)}/providers/Microsoft.AlertsManagement/alerts"
           f"?api-version=2019-05-05-preview&monitorCondition=Fired&timeRange={time_range}")
    n = 0
    while url:
        res = http("GET", url, token)
        for a in res.get("value", []):
            ess = (a.get("properties") or {}).get("essentials") or {}
            if a.get("id") in sec["seen"] or ess.get("alertState") == "Closed":
                continue
            _drop(inbox, "alert-" + a["id"].rsplit("/", 1)[-1], alert_payload(a))
            sec["seen"].append(a["id"])
            n += 1
        sec["seen"] = sec["seen"][-2000:]
        st.save()  # per page, so a failed later page does not re-deliver earlier alerts
        url = res.get("nextLink")
    return n


OWN_PREFIXES = ("あなた:", "あなた：", "You:")


def teams_events(chats, sec, include_existing=False):
    """Diff the on-screen chat list against the last poll. Returns teams.chat events.

    First poll (no baseline) only records signatures unless include_existing.
    Only 1:1 chats and chats flagged as mentioning you are emitted; the self chat
    and your own last messages are ignored.
    """
    import hashlib
    first = not sec.get("sigs")
    sigs = sec.setdefault("sigs", {})
    out = []
    for c in chats:
        cid, kind = c.get("id"), c.get("kind")
        if not cid or kind == "self":
            continue
        sig = hashlib.sha1(f"{c.get('time')}|{c.get('preview')}".encode("utf-8")).hexdigest()[:16]
        prev = sigs.get(cid)
        sigs[cid] = sig
        if prev == sig or (first and not include_existing):
            continue
        preview = (c.get("preview") or "").strip()
        if not preview or preview.startswith(OWN_PREFIXES):
            continue
        if not (kind == "oneOnOne" or c.get("mention")):
            continue
        out.append({"kind": "teams.chat", "source": "teams-ui", "id": f"{cid}#{sig}", "chat_id": cid,
                    "chat_kind": kind, "author": c.get("title"), "ts": c.get("time"),
                    "mentions_me": bool(c.get("mention")), "unread": bool(c.get("unread")), "text": preview})
    return out


def pull_teams(inbox, out, bridge=None, include_existing=False):
    """Return number of new teams.chat events dropped into the inbox."""
    if bridge is None:
        from .notify import PowerShellBridge
        bridge = PowerShellBridge()
    st = State(out)
    sec = st.section("teams")
    evs = teams_events(bridge.chats(), sec, include_existing)
    for ev in evs:
        _drop(inbox, "teams-" + ev["id"].replace(":", "_").replace("@", "_").replace("#", "_")[-60:], ev)
    st.save()
    return len(evs)
