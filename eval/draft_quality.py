"""Quality of drafted text (not just "did it run"): does the draft add value over the template and read like a PM wrote it?

  python eval/draft_quality.py --backend stub --writer copilot [--per-kind 3] [--out q.jsonl] [--show]

Per draft, deterministic checks (no LLM judge, so runs are comparable before / after a prompt change):
  japanese    the text is written in Japanese (identifiers and product names may stay in English)
  english     no run of English words (a translated-nowhere sentence such as "error rate for 10 minutes")
  copy        the draft is not the template again (similarity < 0.8); only for types that have a template
  awkward     none of the stiff / wrong phrases seen in real drafts ("障害が発火", "予定してください" ...)
  claim       no authority or completion that the material does not contain ("経営判断", "完了しました" ...)
  specific    it uses at least one concrete word from the material (a name, id, number, product)
  invented    dates / numbers / people not in the material (the writer's own check)
A draft is "good" when all pass. Reading the drafts (--show) is still the real review.
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from kimeru import graph, plan, writer  # noqa: E402
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["stub", "jev", "kev"], default="stub")
    ap.add_argument("--writer", default="copilot")
    ap.add_argument("--per-kind", type=int, default=3)
    ap.add_argument("--skip", type=int, default=0, help="skip the first N events of each kind (events the prompt was tuned on)")
    ap.add_argument("--kinds", default="teams.chat,monitor.alert,ado.workitem.created,meeting.item")
    ap.add_argument("--fixtures", default="fixtures.jsonl")
    ap.add_argument("--out")
    ap.add_argument("--show", action="store_true", help="print every draft next to its template")
    a = ap.parse_args()
    be = {"kev": KevBackend, "jev": JevBackend}.get(a.backend, StubBackend)()
    w = writer.get_writer(a.writer)
    pbs = plan.load_playbooks(ROOT / "playbooks")
    graphs = {k: v[0] for k, v in graph.load_dir(ROOT / "graphs", pbs).items()}
    kinds = a.kinds.split(",")
    fx = [json.loads(l) for l in (ROOT / "eval" / a.fixtures).read_text(encoding="utf-8").splitlines() if l.strip()]
    picked = [x for k in kinds for x in [f for f in fx if f["event"]["kind"] == k][a.skip: a.skip + a.per_kind]]
    rows, by_type, flag_count, secs = [], {}, {}, []
    for x in picked:
        ev = x["event"]
        res = graph.run(graphs[ev["kind"]], ev, be, playbooks=pbs)
        if not writer.targets(res):
            continue
        material = writer._material(res, ev)
        t0 = time.time()
        drafted = writer.apply(res, ev, w)
        secs.append(time.time() - t0)
        for d in drafted:
            if not d.get("drafted_by"):
                by_type.setdefault(d["type"], [0, 0])[1] += 1
                flag_count["not_drafted"] = flag_count.get("not_drafted", 0) + 1
                print(f"[{x['id']}] 下書きなし: {d['type']}  writer_error={str(res.get('writer_error') or '')[:300]}  warning={d.get('writer_warning', '')}", flush=True)
                continue
            item = {"type": d["type"], "title": d.get("title"), "text": d[writer.FIELD[d["type"]]],
                    "template": d.get("template_text"), "unverified": d.get("unverified", [])}
            item["flags"] = check(item, material)
            item["id"] = x["id"]
            rows.append(item)
            good = not item["flags"]
            by_type.setdefault(item["type"], [0, 0])
            by_type[item["type"]][0] += good
            by_type[item["type"]][1] += 1
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
            by_type.setdefault("memo", [0, 0])
            by_type["memo"][0] += not memo_flags
            by_type["memo"][1] += 1
            for f in memo_flags:
                flag_count[f] = flag_count.get(f, 0) + 1
        print(f"{x['id']:12} {res['node']:18} {secs[-1]:5.1f}s", flush=True)
    if a.out:
        Path(a.out).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    total = sum(n for _, n in by_type.values())
    good = sum(g for g, _ in by_type.values())
    print("\n== 種別ごと（良い/全体）")
    for t, (g, n) in sorted(by_type.items()):
        print(f"  {t:12} {g}/{n}")
    print(f"== 合計 good={good}/{total} ({good / total:.0%})" if total else "== no drafts")
    print("== 指摘の内訳:", ", ".join(f"{k}={v}" for k, v in sorted(flag_count.items())) or "なし")
    if secs:
        secs.sort()
        print(f"== 時間 median={secs[len(secs) // 2]:.1f}s max={secs[-1]:.1f}s")


if __name__ == "__main__":
    main()
