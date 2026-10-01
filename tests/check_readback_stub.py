"""For tests/check-py-quoting.ps1 only: importing this module cuts the network and answers the ADO read-back of check.ps1 (T22) with one
stored comment, so that the REAL execute.inspect_comment runs end to end without ADO. The target is made up (fake-org / fake-project)
and the work item is 0. It reads the text file named by sys.argv[3] (the argument the program of check.ps1 takes)."""
import sys
import urllib.request

from kimeru import execute, pull


def _blocked(*a, **k):
    raise OSError("the network is cut in this check")


def _http(method, url, token, body=None, *a, **k):
    if method != "GET" or not url.startswith("https://dev.azure.com/fake-org/fake-project/_apis/wit/workItems/0/comments"):
        raise OSError("unexpected call in this check: " + str(url)[:80])
    text = open(sys.argv[3], encoding="utf-8").read()
    return {"comments": [{"text": "<div>" + execute._escaped(execute.comment_text({"text": text})) + "</div>"}]}   # wrapped in a tag, `<` escaped


urllib.request.urlopen = _blocked
pull.az_token = lambda *a, **k: "fake-token"
pull.http_json = _http
pull.ado_tenant = lambda *a, **k: "fake-tenant"
