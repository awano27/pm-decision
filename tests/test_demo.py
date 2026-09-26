import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from kimeru import demo, graph, plan
from kimeru.backends import StubBackend
from kimeru.cli import process

ROOT = Path(__file__).resolve().parent.parent
PBS = plan.load_playbooks(ROOT / "playbooks")
GRAPHS = graph.load_dir(ROOT / "graphs", PBS)


class TestDemo(unittest.TestCase):
    def test_scenario_runs_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                results = demo.run(ROOT / "examples" / "demo_day.json", GRAPHS, StubBackend(), PBS, process, d, pace=0)
            out = buf.getvalue()
            self.assertGreaterEqual(len(results), 9)
            for marker in ("[規則] critical_outage", "[規則] critical_bug", "自分とのチャットに投稿", "朝のまとめ", "=== まとめ"):
                self.assertIn(marker, out)
            # rules decide the two severe events without asking the model
            nodes = {r["node"] for r in results}
            self.assertIn("set_p1_critical", nodes)
            self.assertTrue(nodes & {"page", "page_planned"})
            self.assertTrue((Path(d) / "decisions.jsonl").exists())

    def test_every_queued_advise_proposes_an_action(self):
        for lst in GRAPHS.values():
            for g in lst:
                for nid, n in g["nodes"].items():
                    if n["kind"] == "advise" and n.get("queue"):
                        self.assertTrue(n.get("actions"), f"{g['name']}/{nid}: approving must do something")


if __name__ == "__main__":
    unittest.main()
