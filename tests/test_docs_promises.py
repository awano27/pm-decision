"""What the first screen promises matches what kimeru does (README top, hero image, talk script, status table)."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import re
import unittest
from pathlib import Path

try:
    from . import hidden_words
except ImportError:
    import hidden_words

from kimeru import config

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
TOP = "\n".join(README.splitlines()[:50])


def status_row(mark):
    return next(l for l in README.splitlines() if l.startswith(f"| {mark}"))


def quickstart():
    return (ROOT / "docs" / "quickstart.md").read_text(encoding="utf-8")


def code_lines(text):
    """The non-empty lines inside the ``` blocks of a document."""
    out, inside = [], False
    for line in text.splitlines():
        if line.startswith("```"):
            inside = not inside
        elif inside and line.strip():
            out.append(line.strip())
    return out


def without_backend(cmd):
    """A command with its --backend choice dropped: the backend is named in the reference once, not in every line."""
    return re.sub(r"\s--backend \S+", "", cmd).strip()


class TestTheFirstScreen(unittest.TestCase):
    def test_the_top_says_the_phone_is_not_notified_and_you_send_it_yourself(self):
        self.assertIn("スマホには通知されません", TOP)
        self.assertIn("あなたがコピーして", TOP)
        self.assertNotRegex(TOP, r"スマホから\s*`?OK")

    def test_the_hero_image_does_not_promise_a_phone(self):
        svg = (ROOT / "assets" / "readme" / "hero.svg").read_text(encoding="utf-8")
        self.assertNotIn("iPhone", svg)
        self.assertNotIn("先回りして決める", svg)   # kimeru prepares; the PM decides (the share card is made from this picture)
        self.assertNotIn("「決定」か「助言」", svg)

    def test_what_moved_out_of_the_readme_is_still_written_in_the_usage_doc(self):
        usage = (ROOT / "docs" / "usage.md").read_text(encoding="utf-8")
        for text in ("run examples/teams_chat.json", "http://127.0.0.1:8009/v1", "TYPESAFE_API_KEY", "| 向いている | 向かない |",
                     "コミット済みのファイルを使う", "**リリース延期の相談**", "**障害の通知**", "**情報不足のチケット**",
                     "**会議後の宿題**", "判断そのものを LLM に任せたい"):
            self.assertIn(text, usage, text)

    def test_the_default_judge_is_described_as_the_code_has_it(self):
        self.assertEqual(config.SETTINGS["backend"][1], "stub")
        self.assertIn("キーワードによる簡易判定", TOP)
        self.assertNotIn("既定はこの PC に接続した **Kev**", README)


class TestTheStatusTable(unittest.TestCase):
    def test_codex_is_not_listed_as_usable(self):
        self.assertNotIn("Codex", status_row("✅"))
        self.assertIn("Codex", status_row("⛔"))

    def test_what_has_no_record_on_a_real_pc_is_not_usable_yet(self):
        usable = status_row("✅")
        for thing in ("アラート", "Claude", "Grok"):
            self.assertNotIn(thing, usable)
            self.assertIn(thing, status_row("🔬"))

    def test_no_unsupported_third_party_review_claim(self):
        for path in [ROOT / "README.md", *(ROOT / "docs").glob("*.md")]:
            self.assertNotIn("第三者レビュー", path.read_text(encoding="utf-8"), path.name)


class TestCounts(unittest.TestCase):
    def test_the_playbook_count_in_the_docs_is_the_real_one(self):
        n = len(list((ROOT / "playbooks").glob("*.json")))
        self.assertIn(f"進め方の型（{n} 種類）", README)
        ref = (ROOT / "docs" / "reference.md").read_text(encoding="utf-8")
        self.assertIn(f"{n} 種類", ref)

    def test_the_fixture_count_in_eval_readme_is_the_real_one(self):
        n = sum(1 for l in (ROOT / "eval" / "fixtures.jsonl").read_text(encoding="utf-8").splitlines() if l.strip())
        first = (ROOT / "eval" / "README.md").read_text(encoding="utf-8").splitlines()[2]
        self.assertIn(f"({n} events)", first)

    def test_every_file_security_md_names_exists_in_the_code(self):
        sec = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
        code = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                         for p in [*(ROOT / "kimeru").glob("*.py"), *(ROOT / "tools").glob("*.ps1")])
        for name in set(re.findall(r"`([\w.-]+\.(?:jsonl|json|txt|bak|html))`", sec)):
            base = name.removesuffix(".bak")
            self.assertTrue(base in code or base.split(".")[0] in code, name)


class TestTheQuickstart(unittest.TestCase):
    def test_every_python_command_in_the_quickstart_is_written_in_the_readme_the_usage_or_the_reference(self):
        docs = [l for p in (ROOT / "README.md", ROOT / "docs" / "usage.md", ROOT / "docs" / "reference.md")
                for l in p.read_text(encoding="utf-8").splitlines()]
        cmds = [c for c in code_lines(quickstart()) if c.startswith("python -m kimeru")]
        self.assertGreaterEqual(len(cmds), 3)   # the demo, the daily cycle, the judge with Kev
        for cmd in cmds:
            self.assertTrue(any(without_backend(cmd) in without_backend(d) for d in docs), cmd)

    def test_every_cmd_file_the_quickstart_runs_exists_and_is_described_in_the_docs(self):
        names = set(re.findall(r"\b([\w-]+\.cmd)\b", "\n".join(code_lines(quickstart()))))
        self.assertTrue(names)
        docs = "\n".join(p.read_text(encoding="utf-8") for p in
                         (ROOT / "README.md", ROOT / "docs" / "reference.md", ROOT / "docs" / "managed-pc-check.md"))
        for name in names:
            self.assertTrue((ROOT / name).is_file(), name)
            self.assertIn(name, docs, name)

    def test_each_run_check_step_names_a_check_the_manual_describes(self):
        manual = (ROOT / "docs" / "managed-pc-check.md").read_text(encoding="utf-8")
        items = re.findall(r"run-check\.cmd (T\d+)", quickstart())
        self.assertTrue(items)
        for item in items:
            self.assertRegex(manual, rf"(?m)^### {item} ", item)

    def test_the_demo_step_names_the_summary_and_report_the_readme_shows(self):
        for mark in ("=== まとめ", "out/demo/report.html"):
            self.assertIn(mark, quickstart())
            self.assertIn(mark, README)

    def test_the_quickstart_has_five_steps_each_with_time_command_sign_and_where_to_look(self):
        steps = re.split(r"(?m)^## (?=\d\. )", quickstart())[1:]
        self.assertEqual(len(steps), 5)
        for step in steps:
            for label in ("所要時間", "コマンド", "できたと分かる印", "失敗したとき"):
                self.assertIn(label, step)
            self.assertRegex(step, r"\]\([^)\s]+\.md(#[^)\s]+)?\)|\]\(\.\./README\.md#[^)\s]+\)")

    def test_the_quickstart_names_the_toast_setting_that_turns_the_pc_notice_off(self):
        self.assertEqual(config.SETTINGS["toast"][0], "KIMERU_TOAST")
        self.assertIn("KIMERU_TOAST=0", quickstart())

    def test_the_quickstart_names_the_queue_file_that_the_waiting_items_go_to(self):
        self.assertIn("out/queue.jsonl", quickstart())
        self.assertIn("queue.jsonl", (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"))

    def test_the_quickstart_says_the_default_judge_is_only_keyword_matching(self):
        self.assertIn("キーワードによる簡易判定", quickstart())

    def test_the_self_chat_test_step_asks_before_it_sends_one_message(self):
        q = quickstart()
        self.assertIn("[y/N]", q)
        self.assertIn("自分とのチャット", q)

    def test_the_quickstart_is_linked_from_the_first_steps_and_the_docs_table(self):
        first_steps = README[README.index("## 🚀 まず動かす"):README.index("## 🧠 しくみ")]
        docs_table = README[README.index("## 📚 ドキュメント"):]
        self.assertIn("(docs/quickstart.md)", first_steps)
        self.assertIn("(docs/quickstart.md)", docs_table)


class TestTheQuickstartPromisesNoMore(unittest.TestCase):
    def test_the_quickstart_does_not_say_it_replies_or_sends_to_someone_else(self):
        for phrase in ("自動で返信", "相手に送る", "相手へ送る", "相手に返信", "自動で送"):
            self.assertNotIn(phrase, quickstart(), phrase)

    def test_the_quickstart_names_no_workplace_word(self):
        self.assertEqual(hidden_words.found(quickstart()), [])

    def test_the_quickstart_gives_no_figure_with_a_unit_for_jev_and_no_percentage(self):
        q = quickstart()
        self.assertNotIn("%", q)
        for line in q.splitlines():
            if "Jev" in line:   # a step's time (5 分) is not a figure of the judge; a count, a rate or a speed is
                self.assertNotRegex(line, r"\d\s*(件|秒|点|回|割|%)", line)


class TestTheKevBundleDocs(unittest.TestCase):
    """N3: the Kev folder is built by one command, and the seven documented mismatches are fixed (one line each)."""
    BUNDLE = (ROOT / "docs" / "kev-bundle.md").read_text(encoding="utf-8")
    MANAGED = (ROOT / "docs" / "managed-environments.md").read_text(encoding="utf-8")
    CHECK = (ROOT / "docs" / "managed-pc-check.md").read_text(encoding="utf-8")
    REF = (ROOT / "docs" / "reference.md").read_text(encoding="utf-8")
    PREPARE = (ROOT / "tools" / "prepare-bundle.ps1").read_text(encoding="utf-8-sig")
    SECURITY = (ROOT / "SECURITY.md").read_text(encoding="utf-8")

    def test_the_builder_and_its_launcher_exist_and_the_bundle_doc_names_the_launcher(self):
        self.assertTrue((ROOT / "tools" / "build-kev-bundle.ps1").is_file())
        self.assertTrue((ROOT / "build-kev-bundle.cmd").is_file())
        self.assertIn("build-kev-bundle.cmd", self.BUNDLE)

    def test_the_bundle_doc_makes_the_one_command_the_main_path_and_folds_the_manual_steps(self):
        self.assertIn("<details>", self.BUNDLE)
        self.assertIn("手作業", self.BUNDLE)
        self.assertLess(self.BUNDLE.index("build-kev-bundle.cmd"), self.BUNDLE.index("<details>"))

    def test_the_bundle_doc_says_how_to_install_uv_in_one_line(self):
        self.assertIn("powershell -ExecutionPolicy ByPass -c \"irm https://astral.sh/uv/install.ps1 | iex\"", self.BUNDLE)

    def test_the_manual_steps_install_python_313_with_uv_before_they_find_it(self):
        self.assertIn("uv python install 3.13", self.BUNDLE)
        self.assertLess(self.BUNDLE.index("uv python install 3.13"),
                        self.BUNDLE.index("uv python find --managed-python 3.13"))

    def test_the_manual_steps_take_the_pip_marker_off_the_copy_before_they_install_torch(self):
        self.assertIn(r"Remove-Item C:\develop\kev-bundle\python\Lib\EXTERNALLY-MANAGED", self.BUNDLE)
        self.assertLess(self.BUNDLE.index("uv python find --managed-python 3.13"), self.BUNDLE.index("EXTERNALLY-MANAGED"))
        self.assertLess(self.BUNDLE.index("EXTERNALLY-MANAGED"), self.BUNDLE.index("python.exe -m pip install"))
        self.assertIn('python.exe -m pip install "torch==<pins.json の torch>"', self.BUNDLE)
        self.assertNotIn("uv python find 3.13", self.BUNDLE)

    def test_managed_environments_says_the_double_click_is_for_the_managed_pc(self):
        self.assertIn("管理された PC 側の操作はダブルクリック 1 回", self.MANAGED)

    def test_managed_environments_points_the_kev_folder_at_the_bundle_doc_not_the_upstream(self):
        self.assertNotIn("[Kev の手順](https://github.com/jaredpalmer/kev)", self.MANAGED)
        self.assertIn("(kev-bundle.md)", self.MANAGED)

    def test_managed_pc_check_links_the_kev_folder_to_the_bundle_doc(self):
        self.assertRegex(self.CHECK, r"\(kev-bundle\.md\)")
        self.assertIn("build-kev-bundle.cmd", self.CHECK)

    def test_prepare_bundle_says_fp32_needs_about_fourteen_gigabytes_and_keeps_the_threshold(self):
        self.assertIn("fp32 で約 14GB", self.PREPARE)
        self.assertIn("($freeRam -ge 11)", self.PREPARE)

    def test_reference_says_the_bundle_launcher_chooses_kev_dtype_when_it_is_unset(self):
        row = next(l for l in self.REF.splitlines() if l.startswith("| `KEV_DTYPE`"))
        self.assertIn("start-kev.cmd", row)
        self.assertNotIn("未設定なら CPU に合わせて自動", row)

    def test_readme_gives_the_kev_prerequisites_and_names_the_builder(self):
        self.assertIn("build-kev-bundle.cmd", README)
        self.assertIn("bf16 で約 10GB", README)
        self.assertIn("fp32 で約 14GB", README)
        self.assertIn("uv", README)

    def test_security_md_lists_the_four_destinations_of_the_builder_in_one_row(self):
        row = next(l for l in self.SECURITY.splitlines() if l.startswith("|") and "build-kev-bundle" in l)
        for host in ("github.com", "pypi.org", "download.pytorch.org", "huggingface.co"):
            self.assertIn(host, row)
        self.assertIn("約 10GB", row)
        self.assertIn("[y/N]", row)


if __name__ == "__main__":
    unittest.main()
