"""R35 (setting split_asks=1): a chat message that asks for several things is judged request by request.

"進捗どうですか？あとリリース日を決めてください" reads as one `status` question, which hides the request for a decision. With
split_asks=1 the requests are the sentences that carry a request mark (? ください お願いします いただけますか でしょうか 決め),
except a thanks or a decision already made (よろしくお願いします, 決めました). At most MAX requests are taken, in order.

Only the text the judge already sees is split (the state's `text`, cut at judge_text_max), so no text beyond that leaves the
PC. graph.run asks the intent question once for the message and once for each request (graph._split_intent)."""
import re

NODE = "intent"   # the judge of graphs/teams_chat.json whose question is asked once per request
MAX = 3   # requests judged in one message; the later ones are not judged
SPLIT = re.compile(r"(?<=[.!?。！？；;\r\n])")   # the sentence ends of the match rules (graph._match)
MARK = re.compile(r"[?？]|ください|お願いします|いただけますか|でしょうか|決め")
NOT_A_REQUEST = ("よろしくお願いします", "決めました")   # a closing, and a decision already made: the marks in them do not count


def sentences(text):
    return [s.strip() for s in SPLIT.split(str(text or "")) if s.strip()]


def find(text):
    """The requests of `text` (the sentences, as written), in order, at most MAX. One request or none is returned as it is:
    the caller judges a message with fewer than two requests as before."""
    found = []
    for s in sentences(text):
        rest = s
        for closing in NOT_A_REQUEST:
            rest = rest.replace(closing, "")
        if MARK.search(rest):
            found.append(s)
            if len(found) == MAX:
                break
    return found
