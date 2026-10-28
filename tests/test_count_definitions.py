try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import contextlib
import io
import json
import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from kimeru import counting, demo, graph, plan, report, stats
from kimeru.backends import StubBackend
from kimeru.cli import process

ROOT = Path(__file__).resolve().parent.parent
PBS = plan.load_playbooks(ROOT / "playbooks")
GRAPHS = graph.load_dir(ROOT / "graphs", PBS)

RULE = {"node": "critical_incident", "answer": {"matched": "outage"}, "edge": "yes"}
MODEL = {"node": "intent", "answer": {"choice": "status", "confidence": 0.9}, "edge": "status"}


def _row(i, path, outcome="decide", human=False):
    return {"graph": "g", "event_id": f"e{i}", "event_kind": "teams.chat", "path": path, "outcome": outcome, "node": "n",
            "needs_human": human, "notify": False, "at": datetime.now(timezone.utc).isoformat(),
            "judge": {"name": "StubBackend"}, "advice": "x", "actions": []}


ROWS = [
    _row(1, [RULE]),                                   # a rule only: no model call
    _row(2, [RULE, MODEL]),                            # a rule hit, but the model was called too
    _row(3, [MODEL], outcome="decide", human=True),    # a draft or an execution waits for approval
    _row(4, [MODEL], outcome="advise", human=True),    # plain advice to the PM
    _row(5, [MODEL]),                                  # the model decided
]


class TestOneCount(unittest.TestCase):
    def test_definitions(self):
        t = counting.tally(ROWS)
        self.assertEqual((t["n"], t["human"], t["rule"], t["auto"], t["safe"]), (5, 2, 1, 3, 0))

    def test_digest_and_report_agree(self):
        t = counting.tally(ROWS)
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            (out / "decisions.jsonl").write_text("\n".join(json.dumps(r) for r in ROWS) + "\n", encoding="utf-8")
            digest = stats.week(out)
            m = re.search(r"人に回した (\d+) 件.*?規則だけで決めた (\d+) 件", digest)
            self.assertIsNotNone(m, digest)
            self.assertEqual((int(m.group(1)), int(m.group(2))), (t["human"], t["rule"]))
            html = report.build(out, GRAPHS)
            self.assertIn(f"<b>{t['human']}</b><span>人の確認へ", html)
            self.assertIn(f"<b>{t['rule']}</b><span>うち規則（安全網）で即決定", html)

    def test_demo_summary_matches_its_own_records(self):
        with tempfile.TemporaryDirectory() as d:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                demo.run(ROOT / "examples" / "demo_day.json", GRAPHS, StubBackend(), PBS, process, d, pace=0)
            text = buf.getvalue()
            rows = [json.loads(x) for x in (Path(d) / "decisions.jsonl").read_text(encoding="utf-8").splitlines() if x]
            t = counting.tally(rows)
            self.assertIn(f"人の確認 {t['human']}", text)
            self.assertIn(f"規則（安全網）で即決定 {t['rule']} 件", text)
            digest = stats.week(Path(d))
            m = re.search(r"人に回した (\d+) 件.*?規則だけで決めた (\d+) 件", digest)
            self.assertEqual((int(m.group(1)), int(m.group(2))), (t["human"], t["rule"]))
            html = report.build(Path(d), GRAPHS)
            self.assertIn(f"<b>{t['human']}</b><span>人の確認へ", html)


if __name__ == "__main__":
    unittest.main()
