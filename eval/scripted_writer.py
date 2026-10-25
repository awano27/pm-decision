"""A writer that always answers the same way, for `--writer cmd` runs that must be repeatable
(KIMERU_WRITER_CMD="python eval/scripted_writer.py").

It reads the request on stdin and fills every key listed under 「書くもの」 with one fixed sentence. It does not use the material,
so a run measures how the request is built (which blocks it carries), not how well a model writes."""
import json
import re
import sys

prompt = sys.stdin.buffer.read().decode("utf-8", "replace")   # bytes: the console code page (cp1252 on CI) must not decide
keys = re.findall(r"(?m)^- (a\d+):", prompt) or ["a1"]
out = {k: "承知しました。内容を確認して、改めてご連絡します。" for k in keys}
out["memo"] = {"summary": "確認の依頼", "missing": [], "options": [], "next": "内容を確認する"}
sys.stdout.buffer.write((json.dumps(out, ensure_ascii=False) + "\n").encode("utf-8"))
