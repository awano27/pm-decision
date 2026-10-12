"""R22 golden: with judge_context at its default (0) a local judge is asked the same questions with the same state, and the
judgments, the decisions.jsonl rows and processed.txt are the same as before the setting existed. The digest below was taken
from the code before R22 (a HEAD export); a change that alters the default path changes it. Only the clock-dependent fields
(`at`, `measurement`, `perf`) are left out. No network: the judge is the real Kev class answering from the stub's keywords."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import copy
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kimeru import cli
from kimeru.backends import KevBackend, StubBackend
from tests.test_daily import GRAPHS, PBS

NOW = datetime(2026, 10, 12, 3, 0, tzinfo=timezone.utc)
CHAT = "19:synthetic-chat-1@unq.gbl.spaces"
CLOCK = ("at", "measurement", "perf")
GOLDEN = "ac767d1b7dc18498a6e65e034867d2506ca0bf2c512185b18ae57f982cbeade8"   # scenario_digest() at HEAD 776a678, before R22


class RecordingKev(KevBackend):
    """The real Kev class (this PC by default) that records what it is asked."""

    def __init__(self):
        super().__init__()
        self.calls = []

    def ask(self, state, questions):
        self.calls.append([copy.deepcopy(state), copy.deepcopy(questions)])
        return StubBackend().ask(state, questions)


def _event(eid, text, author):
    return {"kind": "teams.chat", "source": "teams", "id": eid, "ts": "2026-10-12T02:59:00+00:00", "chat_id": CHAT,
            "author": author, "mentions_me": True, "text": text}


def _earlier(eid, hours_before, body):
    at = (NOW - timedelta(hours=hours_before)).isoformat(timespec="seconds")
    return {"at": at, "node": "intent", "outcome": "advise",
            "event": {"kind": "teams.chat", "id": eid, "chat_id": CHAT, "author": "Sato", "text": body}}


def scenario_digest():
    """Runs four chat messages, after one earlier message of the same chat is on record, and digests everything the default
    path produces that is not clock-dependent."""
    events = [_event("g-1", "至急で判断をお願いします。リリース日を来週に動かしてよいか決めてください", "Sato"),
              _event("g-2", "よろしくお願いします", "Sato"),
              _event("g-3", "本番で決済が通らないと顧客から連絡がありました", "Kato"),
              _event("g-4", "ログイン画面の改修、今どこまで進んでますか？", "Ito")]
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "out"
        out.mkdir()
        (out / "decisions.jsonl").write_text(json.dumps(_earlier("g-0", 1, "前の依頼"), ensure_ascii=False) + "\n", encoding="utf-8")
        judge = RecordingKev()
        results = [cli.process(ev, GRAPHS, judge, out, PBS, now=NOW, dedup=True) for ev in events]
        rows = [{k: v for k, v in json.loads(line).items() if k not in CLOCK}
                for line in (out / "decisions.jsonl").read_text(encoding="utf-8").splitlines()]
        payload = {"asked": judge.calls,
                   "results": [[{k: v for k, v in r.items() if k not in CLOCK} for r in rs] for rs in results],
                   "decisions": rows,
                   "processed": (out / "processed.txt").read_text(encoding="utf-8")}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class TestTheDefaultPathIsUnchanged(unittest.TestCase):
    def test_with_the_default_the_questions_judgments_and_records_are_the_recorded_golden(self):
        self.assertEqual(scenario_digest(), GOLDEN)


if __name__ == "__main__":
    unittest.main()
