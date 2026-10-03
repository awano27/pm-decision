"""Judge backends. All return Jev-shaped answers:
noul -> {"noul": p}; choice -> {"choice", "confidence", "probabilities"};
score -> {"score", "confidence", "probabilities"}.
"""
import http.client
import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config


API_KEYS = ("type", "instructions", "criteria")


class BackendAnswerError(ValueError):
    """A judge response is missing or malformed for its requested question."""


def _probability_map(question, value, qid):
    if value is None:  # probability maps are optional in supported judge responses
        return
    if not isinstance(value, dict):
        raise BackendAnswerError(f"{qid}: probabilities must be an object")
    qtype = question.get("type")
    criteria = question.get("criteria") or {}
    for key, probability in value.items():
        if qtype == "choice":
            valid_key = key in criteria
        elif qtype == "noul":
            valid_key = True  # the scalar noul answer is authoritative; maps are unused
        else:
            valid_key = str(key).isdigit() and 0 <= int(key) < len(criteria)
        if not valid_key:
            raise BackendAnswerError(f"{qid}: invalid probability key {key!r}")
        if not _probability(probability):
            raise BackendAnswerError(f"{qid}: probability for {key!r} must be between 0 and 1")


def _probability(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and 0 <= value <= 1)


def validate_answer(question, answer, qid="answer"):
    """Validate required typed values while accepting optional/partial probability maps."""
    if not isinstance(answer, dict):
        raise BackendAnswerError(f"{qid}: answer must be an object")
    qtype = question.get("type")
    if answer.get("type", qtype) != qtype:
        raise BackendAnswerError(f"{qid}: answer type does not match {qtype!r}")
    if "confidence" in answer and not _probability(answer["confidence"]):
        raise BackendAnswerError(f"{qid}: confidence must be between 0 and 1")
    if qtype == "noul":
        if "noul" not in answer or not _probability(answer["noul"]):
            raise BackendAnswerError(f"{qid}: noul must be a number between 0 and 1")
    elif qtype == "choice":
        criteria = question.get("criteria") or {}
        if answer.get("choice") not in criteria:
            raise BackendAnswerError(f"{qid}: choice must be one of {sorted(criteria)}")
        if "confidence" not in answer:
            raise BackendAnswerError(f"{qid}: confidence is required for choice answers")
    elif qtype == "score":
        # Scores are continuous against exclusive routing bands: a three-level
        # scale accepts 2.6, while 3.0 is outside its [0, 3) range.
        score, limit = answer.get("score"), len(question.get("criteria") or [])
        if (isinstance(score, bool) or not isinstance(score, (int, float))
                or not math.isfinite(score) or not 0 <= score < limit):
            raise BackendAnswerError(f"{qid}: score must be at least 0 and below {limit}")
        if "confidence" not in answer:
            raise BackendAnswerError(f"{qid}: confidence is required for score answers")
    else:
        raise BackendAnswerError(f"{qid}: unsupported question type {qtype!r}")
    _probability_map(question, answer.get("probabilities"), qid)
    return answer


def validate_answers(questions, answers):
    if not isinstance(answers, dict):
        raise BackendAnswerError("answers must be an object")
    missing = set(questions) - set(answers)
    if missing:
        raise BackendAnswerError(f"missing answers: {', '.join(sorted(missing))}")
    for qid, question in questions.items():
        validate_answer(question, answers[qid], qid)
    return answers


def jev_questions(questions):
    """Drop kimeru-only keys (e.g. `hints`) before sending to Jev."""
    return {qid: {k: v for k, v in q.items() if k in API_KEYS} for qid, q in questions.items()}


class JevBackend:
    """TypeSafe System One. Key comes from the environment only (never a file)."""

    API = "https://api.typesafe.ai/v1"
    NAME = "Jev"
    TIMEOUT = 60
    PROFILE = "jev"

    def __init__(self, model="jev-latest", key_env="TYPESAFE_API_KEY", retries=4, api=None, key_required=True):
        self._key = os.environ.get(key_env) or ""
        if key_required and not self._key:
            raise SystemExit(f"{key_env} is not set")
        self.api = (api or self.API).rstrip("/")
        self.model, self.retries = model, retries
        from .profiles import PROFILES
        self.profile = PROFILES[self.PROFILE]   # threshold scaling used by graph.route / plan.build

    def ask(self, state, questions):
        body = json.dumps({"state": state, "model": self.model, "questions": jev_questions(questions)}).encode("utf-8")
        delay = 1.0
        for attempt in range(self.retries):
            headers = {"Content-Type": "application/json", "User-Agent": "kimeru/0.1"}
            if self._key:
                headers["Authorization"] = "Bearer " + self._key
            req = urllib.request.Request(self.api + "/systemone", data=body, method="POST", headers=headers)
            try:
                with _urlopen(req, self.TIMEOUT) as r:
                    response = json.loads(r.read().decode("utf-8"))
                    if not isinstance(response, dict):
                        raise BackendAnswerError("response must be an object")
                    return validate_answers(questions, response.get("answers"))
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504, 529) and attempt < self.retries - 1:
                    time.sleep(delay)
                    delay *= 2
                    continue
                msg = e.read().decode("utf-8", "replace")[:300]
                if self._key:
                    msg = msg.replace(self._key, "<key>")
                err = BackendUnavailable if e.code in (429, 500, 502, 503, 504, 529) else RuntimeError
                raise err(f"{self.NAME} HTTP {e.code}: {msg}") from None
            except urllib.error.URLError as e:
                raise BackendUnavailable(f"{self.NAME} not reachable at {self.api} ({e.reason})") from None
            except TimeoutError:
                raise BackendUnavailable(f"{self.NAME} timed out after {self.TIMEOUT}s at {self.api}") from None
            except (OSError, http.client.HTTPException) as e:   # reset / half-closed / incomplete read
                if attempt < self.retries - 1:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise BackendUnavailable(f"{self.NAME} connection failed at {self.api} ({type(e).__name__})") from None


LOOPBACK = ("127.0.0.1", "localhost", "::1")
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _urlopen(req, timeout):
    """A judge on this PC (Kev) is called directly, never through HTTP(S)_PROXY or the Windows
    proxy setting: a proxy cannot reach 127.0.0.1 and the event text would leave the PC."""
    host = urllib.parse.urlparse(req.full_url).hostname or ""
    if host in LOOPBACK:
        return _DIRECT.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def judge_name(backend):
    """Which judge is behind a backend, seen through the wrappers that time or record it (Meter, Timed, Recording)."""
    seen = 0
    while hasattr(backend, "inner") and seen < 5:
        backend, seen = backend.inner, seen + 1
    return getattr(backend, "NAME", type(backend).__name__)


def is_jev(backend):
    """Jev's speed and accuracy numbers must not be published (TypeSafe's terms): nothing that shows them is printed."""
    return judge_name(backend) == JevBackend.NAME


class BackendUnavailable(RuntimeError):
    """The judge could not be reached or is overloaded (429/5xx/timeout after retries).
    Inbox files stay in place and are retried on the next cycle."""


def _local_url(key, remote_ok_key):
    """The address of a judge that runs on this PC. An empty setting means the default (never the cloud address); an
    address that is not this PC is refused unless it was allowed on purpose, because the event text would leave the PC."""
    problem = config.url_problem(key, remote_ok_key)
    if problem:
        raise SystemExit("kimeru: " + problem)
    return config.value(key)


class KevBackend(JevBackend):
    """Kev (jaredpalmer/kev): a Jev-compatible model served on this PC by `kev.serve`.
    Same API as Jev; nothing leaves the machine. URL from KIMERU_KEV_URL (empty = the default
    http://127.0.0.1:8009/v1; an address that is not this PC needs KIMERU_KEV_URL_REMOTE_OK=1); key only if the server sets KEV_API_KEY."""

    NAME = "Kev"
    PROFILE = "kev"
    TIMEOUT = 300  # CPU inference is slow on the first request

    def __init__(self, model="kev", retries=2):
        super().__init__(model=model, key_env="KEV_API_KEY", retries=retries,
                         api=_local_url("kev_url", "kev_url_remote_ok"), key_required=False)


class ClmBackend(JevBackend):
    """CLM (Contrastive-LM/CLM): a TypeSafe-compatible contrastive model served locally by
    `clm-serve` over a Qwen3-8B embedding server. URL from KIMERU_CLM_URL
    (default http://127.0.0.1:8700/v1)."""

    NAME = "CLM"
    PROFILE = "clm"
    TIMEOUT = 900  # on CPU every question embeds state+question with an 8B encoder

    def __init__(self, model="clm-latest", retries=2):
        super().__init__(model=model, key_env="CLM_API_KEY", retries=retries,
                         api=_local_url("clm_url", "clm_url_remote_ok"), key_required=False)


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
