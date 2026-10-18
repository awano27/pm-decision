"""`kimeru try`: one case from arrival to the record, with no account, key, setting or connection.

A release-delay request arrives in a simulated Teams chat, the keyword judge (stub) reads it, a scripted writer fills in the
reply draft and the missing information, the case is posted to a simulated self chat, and the scripted replies ask for the
full text, a rewrite and an approval. The report shows the whole case on one page.

Safe whatever the machine is set up for:
- every KIMERU_* variable and every key kimeru knows is removed from this process before anything reads them, and the state
  folder is a fresh temporary one, so the user's config file, writer, judge, execute and push settings are never read;
- the judge is always the keyword stub and the writer always the script (no --backend, no --writer);
- while the case runs, the usual ways to start a process (subprocess, os.system / popen / spawn* / exec* / startfile, and
  _winapi.CreateProcess on Windows) and to open a connection (socket connect / sendto / sendmsg, getaddrinfo, gethostby*)
  raise `Sealed`; a direct ctypes call is not covered. Nothing here needs them, and a run that tried one ends with exit code 1.
  The safety rests first on the fixed parts (in-memory Teams, stub, script); the seal is the second line;
- the records go to --out (out/try by default). The folder of run and daily (out), the current folder, the state folder and a
  folder holding anything the try run did not write are refused; the try folder carries TRY_MARK, and the other commands that
  write records refuse it, so the fictional case #1 never mixes with real records;
- the environment and the settings module are put back as they were when the run ends.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

from . import config, demo, notify, pull, report
from . import writer as writer_mod
from .backends import StubBackend

HERE = Path(__file__).resolve().parent.parent
SCENARIO = HERE / "examples" / "try_release_delay.json"
DEFAULT_OUT = Path("out") / "try"
TRY_MARK = ".kimeru-try"   # the try run's folder: other commands that write records refuse it (cli._main), the demo clears it
TITLE = "kimeru 体験: リリース延期の相談"
# the keys the writer CLIs read for themselves (as in tests/isolate.py); kimeru's own are config.SECRET_ENV
WRITER_KEYS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY", "GITHUB_TOKEN", "GH_TOKEN", "COPILOT_GITHUB_TOKEN")
USAGE = "usage: python -m kimeru try [--out DIR]"


class Sealed(RuntimeError):
    """Something tried to start a process or open a connection during the try run."""


def isolate(state):
    """Drop every kimeru setting and key from this process and point the state folder at `state`. Returns the names dropped."""
    dropped = sorted(k for k in os.environ
                     if (k.startswith("KIMERU_") or k in config.SECRET_ENV or k in WRITER_KEYS) and k != "KIMERU_STATE_DIR")
    for k in dropped:
        del os.environ[k]
    os.environ.pop("KIMERU_STATE_DIR", None)
    os.environ["KIMERU_STATE_DIR"] = str(state)
    os.environ["KIMERU_TOAST"] = "0"
    config.ARGS.clear()
    config.apply([])
    return dropped


@contextmanager
def sealed(attempts=None):
    """No process and no network connection while the block runs: each attempt is noted in `attempts` and raises Sealed."""
    attempts = attempts if attempts is not None else []

    def refuse(what):
        def call(*a, **k):
            attempts.append(what)
            raise Sealed(f"体験モードでは {what} を使いません")
        return call

    saved = [(subprocess, "Popen"), (os, "system"), (os, "popen"), (socket, "create_connection"), (socket, "getaddrinfo"),
             (socket, "gethostbyname"), (socket, "gethostbyname_ex"), (socket, "gethostbyaddr"),
             (socket.socket, "connect"), (socket.socket, "connect_ex"), (socket.socket, "sendto")]
    if hasattr(socket.socket, "sendmsg"):   # not on Windows
        saved.append((socket.socket, "sendmsg"))
    saved += [(os, name) for name in dir(os) if name.startswith(("spawn", "exec", "posix_spawn")) or name == "startfile"]
    try:
        import _winapi
        saved.append((_winapi, "CreateProcess"))
    except ImportError:   # not Windows
        pass
    old = [(obj, name, getattr(obj, name)) for obj, name in saved]
    for obj, name in saved:
        setattr(obj, name, refuse(f"{getattr(obj, '__name__', 'socket')}.{name}"))
    try:
        yield attempts
    finally:
        for obj, name, value in old:
            setattr(obj, name, value)


class TryTeams(demo.DemoTeams):
    """The demo's in-memory Teams, plus the mark a detail post leaves on the screen (so a `詳細 N` is answered once)."""

    def post(self, text, send):
        got = super().post(text, send)
        if send and text.startswith("[kimeru 詳細 #"):
            self.timeline.append("D:" + text.split("#", 1)[1].split("]", 1)[0])
        return got


class ScriptWriter:
    """Stands in for a writer: the scenario's fixed texts, never a model. `修正 N` returns the scenario's rewrite."""
    NAME = "script"

    def __init__(self, scenario):
        self.sc = scenario

    def draft(self, res, event, instruction=None):
        texts = dict(self.sc["draft"], **(self.sc.get("redraft", {}) if instruction else {}))
        out = {"memo": dict(self.sc["memo"])}
        for key, action in writer_mod.targets(res):
            text = texts.get(action["type"])
            if text:
                out[key] = text.format(title=action.get("title", ""))
        return out


MARKS = "④⑤⑥⑦⑧⑨"


def _w(line=""):
    print(line, flush=True)


def _quote(text, mark="      | "):
    for line in str(text).splitlines():
        _w(mark + line)


NODE = {"critical_incident": "重大障害の規則", "ack_only": "お礼・了解だけの規則", "intent": "依頼の種類",
        "plan_decision": "進め方の型"}
CHOICE = {"decision": "判断の依頼", "requirements": "要件の検討", "status": "状況の確認", "incident": "障害",
          "fyi": "共有", "none": "該当なし"}


def _path_line(r):
    parts = []
    for s in r["path"]:
        a = s.get("answer") or {}
        name = NODE.get(s["node"], s["node"])
        if "matched" in a:
            parts.append(f"{name}: {'該当' if s['edge'] == 'yes' else '該当なし'}")
        elif "playbook" in a:
            parts.append(f"{name}: {a['playbook']}")
        elif "choice" in a:
            parts.append(f"{name}: {CHOICE.get(a['choice'], a['choice'])}")
        else:
            parts.append(f"{name}: {s['edge']}")
    return " → ".join(parts)


def _not_ours(out):
    """What in `out` the try run did not write (a folder without the mark that is not empty): the run stops before touching it."""
    out = Path(out)
    if not out.exists() or (out / demo.MARK).exists():
        return []
    return sorted(p.name for p in out.iterdir())


def is_try_folder(folder):
    """True for a folder the try run wrote (it carries TRY_MARK)."""
    try:
        return (Path(folder) / TRY_MARK).is_file()
    except OSError:
        return False


def _norm(path):
    """One spelling per place, as far as it can be known without the place existing: the extended-length prefix (two
    backslashes, a question mark, a backslash) dropped and the path made absolute (on Windows GetFullPathName also drops
    trailing dots and spaces), case folded where the file system ignores case."""
    text = str(path)
    if text.startswith("\\\\?\\"):
        text = text[4:]
    return os.path.normcase(os.path.abspath(text))


def _same(a, b):
    """The same place: by the file system when both exist (an alias such as the administrative share of the local drive, or a
    junction, is the same folder), else by the normalised spelling."""
    try:
        if os.path.exists(a) and os.path.exists(b):
            return os.path.samefile(a, b)
    except OSError:
        pass
    return _norm(a) == _norm(b)


def _out_problem(out):
    """Why `out` cannot hold the try run's records, or None. The folders that hold (or will hold) real records are refused even
    when they are empty: the try run's approved case #1 would otherwise sit among them (`out` of run / daily, this folder, the
    state folder and anything inside it)."""
    out = Path(out)
    if out.exists() and not out.is_dir():
        return f"{out} はフォルダではありません"
    normal = [(Path("out"), "run・daily の既定の記録フォルダ"), (Path.cwd(), "今のフォルダ")]
    state = config.state_dir()
    for place, name in normal:
        if _same(out, place):
            return f"{out} は {name}です。体験の記録が本物の記録に混ざらないよう、ここには書きません" if name[0].isascii() else \
                f"{out} は{name}です。体験の記録が本物の記録に混ざらないよう、ここには書きません"
    ancestors = [Path(_norm(out)), *Path(_norm(out)).parents]
    if any(_same(folder, state) for folder in ancestors):
        return f"{out} は kimeru の状態フォルダ（またはその中）です。体験の記録が本物の記録に混ざらないよう、ここには書きません"
    other = _not_ours(out)
    if other:
        return f"{out} には体験以外のファイルがあります（{', '.join(other[:5])}）。触らずに止めました"
    try:   # a name Windows cannot make (a:b, a|b, a trailing space) or a path through a file: one line here, not a traceback later
        created = not out.exists()
        out.mkdir(parents=True, exist_ok=True)
        probe = out / TRY_MARK
        if not probe.exists():
            probe.write_text("", encoding="utf-8")
            probe.unlink()
        if created and not any(out.iterdir()):
            out.rmdir()
    except OSError as e:
        return f"{out} にフォルダを作れません（{e.strerror or type(e).__name__}）"
    return None


def run(out, scenario=SCENARIO, attempts=None):
    """The case, step by step. Returns the mark of the next step (the caller writes the report)."""
    from .cli import DEFAULT_PLAYBOOKS, process   # cli imports this module lazily: no cycle
    from . import graph, plan
    sc = json.loads(Path(scenario).read_text(encoding="utf-8"))
    pbs = plan.load_playbooks(DEFAULT_PLAYBOOKS)
    gs = graph.load_dir(HERE / "graphs", pbs)
    demo.prepare(out)
    (Path(out) / TRY_MARK).write_text("kimeru try output: fictional records. Other kimeru commands refuse to write here.\n",
                                      encoding="utf-8")
    teams, writer = TryTeams(), ScriptWriter(sc)
    _w(f"=== kimeru を試す: {sc['title']}（体験モード）")
    _w("    判断はキーワード一致の簡易ルールです。返信案・足りない情報・選択肢・次の一手と、あなたの返信は台本の固定文です。"
       "AI モデルの推論ではありません。")
    _w("    AI モデル・外部 CLI・Teams・ADO には接続せず、外へは何も送りません。")
    with sealed(attempts):
        _w("")
        _w("① 相談が届く（Teams の 1 対 1 チャット・模擬）")
        _quote(f"{sc['chat']['title']}: {sc['chat']['preview']}")
        evs = pull.teams_events([sc["chat"]], {"sigs": {"_": {"p": "_", "t": "", "n": 0}}})
        results = [r for p in evs for r in process(p, gs, StubBackend(), out, pbs, writer=writer, measurement_source="demo")]
        r = results[0]
        memo = r.get("memo") or {}
        _w("")
        _w("② kimeru が整理する")
        _w(f"    判断の経路: {_path_line(r)}")
        _w("    足りない情報:")
        for m in memo.get("missing", []):
            _w(f"      ・{m}")
        if r.get("plan"):
            _w(f"    進め方の案: {r['plan']['title']}")
            for i, st in enumerate(r["plan"]["steps"], 1):
                _w(f"      {i}. {st['title']}")
        _w("    人が決めること（選択肢）:")
        for o in memo.get("options", []):
            _w(f"      ・{o}")
        reply = next((a for a in r["actions"] if a["type"] == "teams.reply"), {})
        _w(f"    返信案: {reply.get('text', '')}")
        if reply.get("unverified"):
            _w(f"      ⚠ 元の相談に無い日付・数値: {'、'.join(reply['unverified'])}（送る前に確かめる印）")
        _w("    → 返信案があるので、あなたの確認を待ちます（kimeru は相手へ送りません）")

        _w("")
        _w("③ 確認を求める（自分とのチャットへの投稿・模擬）")
        notify.notify(out, teams, send=True)
        _quote(teams.posts[-1])
        for step, rep in zip(MARKS, sc["replies"]):
            _w("")
            _w(f"{step} あなたの返信（台本が代わりに入力）: 「{rep['text']}」（{rep['label']}）")
            before = len(teams.posts)
            teams.timeline.append("R:" + rep["text"])
            changes = notify.collect(out, teams, writer=writer, real=False, send=True)
            if any(c["status"] == "redrafted" for c in changes):
                notify.notify(out, teams, send=True)
            for text in teams.posts[before:]:
                _quote(text)
            for c in changes:
                if c["status"] == "approved":
                    _w("    #1 を承認済みとして記録しました。承認した計画は記録だけで、相手への返信・起票はしていません")
    return MARKS[len(sc["replies"])]


def main(argv):
    """The `try` command. argv: the command line after `python -m kimeru` (global options may come before `try`)."""
    out, rest = DEFAULT_OUT, list(argv)
    rest.remove("try")
    while rest:
        a = rest.pop(0)
        if a == "--out" or a.startswith("--out="):
            value = a.split("=", 1)[1] if "=" in a else (rest.pop(0) if rest else "")
            if not value.strip():
                print("kimeru: --out の後にフォルダを書いてください（例: python -m kimeru try --out out/try2）", file=sys.stderr)
                return 2
            out = Path(value)
        elif a in ("-h", "--help"):
            print(USAGE)
            print("体験モード: 架空の相談 1 件を、届く → 整理 → 修正 → 承認 → 記録まで、外部に接続せずに見せます。")
            return 0
        else:
            print(f"kimeru: try では {a} を使えません（体験モードは簡易ルールと台本だけで動きます）\n{USAGE}", file=sys.stderr)
            return 2
    if not (HERE / "graphs").is_dir() or not SCENARIO.is_file():   # e.g. installed with pip: the data folders are not in the package
        print("kimeru: 判断グラフか体験の台本が見つかりません。clone したフォルダ（または展開した ZIP）で python -m kimeru try を"
              "実行してください（pip での導入には対応していません）", file=sys.stderr)
        return 2
    problem = _out_problem(out)
    if problem:
        print(f"kimeru: {problem}。体験は既定の {DEFAULT_OUT} か、空のフォルダで実行してください", file=sys.stderr)
        return 2
    saved_env = dict(os.environ)
    saved_config = (dict(config.ARGS), dict(config._FILLED), dict(config.SOURCES), list(config.WARNINGS))
    attempts = []
    try:
        with tempfile.TemporaryDirectory(prefix="kimeru-try-state-") as state:
            dropped = isolate(state)
            _w("    この PC の kimeru の設定ファイル" + (f"と、環境変数の設定・鍵（{len(dropped)} 件）" if dropped else "") +
               "は、この体験では使いません（終わると元に戻します）")
            try:
                last = run(out, attempts=attempts)
            except Sealed as e:   # a regression: say it plainly instead of a traceback
                print(f"kimeru: 体験を止めました: {e}", file=sys.stderr)
                return 1
            except OSError as e:   # the folder went away or filled up while the run wrote to it
                print(f"kimeru: {out} に書けませんでした（{e.strerror or type(e).__name__}）", file=sys.stderr)
                return 1
            if attempts:   # an attempt that something caught and went on from: still a failure of the try run
                print(f"kimeru: 体験の途中で外部への接続・起動が試みられました（{', '.join(attempts)}）。止めて記録しました", file=sys.stderr)
                return 1
            from . import graph, plan
            from .cli import DEFAULT_PLAYBOOKS
            gs = graph.load_dir(HERE / "graphs", plan.load_playbooks(DEFAULT_PLAYBOOKS))
            page = Path(out) / "report.html"
            page.write_text(report.build(out, gs, TITLE, mode="try"), encoding="utf-8")
    finally:   # the process may go on (a test, a library use): the environment and the settings are put back as they were
        os.environ.clear()
        os.environ.update(saved_env)
        for live, kept in zip((config.ARGS, config._FILLED, config.SOURCES, config.WARNINGS), saved_config):
            live.clear()
            live.update(kept) if isinstance(live, dict) else live.extend(kept)
    _w("")
    _w(f"{last} 判断の記録を見る")
    _w(f"    レポート: {page}  ← ブラウザで開くと、依頼・根拠・提案・あなたの確認・記録を 1 画面で追えます")
    _w(f"    記録: {Path(out) / 'decisions.jsonl'}（判断の経路）、{Path(out) / 'approvals.log.jsonl'}（修正と承認）")
    _w("    AI モデルを接続すると、種類の判断は判断モデルが、返信案と足りない情報は文章生成 AI（writer）が、相談の文面から作ります。")
    return 0
