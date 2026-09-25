"""Judge backends. All return Jev-shaped answers:
noul -> {"noul": p}; choice -> {"choice", "confidence", "probabilities"};
score -> {"score", "confidence", "probabilities"}.
"""
import json
import os
import time
import urllib.error
import urllib.request


API_KEYS = ("type", "instructions", "criteria")


def jev_questions(questions):
    """Drop kimeru-only keys (e.g. `hints`) before sending to Jev."""
    return {qid: {k: v for k, v in q.items() if k in API_KEYS} for qid, q in questions.items()}


class JevBackend:
    """TypeSafe System One. Key comes from the environment only (never a file)."""

    API = "https://api.typesafe.ai/v1"
    NAME = "Jev"
    TIMEOUT = 60

    def __init__(self, model="jev-latest", key_env="TYPESAFE_API_KEY", retries=4, api=None, key_required=True):
        self._key = os.environ.get(key_env) or ""
        if key_required and not self._key:
            raise SystemExit(f"{key_env} is not set")
        self.api = (api or self.API).rstrip("/")
        self.model, self.retries = model, retries

    def ask(self, state, questions):
        body = json.dumps({"state": state, "model": self.model, "questions": jev_questions(questions)}).encode("utf-8")
        delay = 1.0
        for attempt in range(self.retries):
            headers = {"Content-Type": "application/json", "User-Agent": "kimeru/0.1"}
            if self._key:
                headers["Authorization"] = "Bearer " + self._key
            req = urllib.request.Request(self.api + "/systemone", data=body, method="POST", headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.TIMEOUT) as r:
                    return json.loads(r.read().decode("utf-8"))["answers"]
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504, 529) and attempt < self.retries - 1:
                    time.sleep(delay)
                    delay *= 2
                    continue
                msg = e.read().decode("utf-8", "replace")[:300]
                if self._key:
                    msg = msg.replace(self._key, "<key>")
                raise RuntimeError(f"{self.NAME} HTTP {e.code}: {msg}") from None
            except urllib.error.URLError as e:
                raise RuntimeError(f"{self.NAME} not reachable at {self.api} ({e.reason})") from None


class KevBackend(JevBackend):
    """Kev (jaredpalmer/kev): a Jev-compatible model served on this PC by `kev.serve`.
    Same API as Jev; nothing leaves the machine. URL from KIMERU_KEV_URL (default
    http://127.0.0.1:8009/v1); key only if the server sets KEV_API_KEY."""

    NAME = "Kev"
    TIMEOUT = 300  # CPU inference is slow on the first request

    def __init__(self, model="kev", retries=2):
        super().__init__(model=model, key_env="KEV_API_KEY", retries=retries,
                         api=os.environ.get("KIMERU_KEV_URL", "http://127.0.0.1:8009/v1"), key_required=False)


class StubBackend:
    """Offline keyword backend for demos and tests. Uses each question's `hints`.

    noul:   hints = [keywords]            -> 0.9 if any hit else 0.1
    choice: hints = {option: [keywords]}  -> most hits wins (ties -> first key)
    score:  hints = {level_index: [kw]}   -> highest level with a hit, else 0
    """

    def ask(self, state, questions):
        text = json.dumps(state, ensure_ascii=False).lower()
        hit = lambda kws: sum(k.lower() in text for k in kws)
        out = {}
        for qid, q in questions.items():
            h = q.get("hints") or {}
            if q["type"] == "noul":
                out[qid] = {"type": "noul", "noul": 0.9 if hit(h) else 0.1}
            elif q["type"] == "choice":
                opts = list(q["criteria"])
                scores = {o: hit(h.get(o, [])) for o in opts}
                best = max(opts, key=lambda o: scores[o])
                conf = 0.85 if scores[best] else 0.3  # no keyword hit -> low confidence -> unsure route
                probs = {o: (conf if o == best else (1 - conf) / max(1, len(opts) - 1)) for o in opts}
                out[qid] = {"type": "choice", "choice": best, "confidence": conf, "probabilities": probs}
            elif q["type"] == "score":
                n = len(q["criteria"])
                lvl = max([int(i) for i, kws in h.items() if hit(kws)] or [0])
                probs = {str(i): (0.85 if i == lvl else 0.15 / (n - 1)) for i in range(n)}
                out[qid] = {"type": "score", "score": float(lvl), "confidence": 0.85, "probabilities": probs}
        return out


class ReplayBackend:
    """Returns canned answers keyed by question id (for tests)."""

    def __init__(self, answers):
        self.answers = answers

    def ask(self, state, questions):
        return {q: self.answers[q] for q in questions}
