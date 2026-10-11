"""R01: when an event reached the PC is measured, and only measured. The arrival comes from the PC's own observation of the Teams
chat list (a time label inside the window it was seen in, or the window's end) or from the source's own time (ADO CreatedDate,
alert firedDateTime). A time that cannot be backed up is Unknown, never estimated. The arrival never changes a judgment, a post,
a notice, the PC notification, the phone text, the judge's state or a graph key. Fakes only: no Teams, no ADO, no network."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kimeru import cli, daily, events, notify, push, pull
from kimeru.backends import StubBackend
from tests.test_daily import GRAPHS, PBS, FakeTeams, one_on_one
from tests.test_pull import ALERT, NOW, WI, FakeHttp
from tests.test_writer import FakeWriter

JST = timezone(timedelta(hours=9))
ARRIVAL_KEYS = ("arrived_at", "arrival_source", "arrival_window_s")
UNKNOWN = {"arrived_at": None, "arrival_source": "unknown", "arrival_window_s": None}
ONE = "19:a_b@unq.gbl.spaces"


def at(hour, minute, day=12):
    return datetime(2026, 10, day, hour, minute, tzinfo=JST)


def chat(preview, label, cid=ONE):
    return {**one_on_one(preview, label), "id": cid}


def taken(inbox):
    """The files the pull dropped, read and removed (what `watch` and `daily` take next)."""
    rows = []
    for f in sorted(Path(inbox).glob("*.json")):
        rows.append(json.loads(f.read_text(encoding="utf-8")))
        f.unlink()
    return rows


def arrival_of_record(rec):
    return {k: rec["measurement"].get(k) for k in ARRIVAL_KEYS}


class TeamsArrival(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.inbox, self.out = Path(self.dir.name) / "inbox", Path(self.dir.name) / "out"
        self.teams = FakeTeams()

    def tearDown(self):
        self.dir.cleanup()

    def poll(self, now, rows):
        """One pull_teams at `now` with the chat list `rows`; returns the payloads it dropped."""
        self.teams.chat_list = rows
        pull.pull_teams(self.inbox, self.out, bridge=self.teams, now=now)
        return taken(self.inbox)

    def judged(self, payload):
        return cli.process(payload, GRAPHS, StubBackend(), self.out, PBS)[0]

    def test_a_clock_label_inside_the_window_is_the_arrival_time(self):
        self.poll(at(10, 37), [chat("おはようございます", "9:00")])
        (payload,) = self.poll(at(10, 42), [chat("承認お願いします", "10:42")])
        m = self.judged(payload)["measurement"]
        self.assertEqual((m["arrival_source"], m["arrival_window_s"]), ("label", 60))
        self.assertEqual(datetime.fromisoformat(m["arrived_at"]), at(10, 42))

    def test_a_yesterday_label_gives_the_observation_time_and_the_window_width(self):
        self.poll(at(10, 37), [chat("おはようございます", "9:00")])
        (payload,) = self.poll(at(10, 42), [chat("承認お願いします", "昨日")])
        m = self.judged(payload)["measurement"]
        self.assertEqual((m["arrival_source"], m["arrival_window_s"]), ("observed", 300))
        self.assertEqual(datetime.fromisoformat(m["arrived_at"]), at(10, 42))

    def test_a_label_outside_the_window_is_not_used_and_the_observation_is_kept(self):
        self.poll(at(10, 37), [chat("おはようございます", "9:00")])
        (payload,) = self.poll(at(10, 42), [chat("承認お願いします", "10:20")])
        m = self.judged(payload)["measurement"]
        self.assertEqual((m["arrival_source"], m["arrival_window_s"]), ("observed", 300))

    def test_a_label_just_before_midnight_is_read_on_the_day_before(self):
        self.poll(at(23, 55, day=11), [chat("おはようございます", "9:00")])
        (payload,) = self.poll(at(0, 3), [chat("承認お願いします", "23:58")])
        m = self.judged(payload)["measurement"]
        self.assertEqual(m["arrival_source"], "label")
        self.assertEqual(datetime.fromisoformat(m["arrived_at"]), at(23, 58, day=11))

    def test_a_chat_seen_for_the_first_time_has_no_arrival_time(self):
        self.poll(at(10, 37), [chat("おはようございます", "9:00")])
        newcomer = chat("承認お願いします", "10:42", cid="19:c_d@unq.gbl.spaces")
        (payload,) = self.poll(at(10, 42), [chat("おはようございます", "9:00"), newcomer])
        self.assertEqual(arrival_of_record(self.judged(payload)), UNKNOWN)

    def test_the_first_poll_after_an_empty_chat_list_has_no_arrival_time(self):
        self.poll(at(10, 37), [chat("おはようございます", "9:00")])
        self.poll(at(10, 40), [])                                        # Teams was not on screen
        (payload,) = self.poll(at(10, 42), [chat("承認お願いします", "10:42")])
        self.assertEqual(arrival_of_record(self.judged(payload)), UNKNOWN)

    def test_the_first_pull_of_a_new_state_folder_with_include_existing_has_no_arrival_time(self):
        self.teams.chat_list = [chat("承認お願いします", "10:42")]
        pull.pull_teams(self.inbox, self.out, bridge=self.teams, include_existing=True, now=at(10, 47))
        (payload,) = taken(self.inbox)
        self.assertIsNone(payload["_kimeru"]["last_seen_at"])                # the first sighting: nothing to measure from
        self.assertEqual(arrival_of_record(self.judged(payload)), UNKNOWN)

    def test_a_window_longer_than_fifteen_minutes_is_unknown_and_exactly_fifteen_is_not(self):
        self.poll(at(10, 0), [chat("おはようございます", "9:00")])
        (late,) = self.poll(at(10, 30), [chat("承認お願いします", "10:30")])   # 30 minutes: the PC slept, nothing is estimated
        self.assertEqual(arrival_of_record(self.judged(late)), UNKNOWN)
        (edge,) = self.poll(at(10, 45), [chat("確認お願いします", "10:45")])  # exactly 15 minutes: the label can be used
        self.assertEqual(self.judged(edge)["measurement"]["arrival_source"], "label")

    def test_a_clock_that_went_back_gives_no_arrival_time(self):
        self.poll(at(10, 42), [chat("おはようございます", "9:00")])
        (payload,) = self.poll(at(10, 37), [chat("承認お願いします", "10:37")])
        self.assertEqual(arrival_of_record(self.judged(payload)), UNKNOWN)

    def test_without_the_note_the_arrival_is_unknown_and_with_it_the_label_is_used(self):
        self.poll(at(10, 37), [chat("おはようございます", "9:00")])
        (payload,) = self.poll(at(10, 42), [chat("承認お願いします", "10:42")])
        from_memory = self.judged({k: v for k, v in payload.items() if k != "_kimeru"})
        self.assertEqual(arrival_of_record(from_memory), UNKNOWN)   # no note, no arrival: never estimated
        self.assertEqual(self.judged(payload)["measurement"]["arrival_source"], "label")


class SourceTimes(unittest.TestCase):
    def test_ado_created_date_with_a_seven_digit_fraction_is_the_arrival_time(self):
        item = json.loads(json.dumps(WI))
        item["fields"]["System.CreatedDate"] = "2026-09-25T00:30:00.1234567Z"
        http = FakeHttp([("/wiql", {"asOf": "2026-09-25T00:35:00Z", "workItems": [{"id": 4812}]}),
                         ("/workitems?ids=4812", {"value": [item]})])
        with tempfile.TemporaryDirectory() as d:
            inbox, out = Path(d) / "inbox", Path(d) / "out"
            pull.pull_ado("org", "proj", inbox, out, http=http, token="t", now=NOW)
            (payload,) = taken(inbox)
        ev = events.normalize(payload)[0]
        self.assertEqual(pull.arrival_of(ev, payload.get("_kimeru"))["arrived_at"], "2026-09-25T00:30:00+00:00")
        self.assertEqual(pull.arrival_of(ev, payload.get("_kimeru"))["arrival_source"], "azure-devops")

    def test_an_alert_fired_time_with_a_one_digit_fraction_is_the_arrival_time(self):
        alert = json.loads(json.dumps(ALERT))
        alert["properties"]["essentials"]["startDateTime"] = "2026-09-25T00:50:00.5Z"
        ev = events.normalize(pull.alert_payload(alert))[0]
        found = pull.arrival_of(ev, None)
        self.assertEqual((found["arrived_at"], found["arrival_source"]), ("2026-09-25T00:50:00+00:00", "azure-monitor"))

    def test_an_unreadable_source_time_is_unknown_and_not_estimated(self):
        ev = {"kind": "ado.workitem.created", "source": "azure-devops", "id": "1", "ts": "soon"}
        self.assertEqual(pull.arrival_of(ev, {"observed_at": "2026-10-12T10:42:00+09:00"}), UNKNOWN)


BROKEN = ["text", ["a"], None, 5, True, {}, {"observed_at": None, "last_seen_at": None},
          {"observed_at": "not a time", "last_seen_at": 7}, {"observed_at": "2026-10-12T10:42:00"},
          {"observed_at": "2026-10-12T10:37:00+09:00", "last_seen_at": "2026-10-12T10:42:00+09:00"},
          {"observed_at": "2099-10-12T10:45:00+09:00", "last_seen_at": "2099-10-12T10:37:00+09:00"},
          {"observed_at": "2026-10-12T10:42:00+09:00", "last_seen_at": "2026-10-12T10:37:00+09:00", "extra": {"nested": [1]}},
          {"observed_at": "9" * 5000, "last_seen_at": "1" * 5000},
          {"observed_at": "2026-10-12T10:42:00.1234567+09:00", "last_seen_at": "2026-10-12T10:37:00.7654321+09:00"}]
TEAMS_EVENT = {"kind": "teams.chat", "source": "teams-ui", "id": "19:a_b#1-abc-1", "chat_id": ONE, "chat_kind": "oneOnOne",
               "author": "Sato", "chat_title": "Sato", "ts": "10:42", "mentions_me": False, "unread": True,
               "text": "承認お願いします"}


class SpyJudge(StubBackend):
    """Records the state the judge is sent (what Kev or Jev would read)."""

    def __init__(self):
        self.states = []

    def ask(self, state, questions):
        self.states.append(json.dumps(state, sort_keys=True, ensure_ascii=False))
        return super().ask(state, questions)


def decision_view(res):
    return {k: res.get(k) for k in ("graph", "event_id", "node", "outcome", "path", "notify", "needs_human", "graph_key", "event",
                                    "actions", "advice")}


class BrokenNotesNeverChangeAJudgment(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def test_no_broken_note_changes_the_decision_or_parks_the_file(self):
        plain = cli.process(dict(TEAMS_EVENT), GRAPHS, StubBackend(), self.root / "plain", PBS)[0]
        for i, note in enumerate(BROKEN):
            name = f"case{i}"
            inbox, out = self.root / name / "inbox", self.root / name / "out"
            inbox.mkdir(parents=True)
            (inbox / "teams-x.json").write_text(json.dumps({**TEAMS_EVENT, "_kimeru": note}, ensure_ascii=False), encoding="utf-8")
            daily.process_inbox(inbox, out, GRAPHS, StubBackend(), PBS, cli.process)
            self.assertEqual(list((inbox / "done").glob("*.error")), [], repr(note)[:60])
            self.assertEqual(len(list((inbox / "done").glob("*.json"))), 1, repr(note)[:60])
            rows = [json.loads(line) for line in (out / "decisions.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), 1, repr(note)[:60])
            self.assertEqual(decision_view(rows[0]), decision_view(json.loads(json.dumps(plain, ensure_ascii=False))), repr(note)[:60])
            self.assertTrue(set(ARRIVAL_KEYS) <= set(rows[0]["measurement"]), repr(note)[:60])   # the fields are always there

    def test_the_judge_is_sent_the_same_state_with_or_without_the_note(self):
        noted = SpyJudge()
        cli.process({**TEAMS_EVENT, "_kimeru": {"observed_at": "2026-10-12T10:42:00+09:00",
                                                 "last_seen_at": "2026-10-12T10:37:00+09:00"}},
                    GRAPHS, noted, self.root / "a", PBS)
        plain = SpyJudge()
        cli.process(dict(TEAMS_EVENT), GRAPHS, plain, self.root / "b", PBS)
        self.assertTrue(plain.states)
        self.assertEqual(noted.states, plain.states)
        self.assertNotIn("_kimeru", noted.states[0])

    def test_the_note_is_hidden_from_the_judge_by_name_too(self):
        self.assertIn("_kimeru", events.JUDGE_HIDDEN)
        self.assertEqual(set(events.state_of({**TEAMS_EVENT, "_kimeru": {"observed_at": "x"}})), set(events.state_of(TEAMS_EVENT)))

    def test_no_arrival_time_reaches_a_post_a_toast_the_phone_text_or_the_self_chat(self):
        with tempfile.TemporaryDirectory() as d:
            out, inbox, teams = Path(d) / "out", Path(d) / "inbox", FakeTeams()
            toasts = []
            run = lambda now: daily.cycle(out, inbox, GRAPHS, StubBackend(), PBS, cli.process, bridge=teams, send=True,
                                          now=now, toaster=lambda title, body: toasts.append((title, body)))
            teams.chat_list = [one_on_one("おはようございます", "8:00")]
            run(datetime(2026, 9, 28, 7, 0))
            teams.chat_list = [one_on_one("至急で承認お願いします", "10:42")]
            run(datetime(2026, 9, 28, 10, 47))
            rows = [json.loads(line) for line in (out / "decisions.jsonl").read_text(encoding="utf-8").splitlines()]
            arrived = [r["measurement"].get("arrived_at") for r in rows if r.get("measurement", {}).get("arrived_at")]
            sent = " ".join(text for text, _ in teams.posts) + " " + " ".join(" ".join(t) for t in toasts)
            sent += " " + push.message([1], 0, 0)
            for value in arrived:
                self.assertNotIn(value, sent)
            self.assertNotIn("arrival", sent)
            self.assertNotIn("到着", sent)


class SpyWriter(FakeWriter):
    """Records the event the writer is handed (what the drafting model would read)."""

    def __init__(self):
        super().__init__()
        self.events = []

    def draft(self, res, event, instruction=None):
        self.events.append(json.dumps(event, sort_keys=True, ensure_ascii=False))
        return super().draft(res, event, instruction)


class WriterNeverSeesTheNote(unittest.TestCase):
    def test_a_valid_note_is_measured_but_never_reaches_the_writer_or_the_material(self):
        note = {"observed_at": "2026-10-12T10:42:00+09:00", "last_seen_at": "2026-10-12T10:37:00+09:00"}
        spy = SpyWriter()
        with tempfile.TemporaryDirectory() as d:
            res = cli.process({**TEAMS_EVENT, "_kimeru": note}, GRAPHS, StubBackend(), Path(d), PBS, writer=spy)[0]
        self.assertEqual(len(spy.events), 1)                                          # the writer did work on this event
        self.assertEqual(res["measurement"]["arrival_source"], "label")                # the note was valid and was used
        for text in spy.events + [json.dumps(res.get("material_event") or {}, sort_keys=True, ensure_ascii=False)]:
            for word in ("_kimeru", "observed_at", "last_seen_at"):
                self.assertNotIn(word, text)


def scenario():
    """The fixed Teams scenario: its posts, the PC notification and the phone text. Identical before and after R01."""
    toasts = []
    with tempfile.TemporaryDirectory() as d:
        out, inbox, t = Path(d) / "out", Path(d) / "inbox", FakeTeams()

        def run(now):
            return daily.cycle(out, inbox, GRAPHS, StubBackend(), PBS, cli.process, bridge=t, send=True, now=now,
                               toaster=lambda title, body: toasts.append([title, body]))

        t.chat_list = [one_on_one("おはようございます", "8:00")]
        run(datetime(2026, 9, 28, 7, 0))
        t.chat_list = [one_on_one("これは何ですか", "8:05")]
        n = run(datetime(2026, 9, 28, 8, 10))["notify"][0]
        t.timeline.append(f"R:OK {n}")
        run(datetime(2026, 9, 28, 8, 15))
        t.chat_list = [one_on_one("来週の件、確認お願いします。至急です", "10:42")]
        run(datetime(2026, 9, 28, 10, 47))
        return {"posts": [[text, bool(sent)] for text, sent in t.posts], "toasts": toasts,
                "push": push.message([1, 2], 1, 1)}


class PostsStayTheSame(unittest.TestCase):
    def test_posts_toasts_and_the_phone_text_are_the_same_as_before_the_measurement(self):
        self.assertEqual(scenario(), GOLDEN)


GOLDEN = {'posts': [['[kimeru #1] Satoさん: これは何ですか\n'
            '→ 意図が判定できないチャット: Sato「これは何ですか」\n'
            '案: 受領しました。内容を確認して返信します。\n'
            'OK 1 / NG 1 / 詳細 1',
            True],
           ['[kimeru brief 2026-09-28] 今日の進め方\n'
            '1. 確認待ち #1: 意図が判定できないチャット: Sato「これは何ですか」\n'
            '確認待ち 1 件（OK 番号 / NG 番号 / 詳細 番号 で返信）',
            True],
           ['[kimeru 送信用 #1]\n受領しました。内容を確認して返信します。', True],
           ['[kimeru #2] Satoさん: 来週の件、確認お願いします。至急です\n'
            '→ 意図が判定できないチャット: Sato「来週の件、確認お願いします。至急です」\n'
            '案: 受領しました。内容を確認して返信します。\n'
            'OK 2 / NG 2 / 詳細 2',
            True]],
 'toasts': [['kimeru: 確認待ち 1 件', '#1'],
            ['kimeru: 返事を受け付けました', '#1 承認'],
            ['kimeru: 確認待ち 1 件', '#2']],
 'push': 'kimeru: 確認待ち 2 件（#1, #2） / 自動決定の通知 1 件 / 投稿できていない確認待ち 1 件。Teams の自分とのチャットを確認してください'}


if __name__ == "__main__":
    unittest.main()
