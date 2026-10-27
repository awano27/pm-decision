"""似た件 (K07): word-overlap search over the decided items, for the same topic raised in another chat or by another person.

Pure functions, no dependency and no file: memory.py builds the documents from records that already exist and shows the result
(setting recall=1). The score is a Dice coefficient over character 2-grams (Japanese has no spaces) plus whole Latin words
(`QA`, `checkout-api`, `ABC-123`), so a hit is explainable by the few terms the two texts share.
"""
import re
import unicodedata

# 2-grams that carry no topic (particles, polite endings, request phrases); a fixed list, at most 40
STOP_GRAMS = frozenset((
    "です", "ます", "まし", "ませ", "ので", "から", "けど", "ため", "こと", "もの", "して", "した", "しま", "てい", "てお", "ない",
    "いま", "いる", "ある", "あり", "れる", "れた", "られ", "って", "では", "には", "とい", "いた", "お願", "願い", "いし", "ださ",
    "くだ", "よろ", "ろし", "しく", "ござ", "ざい",
))

_RUN = re.compile(r"[぀-ゟァ-ヺーヽヾ一-鿿々a-z0-9]+")   # kana (not the middle dot ・), kanji and Latin/digits; anything else separates
_WORD = re.compile(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*")                   # a Latin run kept whole: QA, checkout-api, ABC-123
_LOWER = {c: c + 32 for c in range(ord("A"), ord("Z") + 1)}   # ASCII only: casefold changes the length of some letters (ß), which would shift the shown slices
WORD = "w:"                                                            # prefix of a whole-word term, so it never equals a 2-gram


def terms(text):
    """{term: shown}: the 2-grams of every kana/kanji/Latin run of `text` (NFKC, ASCII letters lowercased) and each Latin word of 2 or more
    characters once more as a whole. `shown` is the term as written (for 似た点). Stop 2-grams are left out."""
    norm = unicodedata.normalize("NFKC", str(text or ""))
    folded = norm.translate(_LOWER)
    out = {}
    for m in _RUN.finditer(folded):
        run = m.group()
        for i in range(len(run) - 1):
            g = run[i:i + 2]
            if g not in STOP_GRAMS:
                out.setdefault(g, norm[m.start() + i:m.start() + i + 2])
    for m in _WORD.finditer(folded):
        if len(m.group()) >= 2:
            out.setdefault(WORD + m.group(), norm[m.start():m.end()])
    return out


def dice(a, b):
    """2 x common / (len(a) + len(b)) of two term collections; 0.0 when either is empty."""
    if not a or not b:
        return 0.0
    return 2 * len(set(a) & set(b)) / (len(a) + len(b))


_HIRAGANA = re.compile(r"[ぁ-ゖ]")


def _runs(grams):
    """Shared 2-grams (as written, in the query's order) chained into the longer strings they overlap into: リリ, リー, ース -> リリース."""
    runs = []
    for g in grams:
        if runs and runs[-1][-1] == g[0] and g not in runs[-1]:
            runs[-1] += g[1:]
        else:
            runs.append(g)
    return runs


def _trim(run):
    """A chained string without the hiragana at its ends: 環境が -> 環境, が止ま -> 止."""
    return re.sub(r"^[ぁ-ゖ]+|[ぁ-ゖ]+$", "", run)


def shared(query, doc, limit=2):
    """What 似た点 shows: the terms both share, as written in the query, whole words first and then the shared 2-grams chained into
    longer strings, the hiragana at their ends cut; at most `limit`. A string still with hiragana or shorter than 2 is left out (が止,
    たい: grammar, not topic), and so is one inside a word
    already named. Only the display: the score never depends on it."""
    common = [t for t in query if t in doc]
    words = [query[t] for t in common if t.startswith(WORD)]
    runs = [_trim(r) for r in _runs([query[t] for t in common if not t.startswith(WORD)])]
    runs = [r for r in runs if len(r) >= 2 and not _HIRAGANA.search(r)]
    picked = []
    for shown in words + runs:
        if any(shown.lower() in p.lower() for p in picked):
            continue
        picked.append(shown)
        if len(picked) == limit:
            break
    return picked


def rank(query, docs, minimum, limit):
    """[(score, doc)] best first: `docs` is [(sort_key, payload, terms)] and the result keeps the payloads whose Dice is at least
    `minimum`, at most `limit`; a tie goes to the larger sort_key (the newer one)."""
    scored = []
    for key, payload, doc_terms in docs:
        score = dice(query, doc_terms)
        if score >= minimum:
            scored.append((score, key, payload))
    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
    return [(s, p) for s, _, p in scored[:max(0, limit)]]
