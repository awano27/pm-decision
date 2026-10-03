"""Local notification onboarding with a human-confirmed device receipt challenge."""
import argparse
import hashlib
import hmac
import json
import math
import secrets
import time
from pathlib import Path

from . import fsutil, push

CHALLENGE_TTL = 15 * 60
_FILE = "onboarding.json"


def _path(out):
    return Path(out) / _FILE


def _read(out):
    data = fsutil.read_json(_path(out), {"challenges": []})
    return data if isinstance(data, dict) and isinstance(data.get("challenges"), list) else {"challenges": []}


def _write(out, data):
    fsutil.write_atomic(_path(out), json.dumps(data, ensure_ascii=False))


def dispatch(argv, out, print_fn=print, now=None):
    """Run onboarding subcommands. Returns 0 on success, 1 for operational failures, 2 for usage errors."""
    parser = argparse.ArgumentParser(prog="onboarding")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    test = sub.add_parser("notification-test")
    test.add_argument("--send", action="store_true")
    confirm = sub.add_parser("confirm")
    confirm.add_argument("token")
    try:
        args = parser.parse_args(list(argv))
    except SystemExit as e:
        return int(e.code)
    now = time.time() if now is None else now
    if args.command == "status":
        routes = push.enabled_routes()
        print_fn("通知経路: " + (", ".join(routes) if routes else "未設定"))
        challenges = _read(out)["challenges"]
        accepted = sum(bool(c.get("sender_accepted")) for c in challenges)
        receipts = sum(bool(c.get("receipt_confirmed")) for c in challenges)
        print_fn(f"送信受理 {accepted} 件 / challenge受信を本人が確認した件数 {receipts} 件（各確認は少なくとも1経路）")
        return 0
    if args.command == "notification-test":
        if not args.send:
            print_fn("dry run: 送信しません。実送信には --send が必要です")
            return 0
        routes = push.enabled_routes()
        if not routes:
            print_fn("失敗: 通知経路が設定されていません")
            return 1
        token = secrets.token_urlsafe(9)
        with fsutil.exclusive(Path(out) / "onboarding.lock") as locked:
            if not locked:
                print_fn("失敗: onboarding の記録が使用中です")
                return 1
            results = push.send_challenge(token)
            rec = {"token_hash": hashlib.sha256(token.encode("utf-8")).hexdigest(), "created_at": now,
                   "sender_accepted": any(v == "accepted" for v in results.values()),
                   "receipt_confirmed": False, "results": results}
            data = _read(out)
            data["challenges"] = (data["challenges"] + [rec])[-100:]
            _write(out, data)
        for route, result in results.items():
            print_fn(f"{route}: {result}")
        if not rec["sender_accepted"]:
            print_fn("送信失敗: 送信経路から受理されませんでした")
            return 1
        print_fn("送信経路は受理しました。端末で受信したら challenge を `onboarding confirm TOKEN` に入力してください")
        return 0
    if args.command == "confirm":
        token_hash = hashlib.sha256(args.token.encode("utf-8")).hexdigest()
        with fsutil.exclusive(Path(out) / "onboarding.lock") as locked:
            if not locked:
                print_fn("失敗: onboarding の記録が使用中です")
                return 1
            data = _read(out)
            match = next((c for c in reversed(data["challenges"])
                          if hmac.compare_digest(str(c.get("token_hash", "")), token_hash)), None)
            if match is None:
                print_fn("失敗: challenge が不明か、すでに確認済みです")
                return 1
            try:
                elapsed = now - float(match.get("created_at"))
            except (TypeError, ValueError, OverflowError):
                elapsed = float("nan")
            if not math.isfinite(elapsed) or not 0 <= elapsed <= CHALLENGE_TTL:
                print_fn("失敗: challenge の期限が切れています")
                return 1
            if not match.get("sender_accepted"):
                print_fn("失敗: 送信経路が受理していない challenge です")
                return 1
            if match.get("receipt_confirmed"):
                print_fn("失敗: challenge はすでに確認済みです")
                return 1
            match["receipt_confirmed"] = True
            match["confirmed_at"] = now
            _write(out, data)
        print_fn("少なくとも1つの通知経路で、本人が端末受信を確認しました")
        return 0
    return 2


def main(argv=None):
    parser = argparse.ArgumentParser(prog="kimeru onboarding")
    parser.add_argument("--out", default="out")
    args, rest = parser.parse_known_args(argv)
    return dispatch(rest, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
