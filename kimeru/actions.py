"""Action executors. MVP is dry-run only: actions are recorded, never sent."""

KNOWN = {
    "teams.reply": "Reply in the originating Teams chat",
    "teams.post": "Post to a Teams channel",
    "ado.comment": "Add a comment to a work item",
    "ado.update": "Update work item fields",
    "ado.create": "Create a work item",
    "oncall.page": "Page the on-call engineer",
    "log.only": "Record only; no external effect",
}


def execute(action, dry_run=True):
    t = action.get("type")
    if t not in KNOWN:
        return {"status": "rejected", "reason": f"unknown action {t}", "action": action}
    if dry_run or t == "log.only":
        return {"status": "planned", "action": action}
    raise NotImplementedError(f"live executor for {t} is not enabled in this version")
