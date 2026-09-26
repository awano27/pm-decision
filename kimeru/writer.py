"""Drafts the words of an action after the judge has decided *what* to do.

The judge (Jev / Kev) picks the route; a writer LLM only fills in text: the Teams
reply and the descriptions of work items to create. Drafted replies never go out
on their own: they are queued so the PM approves the exact text (notify.py).

  KIMERU_WRITER=claude   Claude Code CLI (`claude -p`, the user's own login)
  (unset)                keep the graph's template text

The writer gets tools disabled and only the event plus the judge's route as
material, and it is told not to add facts. Any failure falls back to the template.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile

SYSTEM = (
    "あなたはプロジェクトマネージャーの下書き係です。与えられた材料だけを使い、"
    "材料にない事実（日付・人名・数値・約束）を作らないでください。"
    "敬体で簡潔に書き、指定の JSON だけを出力します。"
)

KIND = {"teams.reply": "reply", "ado.create": "task"}


def _material(res, event, instruction=None):
    lines = [f"送信者: {event.get('author') or '不明'}", f"メッセージ: {event.get('text') or event.get('item') or ''}", "",
             "kimeru の判断経路:"]
    for s in res.get("path", []):
        a = s.get("answer") or {}
        val = a.get("choice") or a.get("playbook") or (f"{a['noul']:.2f}" if "noul" in a else a.get("score"))
        lines.append(f"- {s['node']} = {val} → {s['edge']}")
    lines.append(f"結論: {res.get('node')}（{res.get('advice') or ''}）")
    if res.get("plan"):
        lines.append(f"進め方: {res['plan']['title']}")
        lines += [f"  {i + 1}. {st['title']}（{st.get('due', '')}）" for i, st in enumerate(res["plan"].get("steps", []))]
    acts = res.get("actions", [])
    replies = [a for a in acts if a.get("type") == "teams.reply"]
    tasks = [a["title"] for a in acts if a.get("type") == "ado.create"]
    lines += ["", "書くもの:"]
    if replies:
        lines.append(f"- reply: 送信者への Teams 返信（3 文以内）。意図は定型文「{replies[0].get('text', '')}」と同じ")
    if tasks:
        lines.append("- tasks: 次の作業項目それぞれの説明（2〜4 行。何を確認・作成すれば完了か。メッセージ由来の具体名を使う）")
        lines += [f"  - {t}" for t in tasks]
    if instruction:
        lines += ["", f"PM からの修正指示: {instruction}"]
    lines += ["", '出力形式: {"reply": "...", "tasks": {"<作業項目名>": "説明", ...}}（不要なキーは省略）']
    return "\n".join(lines)


def _parse(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return d if isinstance(d, dict) else None


class ClaudeWriter:
    NAME = "claude"

    def __init__(self, model=None, timeout=180, exe=None):
        self.model = model or os.environ.get("KIMERU_WRITER_MODEL", "sonnet")
        self.timeout = timeout
        self.exe = exe or shutil.which("claude")

    def _call(self, prompt):
        if not self.exe:
            raise RuntimeError("claude CLI not found")
        cmd = [self.exe, "-p", "--tools", "", "--strict-mcp-config", "--no-session-persistence",
               "--model", self.model, "--system-prompt", SYSTEM]
        with tempfile.TemporaryDirectory() as d:  # empty cwd: no project CLAUDE.md or files in reach
            r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                               timeout=self.timeout, cwd=d)
        if r.returncode != 0:
            raise RuntimeError((r.stdout + r.stderr).strip()[:300])
        return r.stdout

    def draft(self, res, event, instruction=None):
        return _parse(self._call(_material(res, event, instruction)))


def get_writer(name=None):
    name = (name if name is not None else os.environ.get("KIMERU_WRITER", "")).strip().lower()
    return ClaudeWriter() if name == "claude" else None


def apply(res, event, writer, instruction=None):
    """Fill drafted text into res["actions"] in place. Returns True if a reply was drafted.

    Keeps the template as `template_text` so the log shows what the writer changed."""
    if writer is None or not any(a.get("type") in KIND for a in res.get("actions", [])):
        return False
    try:
        d = writer.draft(res, event, instruction)
    except Exception as e:  # writer is optional: any failure keeps the template
        res["writer_error"] = f"{type(e).__name__}: {e}"[:300]
        return False
    if not d:
        res["writer_error"] = "no JSON in writer output"
        return False
    drafted = False
    tasks = d.get("tasks") if isinstance(d.get("tasks"), dict) else {}
    for a in res["actions"]:
        if a.get("type") == "teams.reply" and isinstance(d.get("reply"), str) and d["reply"].strip():
            a.setdefault("template_text", a.get("text", ""))
            a["text"], a["drafted_by"] = d["reply"].strip(), writer.NAME
            drafted = True
        elif a.get("type") == "ado.create" and isinstance(tasks.get(a.get("title")), str):
            a["description"], a["drafted_by"] = tasks[a["title"]].strip(), writer.NAME
    return drafted
