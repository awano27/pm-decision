"""Local notification onboarding with a human-confirmed device receipt challenge.

Two facts are kept apart on purpose:
  * the route ACCEPTED the send (the HTTP request / mail hand-off succeeded). That says nothing about a phone;
  * the PERSON saw the code on a device and typed it back (`confirm CODE --device phone`).
Every route gets its own code, so one confirmation names exactly one route and never stands for the others. A record written
before this (one code sent to every route) stays readable: its confirmation counts as "route unknown" and is never counted as a
route's real-device check. The real-device state is NOT_RUN until a route has a confirmation made with --device phone.
"""
import argparse
import hashlib
import hmac
import json
import math
import secrets
import time
from datetime import datetime
from pathlib import Path

from . import fsutil, push

CHALLENGE_TTL = 15 * 60
_FILE = "onboarding.json"
UNKNOWN_ROUTE = "(経路不明)"      # a record written before per-route codes: one code went to every route
DEVICES = ("phone", "pc", "other")
NOT_RUN = "NOT_RUN"


def _path(out):
    return Path(out) / _FILE


def _read(out):
    data = fsutil.read_json(_path(out), {"challenges": []})
    return data if isinstance(data, dict) and isinstance(data.get("challenges"), list) else {"challenges": []}


def _write(out, data):
    fsutil.write_atomic(_path(out), json.dumps(data, ensure_ascii=False))


def _eq(stored, wanted_hash):
    return hmac.compare_digest(str(stored or "").encode("utf-8"), wanted_hash.encode("utf-8"))


def _fmt(ts):
    try:
        return datetime.fromtimestamp(float(ts)).astimezone().isoformat(timespec="minutes")
    except (TypeError, ValueError, OverflowError, OSError):
        return "時刻不明"


def _entries(c):
    """(route name, entry) for one challenge record. A record with per-route codes keeps one entry per route; an older record
    (one code sent to every route, top-level fields) is one entry whose route is unknown: its single confirmation never
    counts for a particular route."""
    routes = c.get("routes")
    if isinstance(routes, dict) and routes:
        return [(name, e) for name, e in routes.items() if isinstance(e, dict)]
    return [(UNKNOWN_ROUTE, c)]


def summary(out):
    """What the records say, in two layers: the route accepted the send, and the person confirmed seeing the code on a device.
    `device_state` stays NOT_RUN until a route has a confirmation made with --device phone."""
    challenges = [c for c in _read(out)["challenges"] if isinstance(c, dict)]
    routes = {}
    for c in challenges:
        for name, e in _entries(c):
            r = routes.setdefault(name, {"tested": 0, "accepted": 0, "confirmed": 0, "phone_confirmed": 0, "last_at": None,
                                         "last_result": "", "last_confirmed_at": None, "last_phone_at": None})
            r["tested"] += 1
            r["accepted"] += bool(e.get("sender_accepted"))
            r["last_at"] = c.get("created_at")
            r["last_result"] = "accepted" if e.get("sender_accepted") else "failed"
            if e.get("receipt_confirmed"):
                r["confirmed"] += 1
                r["last_confirmed_at"] = e.get("confirmed_at")
                if e.get("device") == "phone" and name != UNKNOWN_ROUTE:
                    r["phone_confirmed"] += 1
                    r["last_phone_at"] = e.get("confirmed_at")
    known_phone = {n for n, r in routes.items() if r["phone_confirmed"]}
    expected = set(push.enabled_routes()) or known_phone
    if not known_phone:
        state = NOT_RUN
    else:
        state = "PARTIAL" if expected - known_phone else "CONFIRMED_ON_PHONE"
    return {"challenges": len(challenges),
            "accepted": sum(bool(c.get("sender_accepted")) for c in challenges),
            "confirmed": sum(bool(c.get("receipt_confirmed")) for c in challenges),
            "legacy_confirmed": routes.get(UNKNOWN_ROUTE, {}).get("confirmed", 0),
            "routes": routes, "device_state": state}


def _scope_lines(sm):
    unverified = sorted(r for r in push.enabled_routes() if not sm["routes"].get(r, {}).get("phone_confirmed"))
    lines = []
    if unverified:
        lines.append("スマホで本人が確認していない経路: " + ", ".join(unverified))
    if sm["legacy_confirmed"]:
        lines.append(f"旧形式の確認 {sm['legacy_confirmed']} 件は経路も端末も不明のため、どの経路の実機確認にも数えない")
    lines += ["届くまでの遅れ、ロック画面・集中モード・通知オフのときの表示は測っていない",
              "確認した端末以外（別のスマホ、タブレット）は未確認",
              "試験の文（challenge）が届いたことだけを確認する。実際の件数入りの文面での受信は別に確認が要る",
              "確認した後で URL・鍵・端末側の通知設定を変えた場合は、再確認が要る"]
    return lines


def device_report(out):
    """The record of the real-device check as text: date and time, route, result and the scope that is still unverified.
    It holds no URL, key or code (only hashes are stored)."""
    sm = summary(out)
    lines = ["実機確認の記録（onboarding device-check）"]
    if sm["device_state"] == NOT_RUN:
        lines.append(f"実機確認の状態: {NOT_RUN}（本人がスマホで challenge を見て `confirm CODE --device phone` した記録がありません）")
    else:
        lines.append(f"実機確認の状態: {sm['device_state']}（本人の確認に基づく。下の未確認の範囲を読むこと）")
    enabled = push.enabled_routes()
    lines.append("設定中の経路: " + (", ".join(enabled) if enabled else "未設定"))
    for name in sorted(sm["routes"], key=lambda n: (n == UNKNOWN_ROUTE, n)):
        r = sm["routes"][name]
        if name == UNKNOWN_ROUTE:
            lines.append(f"経路 {name}: 旧形式の試験 {r['tested']} 件 / 本人の確認 {r['confirmed']} 件（経路・端末不明。数えない）")
            continue
        send = "受理（経路が受け付けただけ。端末への到達とは別）" if r["last_result"] == "accepted" else "失敗（受理されていない）"
        phone = f"あり {_fmt(r['last_phone_at'])}" if r["phone_confirmed"] else "なし"
        lines.append(f"経路 {name}: 最新の試験 {_fmt(r['last_at'])} / 送信: {send} / 本人の確認 {r['confirmed']} 件 / スマホでの確認: {phone}")
    for name in enabled:
        if name not in sm["routes"]:
            lines.append(f"経路 {name}: 試験の記録なし（{NOT_RUN}）")
    lines.append("未確認の範囲:")
    lines += ["  - " + x for x in _scope_lines(sm)]
    return "\n".join(lines)


def dispatch(argv, out, print_fn=print, now=None):
    """Run onboarding subcommands. Returns 0 on success, 1 for operational failures, 2 for usage errors."""
    parser = argparse.ArgumentParser(prog="onboarding")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("device-check")
    test = sub.add_parser("notification-test")
    test.add_argument("--send", action="store_true")
    confirm = sub.add_parser("confirm")
    confirm.add_argument("token")
    confirm.add_argument("--device", choices=DEVICES, default="unspecified",
                         help="where you saw the code; only `phone` counts as a real-device check")
    try:
        args = parser.parse_args(list(argv))
    except SystemExit as e:
        return int(e.code)
    now = time.time() if now is None else now
    if args.command == "status":
        routes = push.enabled_routes()
        print_fn("通知経路: " + (", ".join(routes) if routes else "未設定"))
        sm = summary(out)
        print_fn(f"送信受理（経路が受け付けた）{sm['accepted']} 件 / 本人が端末で確認した件数 {sm['confirmed']} 件"
                 f"（うち経路不明の旧形式 {sm['legacy_confirmed']} 件）")
        print_fn(f"実機確認の状態: {sm['device_state']}（詳細は `onboarding device-check`）")
        return 0
    if args.command == "device-check":
        print_fn(device_report(out))
        return 0
    if args.command == "notification-test":
        if not args.send:
            print_fn("dry run: 送信しません。実送信には --send が必要です")
            return 0
        routes = push.enabled_routes()
        if not routes:
            print_fn("失敗: 通知経路が設定されていません")
            return 1
        codes, seen = {}, set()
        for route in routes:   # one code per route: a confirmation names the route that carried it
            code = base = secrets.token_urlsafe(9)
            n = 1
            while code in seen:
                n += 1
                code = f"{base}-{n}"
            seen.add(code)
            codes[route] = code
        with fsutil.exclusive(Path(out) / "onboarding.lock") as locked:
            if not locked:
                print_fn("失敗: onboarding の記録が使用中です")
                return 1
            results = push.send_challenge(codes)
            per_route = {}
            for route in routes:
                result = results.get(route, "skipped")
                per_route[route] = {"token_hash": hashlib.sha256(codes[route].encode("utf-8")).hexdigest(), "result": result,
                                    "sender_accepted": result == "accepted", "receipt_confirmed": False}
            rec = {"version": 2, "created_at": now, "routes": per_route, "results": results,
                   "sender_accepted": any(e["sender_accepted"] for e in per_route.values()), "receipt_confirmed": False}
            data = _read(out)
            data["challenges"] = (data["challenges"] + [rec])[-100:]
            _write(out, data)
        for route, result in results.items():
            print_fn(f"{route}: {result}")
        if not rec["sender_accepted"]:
            print_fn("送信失敗: 送信経路から受理されませんでした")
            return 1
        failed = [r for r, e in per_route.items() if not e["sender_accepted"]]
        if failed:
            print_fn("受理されなかった経路: " + ", ".join(failed) + "（この経路は受信を確認できません）")
        print_fn("送信経路は受理しました。受理は端末への到達を意味しません。経路ごとに別の challenge を送っています: "
                 "スマホで見えた challenge を `onboarding confirm CODE --device phone` に入力してください（見えなかった経路は入力しない）")
        return 0
    if args.command == "confirm":
        token_hash = hashlib.sha256(args.token.encode("utf-8")).hexdigest()
        with fsutil.exclusive(Path(out) / "onboarding.lock") as locked:
            if not locked:
                print_fn("失敗: onboarding の記録が使用中です")
                return 1
            data = _read(out)
            match = None
            for c in reversed(data["challenges"]):
                if not isinstance(c, dict):
                    continue
                for name, e in _entries(c):
                    if _eq(e.get("token_hash"), token_hash):
                        match = (c, name, e)
                        break
                if match:
                    break
            if match is None:
                print_fn("失敗: challenge が不明です")
                return 1
            c, name, entry = match
            if entry.get("receipt_confirmed"):   # idempotent: nothing is rewritten, so a second confirmation cannot change the device or time
                print_fn("失敗: challenge はすでに確認済みです（記録は変えていません）")
                return 1
            try:
                elapsed = now - float(c.get("created_at"))
            except (TypeError, ValueError, OverflowError):
                elapsed = float("nan")
            if not math.isfinite(elapsed) or not 0 <= elapsed <= CHALLENGE_TTL:
                print_fn("失敗: challenge の期限が切れています")
                return 1
            if not entry.get("sender_accepted"):
                print_fn("失敗: 送信経路が受理していない challenge です")
                return 1
            entry["receipt_confirmed"] = True
            entry["confirmed_at"] = now
            entry["device"] = args.device
            if entry is not c:
                c["receipt_confirmed"] = True      # "at least one route confirmed" (the meaning this field always had)
                c.setdefault("confirmed_at", now)
            _write(out, data)
        label = name if name != UNKNOWN_ROUTE else "経路不明の旧形式の記録"
        print_fn(f"少なくとも1つの通知経路（{label}）で、本人が端末受信を確認しました")
        if name == UNKNOWN_ROUTE:
            print_fn("旧形式のため、どの経路の確認としても数えません")
        elif args.device != "phone":
            print_fn("端末が phone ではない（または未指定）ため、スマホ実機の確認としては数えません。実機確認の状態: "
                     + summary(out)["device_state"])
        else:
            print_fn("他の経路は確認していません。実機確認の状態: " + summary(out)["device_state"] + "（`onboarding device-check`）")
        return 0
    return 2


def main(argv=None):
    parser = argparse.ArgumentParser(prog="kimeru onboarding")
    parser.add_argument("--out", default="out")
    args, rest = parser.parse_known_args(argv)
    return dispatch(rest, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
