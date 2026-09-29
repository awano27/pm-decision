# PoC: reading Teams via UI Automation (2026-09-23)

Goal: ingest Teams chats with zero admin consent, no Graph API, no Power Automate.

## Setup
- sbroenne/mcp-windows v1.3.24 standalone (`windows-mcp-server-1.3.24-win-x64.zip`, SHA256 verified against `SHA256SUMS.txt`)
- Driven over MCP stdio (`ui_snapshot`, `window_management`); read-only tools only
- New Teams (MSTeams 25306.804), Windows 11, x64

## Results (structure only; no message text recorded here)

| State | Success | Elements | Time | Content identical to normal |
|---|---|---|---|---|
| Normal (foreground) | yes | 196 | 1.97 s | — |
| Occluded (another window maximized on top) | yes | 196 | 1.89 s | yes |
| Minimized | yes | 195 | 2.16 s | yes (44/44 names) |

- Framework auto-detected as Chromium/Electron. `maxDepth: 20` required; the default depth 5 misses content.
- Chat list: `TreeItem` nodes, each name = chat title + last-message preview + timestamp.
- Open chat: message `Group` nodes with `Text` children (sender, time, body).

## Implications for kimeru
- Viable as the zero-consent default Teams source.
- Without clicking, only the **open chat** has full messages; other chats expose **last-message preview** only.
  Polling the chat list and diffing previews detects new activity; reading full text needs switching chats (UI side effect).

## Unverified
- Whether a minimized window reflects *new* messages live (tested content was unchanged between states).
- Work-account tenant UI vs. this account; localization differences in node names.
- Robustness across Teams UI updates.

## 他のチャットを開いて読む（`teams-self.ps1 -Action readchat`）

既定では使いません（設定 `read_full=1` のときだけ、`kimeru` が必要な件に限って呼びます）。

- `-ChatId <id> -Count 5`: 一覧の該当の項目を選び（選択 → 実行 → クリックの順）、**画面の題名（`| <題名> |`）と、一覧の選択の状態の両方** で開いたことを確かめてから、右側の領域の直近のメッセージの文字を読みます。確かめられないときは、何も読まずに止まり（固定のエラー文を返す）、元のチャットへ戻します
- 読み取りだけです。入力欄、貼り付け、送信の処理は、この Action に入っていません（`tests/test_fulltext.py` が、スクリプトの文面で確かめています）
- 排他（ロック）と、キーボード・マウスが止まるまでの待ちは、他の画面操作と同じです
- 読み終えたら、元に開いていたチャットへ戻し、`returned` に結果を入れます。元のチャットが無かった（Copilot の画面などだった）ときは戻せません
- メッセージの取り方（`how`）は、まず AutomationId（`message-body-` など）、なければ右側の領域の名前つきの要素を位置順に読みます。Teams の版で変わるので、実機の結果（`T24`）で確かめます
- **開いたチャットは、Teams で既読になります**
