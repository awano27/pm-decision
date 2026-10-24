"""Quality of drafted text (not just "did it run"): does the draft add value over the template and read like a PM wrote it?

  python eval/draft_quality.py --backend stub --writer copilot [--per-kind 3] [--out q.jsonl] [--show]
  python eval/draft_quality.py --writer cmd --fixtures fixtures_context.jsonl --per-kind 12 \
      --settings writer_precedent=0 --settings writer_precedent=1     # the same fixtures once per group of settings

Per draft, deterministic checks (no LLM judge, so runs are comparable before / after a prompt change):
  japanese    the text is written in Japanese (identifiers and product names may stay in English)
  english     no run of English words (a translated-nowhere sentence such as "error rate for 10 minutes")
  copy        the draft is not the template again (similarity < 0.8); only for types that have a template
  awkward     none of the stiff / wrong phrases seen in real drafts ("障害が発火", "予定してください" ...)
  claim       no authority or completion that the material does not contain ("経営判断", "完了しました" ...)
  specific    it uses at least one concrete word from the material (a name, id, number, product)
  invented    dates / numbers / people not in the material (the writer's own check)
Also per draft (K01): tokens_total / tokens_unverified / grounded_rate (the share of its dates, numbers and people that the
material has), and thread_given / precedent_given / style_given / today_given (whether the request carried that block).
A draft is "good" when all pass. Reading the drafts (--show) is still the real review.
The coefficients `kimeru calibrate --apply` wrote to the state folder are not read, unless you pass --user-thresholds.
"""
import argparse
import contextlib
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from kimeru import config, profiles, graph, plan, writer  # noqa: E402
from kimeru.backends import JevBackend, KevBackend, StubBackend  # noqa: E402

TOKEN = re.compile(r"[一-龠]{2,}|[ァ-ヶー]{3,}|[A-Za-z][A-Za-z0-9_\-]{2,}|\d+(?:\.\d+)?[%件日人分時]?")


def _tokens(text):
    return {t for t in TOKEN.findall(text or "")}


def has_tradeoff(option):
    """An option names both what is good and what is worrying: with the words (利点 / 懸念 / ただし / 一方 ...) or as
    「案：良い点／気になる点」 (the shape the prompt asks for)."""
    if re.search(r"利点|懸念|リスク|メリット|デメリット|ただし|一方|反面|が、|ものの", option):
        return True
    m = re.search(r"[：:](.+?)[／/](.+)", option)
    return bool(m and len(m.group(1).strip()) >= 4 and len(m.group(2).strip()) >= 4)


def check(item, material_text):
    text, tpl = item["text"], item.get("template") or ""
    flags = writer.quality_flags(text, tpl, material_text)
    if not (_tokens(text) & _tokens(material_text)):
        flags.append("specific")
    if item.get("unverified"):
        flags.append("invented")
    return flags


def grounding(text, material_text):
    """How much of a draft's dates, numbers and people the material has. Distinct tokens, so that a token written twice counts once
    on both sides; grounded_rate is None for a draft with none (nothing to be wrong about)."""
    total = len(set(writer.TOKENS.findall(text or "")))
    unv = len(writer.unverified(text, material_text))
    return {"tokens_total": total, "tokens_unverified": unv, "grounded_rate": None if not total else 1 - unv / total}


GIVEN = {   # which block of the request each *_given field looks for
    "thread_given": lambda p: "直前のやり取り" in p,
    "precedent_given": lambda p: "前に PM が決めたこと（データ" in p,   # the head's start: a variant head (K02) still counts
    "style_given": lambda p: writer.STYLE_HEAD in p,
    "today_given": lambda p: re.search(r"(?m)^今日: ", p) is not None,
}


def given_of(prompt):
    return {k: bool(f(prompt or "")) for k, f in GIVEN.items()}


def parse_settings(specs):
    """[(label, {key: value})] from the --settings arguments ("a=b,c=d", repeatable); no argument is one group of the defaults."""
    if not specs:
        return [("(既定)", {})]
    out = []
    for spec in specs:
        pairs = {}
        for part in spec.split(","):
            key, sep, val = part.partition("=")
            key = key.strip()
            if not sep or key not in config.SETTINGS:
                raise SystemExit(f"--settings {spec!r}: {part!r} is not key=value of a known setting")
            pairs[key] = val.strip()
        out.append((spec, pairs))
    return out


@contextlib.contextmanager
def settings_env(pairs):
    """The settings in the environment for the length of one run only (what config.value reads), put back as they were."""
    saved = {}
    try:
        for key, val in pairs.items():
            env = config.SETTINGS[key][0]
            saved[env] = os.environ.get(env)
            os.environ[env] = val
        yield
    finally:
        for env, old in saved.items():
            if old is None:
                os.environ.pop(env, None)
            else:
                os.environ[env] = old


def capturing(w):
    """The same writer, but it keeps the requests it is sent (the CLI writers all ask through ask_text)."""
    prompts = []
    if not hasattr(w, "ask_text"):
        return w, prompts

    class Capture(type(w)):
        def ask_text(self, prompt):
            prompts.append(prompt)
            return super().ask_text(prompt)

    c = Capture.__new__(Capture)
    c.__dict__.update(w.__dict__)
    return c, prompts


def run_group(a, label, pairs, be, graphs, pbs, picked):
    """One pass over the fixtures with the settings of one group in the environment. Returns (by_type, flag_count, secs, rows)."""
    rows, by_type, flag_count, secs = [], {}, {}, []   # by_type[t] = [good, all, [grounded_rate...], invented]
    with settings_env(pairs):
        for x in picked:
            ev = x["event"]
            ctx = x.get("context") if isinstance(x.get("context"), dict) else {}
            if ctx.get("earlier"):
                ev = dict(ev, thread=ctx["earlier"])   # the chat's earlier messages, as the opened chat gives them (judges never see "thread")
            res = graph.run(graphs[ev["kind"]], ev, be, playbooks=pbs)
            if not writer.targets(res):
                continue
            material = writer._material(res, ev)
            w, prompts = capturing(writer.get_writer(a.writer))
            t0 = time.time()
            drafted = writer.apply(res, ev, w, precedent=x.get("precedent"))
            secs.append(time.time() - t0)
            given = given_of(prompts[0] if prompts else "")
            if not prompts and not getattr(w, "PROMPT_ONLY", False):
                print(f"[{x['id']}] 依頼文を捕まえられなかった（writer {a.writer} に ask_text が無い）: *_given は全部 False", flush=True)
            for d in drafted:
                if not d.get("drafted_by"):
                    by_type.setdefault(d["type"], [0, 0, [], 0])[1] += 1
                    flag_count["not_drafted"] = flag_count.get("not_drafted", 0) + 1
                    print(f"[{x['id']}] 下書きなし: {d['type']}  writer_error={str(res.get('writer_error') or '')[:300]}  warning={d.get('writer_warning', '')}", flush=True)
                    continue
                item = {"type": d["type"], "title": d.get("title"), "text": d[writer.FIELD[d["type"]]],
                        "template": d.get("template_text"), "unverified": d.get("unverified", [])}
                item["flags"] = check(item, material)
                item["id"] = x["id"]
                item["settings"] = label
                item.update(grounding(item["text"], material))
                item.update(given)
                if "expect_facts" in x:   # a stricter answer key than the material: only the facts the reply may carry
                    item["invented_vs_expect"] = writer.unverified(item["text"], " ".join(x["expect_facts"]))
                rows.append(item)
                st = by_type.setdefault(item["type"], [0, 0, [], 0])
                st[0] += not item["flags"]
                st[1] += 1
                if item["grounded_rate"] is not None:
                    st[2].append(item["grounded_rate"])
                st[3] += "invented" in item["flags"]
                for f in item["flags"]:
                    flag_count[f] = flag_count.get(f, 0) + 1
                if a.show:
                    print(f"[{x['id']}] {item['type']}{' 「' + item['title'] + '」' if item['title'] else ''}"
                          f"{'  ⚑ ' + ','.join(item['flags']) if item['flags'] else ''}")
                    print("  定型:", (item["template"] or "-").replace("\n", " / ")[:120])
                    print("  下書:", item["text"].replace("\n", " / "))
            if a.show and res.get("memo"):
                m = res["memo"]
                print(f"[{x['id']}] メモ: 要点={m.get('summary', '')} / 不足={m.get('missing', [])} / 選択肢={m.get('options', [])} / 次={m.get('next', '')}"
                      f"{' / 聞き返し=' + m['ask_back'] if m.get('ask_back') else ''}")
            if res.get("memo"):
                mm = res["memo"]
                opts = [str(o) for o in mm.get("options", [])]
                memo_flags = []
                if len(opts) == 1:
                    memo_flags.append("options_few")
                if opts and not all(has_tradeoff(o) for o in opts):
                    memo_flags.append("options_no_tradeoff")
                if not mm.get("next"):
                    memo_flags.append("next_missing")
                st = by_type.setdefault("memo", [0, 0, [], 0])
                st[0] += not memo_flags
                st[1] += 1
                for f in memo_flags:
                    flag_count[f] = flag_count.get(f, 0) + 1
            print(f"{x['id']:12} {res['node']:18} {secs[-1]:5.1f}s", flush=True)
    return by_type, flag_count, secs, rows


def print_group(label, by_type, flag_count, secs, grouped):
    total = sum(v[1] for v in by_type.values())
    good = sum(v[0] for v in by_type.values())
    print(f"\n== 種別ごと（良い/全体）{'  設定: ' + label if grouped else ''}")
    for t, v in sorted(by_type.items()):
        gr = f"  grounded={sum(v[2]) / len(v[2]):.2f}" if v[2] else ""
        print(f"  {t:12} {v[0]}/{v[1]}{gr}  invented={v[3]}")
    print(f"== 合計 good={good}/{total} ({good / total:.0%})" if total else "== no drafts")
    print("== 指摘の内訳:", ", ".join(f"{k}={v}" for k, v in sorted(flag_count.items())) or "なし")
    if secs:
        secs = sorted(secs)
        print(f"== 時間 median={secs[len(secs) // 2]:.1f}s max={secs[-1]:.1f}s")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["stub", "jev", "kev"], default="stub")
    ap.add_argument("--writer", default="copilot")
    ap.add_argument("--per-kind", type=int, default=3)
    ap.add_argument("--skip", type=int, default=0, help="skip the first N events of each kind (events the prompt was tuned on)")
    ap.add_argument("--kinds", default="teams.chat,monitor.alert,ado.workitem.created,meeting.item")
    ap.add_argument("--fixtures", default="fixtures.jsonl")
    ap.add_argument("--out")
    ap.add_argument("--settings", action="append", metavar="a=b,c=d",
                    help="run the same fixtures once per group of settings (repeatable); the settings are in the environment during that run only")
    ap.add_argument("--show", action="store_true", help="print every draft next to its template")
    ap.add_argument("--user-thresholds", action="store_true",
                    help="also read the coefficients `kimeru calibrate --apply` wrote (default: not read)")
    a = ap.parse_args()
    be = {"kev": KevBackend, "jev": JevBackend}.get(a.backend, StubBackend)()
    groups = parse_settings(a.settings)
    pbs = plan.load_playbooks(ROOT / "playbooks")
    graphs = {k: v[0] for k, v in graph.load_dir(ROOT / "graphs", pbs).items()}
    kinds = a.kinds.split(",")
    fx = [json.loads(l) for l in (ROOT / "eval" / a.fixtures).read_text(encoding="utf-8").splitlines() if l.strip()]
    picked = [x for k in kinds for x in [f for f in fx if f["event"]["kind"] == k][a.skip: a.skip + a.per_kind]]
    all_rows, summary = [], []
    for label, pairs in groups:
        if len(groups) > 1:
            print(f"\n######## 設定: {label}")
        by_type, flag_count, secs, rows = run_group(a, label, pairs, be, graphs, pbs, picked)
        all_rows += rows
        print_group(label, by_type, flag_count, secs, len(groups) > 1)
        summary.append((label, by_type, secs))
    if a.out:
        Path(a.out).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in all_rows), encoding="utf-8")
    if len(groups) > 1:
        print("\n== 設定の組 x 種別（良い/全体, grounded_rate の平均, invented の件数, writer の秒の中央値）")
        for label, by_type, secs in summary:
            med = f"{sorted(secs)[len(secs) // 2]:.1f}s" if secs else "-"
            for t, v in sorted(by_type.items()):
                gr = f"{sum(v[2]) / len(v[2]):.2f}" if v[2] else "-"
                print(f"  {label:34} {t:12} {v[0]}/{v[1]}  grounded={gr}  invented={v[3]}  {med}")


if __name__ == "__main__":
    with profiles.ignoring_overrides("--user-thresholds" not in sys.argv):   # a number printed here must not depend on what one PC has tuned
        main()
