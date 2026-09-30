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

- `-ChatId <id> -Count 5 -Preview <プレビューの先頭>`: 一覧の該当の項目を選び（選択 → 実行 → クリックの順）、開いたことを確かめてから、右側の領域の直近のメッセージの文字を読みます。**一覧が選択を報告する画面では、画面の題名（`| <題名> |`）と一覧の選択の状態の両方**、**報告しない画面では、題名に加えて、読んだ文が `-Preview` と合うこと**（先頭 40 文字。どの画面でも 12 文字未満なら、開く前に止まる。「名前:」を外したあとも 12 文字）で確かめます。選択を報告しない画面で、開いているチャットの題名が読むチャットの題名と同じとき（`Test-ChatOpen` と同じ照合）は、画面を動かさず、何も読まず、固定のエラー文を返します（何も操作していないので、戻す処理も通知もありません）。確かめられないときは、何も読まずに止まり（固定のエラー文を返す）、元のチャットへ戻します
- 読み取りだけです。入力欄、貼り付け、送信の処理は、この Action に入っていません（`tests/test_fulltext.py` が、スクリプトの文面で確かめています）
- 排他（ロック。待ちは 60 秒まで）は、他の画面操作と同じです。開く前に、読み取りだけで、キーボード・マウスが止まっていること（既定 30 秒。環境変数 `KIMERU_READ_IDLE_SEC`、設定 `read_idle_sec` だけで決まり、`KIMERU_IDLE_SEC` は効かない）、Teams のウィンドウ（ポップアウトを含む）が前面にないこと、フォーカスが Teams の入力欄にないこと（フォーカスが取れないときは使用中）を確かめ、どれかに当たれば、何も開かずに止まります（待たずに、次のサイクルに回します）。それに加えて、選択・実行の **直前ごとに**、キーボード・マウスを使っていないことを確かめます（使っていれば、何もせずに止まります。自分のクリックは、利用者の操作と数えません）。元のチャットへ戻す処理だけは、別の短い閾値（3 秒、待ちは最長 20 秒）です。元のチャットへ戻す閾値は、`read_idle_sec` が 3 未満ならそれに合わせます。`diag` は、フォーカスの要素の ProcessId・それを持つ窓のハンドルとそのプロセス・前面の窓のプロセス（数字と種類の名前だけ）を出します。`check.ps1` の T24 は、読んだあとに「N 秒以内に Teams の入力欄をクリックしてください」と出して待ち（既定 8 秒）、そのあとで `diag` を取るので、T24-diag には、入力欄にフォーカスがあるときの type、topPid、fgPid、それらが Teams のプロセスか、`teamsInUse` が、数字と真偽だけで載ります（ポップアウトした窓を確かめるときは、その窓を前面にして、その入力欄をクリックします）
- 元に開いていたチャットを、一覧の選択と画面の題名の両方で覚えます。読み終えたとき・失敗したとき・時間切れ（90 秒）・利用者の操作で止まったとき、どれでも、戻す処理を通ります。結果は `restore`（`restored` 戻れた／`unchanged` 元のチャットを読んだだけ／`self` 戻せず、自分とのチャットへ移した／`failed` どちらもできない／`none` 何も動かしていない）と `returned`、`hadOriginal` に入り、失敗のときも返ります。元のチャットが分からなかった（Copilot の画面などだった）ときは、自分とのチャットへ移します
- 戻す処理は、入力欄に触れません（一覧の項目の選択・実行と、自分とのチャットを開く固定のリンクだけ）。Teams を前面に出してクリックする経路は、既定では使いません（環境変数 `KIMERU_READ_CLICK=1`、設定 `read_click` のときだけ）。Teams が前面に出たとき（固定のリンクを含む）は、終わったあと、元の前面のウィンドウへ戻します。何も動かしていないとき（`restore` が `none`）は、戻す処理も通知もありません。元のチャットの復元で時間切れや例外になっても、自分とのチャットへ移す処理は通ります
- 自分とのチャットを開く処理（`open` / `post` の前段）の最後の手段（項目にフォーカスして Enter）は、フォーカスが一覧の項目にあることを確かめてからだけ Enter を送ります（入力欄にあれば送りません）
- メッセージの取り方（`how`）は、まず AutomationId（`message-body-` など）、なければ右側の領域の名前つきの要素を位置順に読みます。Teams の版で変わるので、実機の結果（`T24`）で確かめます
- **開いたチャットは、Teams で既読になります**
