"""Print the self-chat post that README.md shows under 「実際には、こう見える」.

The judgment is the offline keyword judge; the memo and the drafts come from a scripted writer that stands in for
Copilot, so the example is reproducible without any account: the texts are illustrative, the layout is the real
one (`kimeru.notify.format_post`). tests/test_readme_samples.py checks that README.md shows exactly this.

    python examples/readme_post.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kimeru import graph, notify, plan, writer  # noqa: E402
from kimeru.backends import StubBackend  # noqa: E402
from kimeru.cli import process  # noqa: E402

MSG = {"id": "1727000000001", "createdDateTime": "2026-09-23T09:12:00Z", "chatId": "19:abc@thread.v2",
       "from": {"user": {"displayName": "佐藤（QA）"}},
       "body": {"contentType": "text", "content": "リリース日を来週火曜にずらしてよいか判断お願いします。QA 環境が止まっていて至急です"},
       "mentions": [{"id": 0}]}

MEMO = {"summary": "QA 環境の障害によるリリース日程の変更判断が求められています。",
        "missing": ["QA 環境の障害の詳細と復旧見込み", "現在のリリース予定日", "影響を受ける関係者"],
        "options": ["延期: QA 完了を待つ（品質は確保、遅延のリスク）", "スコープ縮小: 機能を削って予定どおり",
                    "現状維持: 予定どおり（QA 未了のリスク）"],
        "next": "QA 環境の障害の詳細と復旧見込みを佐藤さんに確認する",
        "ask_back": "QA 環境の障害の詳細、復旧予定、現在のリリース予定日を教えていただけますか。"}


class ScriptedCopilot:
    """Stands in for the Copilot writer: same name, fixed texts."""
    NAME = "copilot"

    def draft(self, res, event, instruction=None):
        out = {"memo": dict(MEMO)}
        for key, a in writer.targets(res):
            out[key] = ("受領しました。まず遅延原因の復旧見込みを確認させていただきます。" if a["type"] == "teams.reply"
                        else f"{a.get('title')}: QA 環境の復旧見込みを確認し、結果を記録する")
        return out


from kimeru.demo import DemoTeams as Teams  # noqa: E402  (in memory: nothing is posted anywhere)


def post():
    with tempfile.TemporaryDirectory() as d:
        os.environ["KIMERU_STATE_DIR"] = str(Path(d) / "state")
        pbs = plan.load_playbooks(ROOT / "playbooks")
        gs = graph.load_dir(ROOT / "graphs", pbs)
        out = Path(d) / "out"
        process(json.loads(json.dumps(MSG)), gs, StubBackend(), out, pbs, writer=ScriptedCopilot())
        t = Teams()
        notify.notify(out, t, send=True)
        return t.posts[0]


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print(post())
