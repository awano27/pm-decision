# リファレンス

コマンド・設定・環境変数・出力ファイル・入力形式・判断グラフの書き方・評価の実行方法です。概要は [README](../README.md) を見てください。

### コマンド

- 判断の正誤を付ける・週の集計・係数の調整: `kimeru review` / `kimeru digest --week [--share]` / `kimeru calibrate [--apply | --revert | --allow-wider]`（[1 週間の試し方](trial-week.md)）

```bash
python -m kimeru validate
python -m kimeru demo [--pace 1.5]
python -m kimeru run <payload.json>...
python -m kimeru watch inbox/                     # inbox/*.json と議事録 *.txt を常駐処理（処理済みは inbox/done/）
python -m kimeru digest                           # 今日の判断件数と確認待ち
python -m kimeru pull teams|ado|alerts --inbox inbox    # ado: --org <組織 or URL> [--project <名前>] [--login]
python -m kimeru brief [--post [--send]]
python -m kimeru daily [--once] [--send]
python -m kimeru schedule install|remove|status   # タスクスケジューラ（管理者権限不要）
```

### Teams の本文を全文で読む

既定では、チャット一覧の 1 行のプレビューだけを読みます。長い依頼は途中で切れ、その前のやり取りも見ません。`kimeru config set read_full 1` にすると、**必要な件だけ**、そのチャットを開いて直近のメッセージ（既定 5 件）を読みます（読み取りだけ。入力欄には触りません）。

- 開くのは、①プレビューが途中で切れている（末尾が「…」）とき、②プレビューでの判断が、人の確認か通知になるとき、のどちらかだけです。ただの挨拶や、自動で決まって通知の要らない件は開きません
- 1 サイクルに開く件数（`read_max_open`、既定 3）と時間（`read_budget_sec`、既定 60 秒）に上限があります。超えた分は、次のサイクルに回します
- 開いたことは、**一覧の選択の状態と、画面の題名の両方** で確かめます。確かめられない、または読んだ内容がプレビューと合わないときは、何も使わず、プレビューで判断します（承認の投稿と記録に「プレビューだけで判断しました」と出ます）
- 読み終えたら、元に開いていたチャットへ戻します（戻せなかったときは、投稿に出ます）
- **開いたチャットは、Teams で既読になります。** 開いて読んで「対応は不要」だった件は、朝のまとめに、件数とチャットの名前で出ます（未読の印を失って見落とさないため）
- 判断モデルには、`text` を `judge_text_max`（既定 1200 文字）で切って渡します。全文は、writer の材料と、承認の投稿の「元（全文）」に使います。直前のやり取りは、writer だけが見ます（判断モデルには渡しません）
- 全文を保存するのは、**確認待ちの間だけ**（`out/full_text.json`）。承認か却下のあとは消え、記録に残るのは、これまでと同じ長さの要約です
- 同じチャットの続きのメッセージは、前の件がまだ確認待ちなら、同じ番号の投稿に「続きのメッセージ」として加わります
- 行動の題名（ADO の題名など）は、1 行で、`title_max`（既定 100 文字）までです
- 実機での確認: `run-check.cmd T23`（プレビューの長さの分布だけを記録）、`T24`（既読のチャット 1 件を開いて読み、元へ戻れるか）

### 判断の速さ

- `decisions.jsonl` の `perf` に、1 イベントごとの **判断の呼び出しの回数・質問の数・秒数・writer の秒数** を残します。サイクルごとの合計は `daily.log.jsonl` に、中央値と最大は `kimeru digest`（と `digest --week`）に出ます
- 判断の順番は、設定 `priority`（既定 `monitor.alert,teams.chat,ado.workitem.created,meeting.item`）。1 サイクルの時間の上限は、設定 `cycle_budget_sec`（既定は自動運転の間隔）
- 進め方（plan）は、不要と判断した手順の期限を聞きません（設定 `plan_lean`。`0` で従来どおり）。最終的な行動は変わりません
- 設定 `batch_questions=1` にすると、グラフの judge の質問を **1 回の呼び出し** にまとめます（既定は無効。規則だけで決まる経路は、どちらでも 0 問）。最終的な行動は、判断モデルが質問を互いに独立に答えるなら、1 問ずつと同じです
- 測る: `python eval/measure_speed.py --backend kev`（Kev を起動している必要があります。`sequential` / `lean` / `batch` の 3 通りを、呼び出し・質問・秒数・最終的な行動の違いの表で比べます）

### 設定ファイル

秘密でない設定は、状態フォルダの `config.json` に置けます。優先する順番は、**コマンドの引数 > 環境変数 > 設定ファイル > 既定値** です。起動のときに 1 回だけ読みます。

```powershell
python -m kimeru config set writer m365      # 設定ファイルに書く
python -m kimeru config show                 # 項目ごとの今の値と出どころ（arg / env / file / default）
python -m kimeru config unset writer
python -m kimeru config path
```

- API キーとトークンは、設定ファイルに書けません（書くと、使われず、警告が出ます）。`show` は、それらが設定されているかどうかだけを出します
- 設定ファイルが壊れているときは、どこが壊れているかを示して、終了コード 2 で止まります
- `schedule install` は、そのとき効いている設定を設定ファイルに保存します。自動運転は、シェルの設定を引き継がないので、この保存が引き継ぎです。タスクの「前回の結果」には、`daily` の終了コードが出ます
- `daily` は、サイクルごとに、効いている設定の名前と出どころを `daily.log.jsonl` に残します（値は、writer・通知など秘密でないものだけ）。writer が未設定のときは、定型文で動くことも残します
- 自動運転が設定を引き継いだかは、1 サイクルあとに `python -m kimeru --out <出力先> schedule status` で確かめます（最後のサイクルの時刻、失敗した手順、判断待ちの件数、設定の出どころ）

### 環境変数

| 変数 | 意味 |
|---|---|
| `KIMERU_BACKEND` | 既定の判断モデル（`stub` / `jev` / `kev`） |
| `KIMERU_KEV_URL` | Kev の接続先（既定 `http://127.0.0.1:8009/v1`） |
| `TYPESAFE_API_KEY` | Jev を使うときのキー |
| `KIMERU_WRITER` | 文面の下書き（`copilot` / `codex` / `grok` / `claude` / `cmd` / `m365` / `m365-auto`。未設定なら定型文） |
| `KIMERU_WRITER_MODEL` | writer のモデル名（`copilot` / `claude`。省略可） |
| `KIMERU_CODEX_MODEL` / `KIMERU_CODEX_EFFORT` | `codex` のモデル名（既定 `gpt-6-luna`）と推論の強さ（既定 `low`）。空にすると Codex の設定（`~/.codex/config.toml`）に従う |
| `KIMERU_GROK_MODEL` | `grok` のモデル名（省略可） |
| `KIMERU_TOAST` | `0` で PC の Windows 通知を止める |
| `KIMERU_AZ` | `az.cmd` の場所（`kimeru` の隣の `az`、`C:\az` も探します） |
| `KIMERU_SELF_MARKER` | 自分とのチャットの表示が「(あなた)」でないときの言葉（例: `自分`） |
| `KIMERU_BRIEF_HOUR` / `KIMERU_ADO_ORG` / `KIMERU_ADO_PROJECT` / `KIMERU_SUBSCRIPTION` | 朝のまとめの時刻（既定 8）、ADO の組織とプロジェクト、Azure Monitor のサブスクリプション（`daily` の引数の既定値） |
| `KIMERU_CLM_URL` | CLM（試験用）の接続先 |
| `KIMERU_EXECUTE` / `KIMERU_EXECUTE_SIGNATURE` | 承認のあとに実行する種類（実装済みは `ado.comment` だけ。既定は無し）と、コメント末尾の kimeru の 1 行（既定 1、`0` で外す） |
| `KIMERU_SEND_READY` / `KIMERU_OPEN_CHAT_LINK` | 返信を承認したら送る文面だけを自分とのチャットに返す（既定 1）／相手とのチャットを開くリンクも付ける（実験的。既定 0） |
| `KIMERU_PUSH` / `KIMERU_PUSH_MIN_MINUTES` | 件数だけを送る経路（`teams_webhook`,`webhook`,`outlook` をコンマ区切り。既定は無し）と、同じ経路への通知の最短の間隔（分。既定 5） |
| `KIMERU_PUSH_TEAMS_URL` / `KIMERU_PUSH_WEBHOOK_URL` | 上の経路の URL（**秘密**: 環境変数だけ。設定ファイルには書けない） |
| `KIMERU_PUSH_WEBHOOK_BODY` / `KIMERU_PUSH_MAIL_TO` | `webhook` の本文の形（`{text}` を含む JSON。既定 `{"text": "{text}"}`）と、`outlook` の宛先（自分のアドレス） |
| `KIMERU_IDLE_SEC` | Teams を操作する前に、キーボード・マウスが止まっているべき秒数（既定 4、`0` で待たない） |
| `KIMERU_STATE_DIR` | 状態フォルダ（設定ファイル、自分とのチャットの目印、writer の休止の印、診断）。未設定なら `%LOCALAPPDATA%\kimeru` |
| `KIMERU_COPILOT_EXE` / `KIMERU_CLAUDE_EXE` / `KIMERU_CODEX_EXE` / `KIMERU_GROK_EXE` | 各 CLI の場所（PATH に無いとき） |
| `KIMERU_WRITER_CMD` | `KIMERU_WRITER=cmd` のコマンド（プロンプトを標準入力で受け、答えを標準出力へ） |
| `KIMERU_WRITER_NO_REST` / `KIMERU_M365_NO_REST` | `1` で、writer の休止（月間上限・失敗のあと）を無視する（試験用） |
| `KIMERU_DEBUG_WRITER` | `1` で、writer が JSON を返さなかったときの答えの先頭を表示する（架空のサンプルだけで使う） |
| `KEV_DTYPE` | Kev の計算方式（`bf16` / `fp32`。未設定なら CPU に合わせて自動） |

### 出力ファイル（`out/`）

| ファイル | 中身 |
|---|---|
| `decisions.jsonl` | 全判断の経路・回答・記録した行動 |
| `queue.jsonl` | 人の確認待ち |
| `notices.jsonl` | 自動で決めたが知らせる重大な判断（`[kimeru 通知]`） |
| `approvals.json` / `approvals.log.jsonl` | 承認依頼の番号・状態と、OK/NG などの記録 |
| `processed.txt` | 判断済みのイベント（グラフの版とイベント ID）。二重判断の防止 |
| `pull_state.json` / `daily.log.jsonl` | 取り込みの位置、各サイクルの結果 |
| `report.html` | 判断の一覧レポート |

### 入力形式

自動判別: Microsoft Graph `chatMessage`、Azure Monitor 共通アラートスキーマ、Azure DevOps `workitem.created`、議事録 `{title, date, text}` または箇条書きの `.txt`（[docs/minutes-format.md](minutes-format.md)）。

### judge ノード

```json
{"kind": "judge", "question": {"type": "noul|choice|score", "instructions": "...", "criteria": "..."}, "routes": {"unsure": "..."}}
```

- noul: `yes` / `no` / `unsure`。`yes_at`（既定 0.7）、`no_at`（既定 0.3）
- choice: 各選択肢 + `unsure`。`min_conf`（既定 0.6）未満は unsure
- score: `bands: [[上限(未満), node], ...]` + `unsure`。`guards` で「Sev0/1 なのに低い評価」を人へ回せる

### match ノード（規則）

```json
{"kind": "match", "fields": ["title", "description"], "patterns": ["..."], "exclude": ["テスト環境"], "mixed_if": ["本番"], "routes": {"yes": "...", "no": "...", "mixed": "..."}}
```

正規表現で判定（NFKC 正規化・大文字小文字無視）。`exclude` は同じ欄の一致だけを取り消し、同じ欄に `mixed_if` もあれば `mixed`（人へ）。

### plan ノード

`playbooks/*.json` の型を選び、手順ごとの要否・最初の一手・期限（今日 / 今週 / 次スプリント以降）を 1 回のバッチで聞く。終端の `per_step` で手順ごとのアクションを作れる。

### decide ノードの `notify`

`"notify": true` を付けた decide ノード（当番呼び出し・P1・今日中の判断など）は、承認が要らない場合でも `[kimeru 通知]` として自分とのチャットに 1 回投稿されます（writer が文面を書いた件は、承認待ちとして届きます）。

### 評価

```bash
python eval/run_eval.py live --backend kev --out answers.jsonl        # 判断ポイントごと
python eval/e2e.py --backend kev [--fixtures fixtures_holdout.jsonl]  # 最終的な行動
python eval/drafts.py --backend stub --writer copilot                 # 文面の下書きの点検
python eval/day_run.py --backend kev --writer copilot                 # PM の 1 日を、Teams だけ偽物にして通す
```



| 入力 | グラフ | 判断ポイント | 行き先 |
|---|---|---|---|
| 💬 Teams チャット | `teams_chat.json` | 意図 → 進め方 / 判断期限 | 受領返信・手順ごとの Task・Bug 化・人の確認 |
| 🚨 監視アラート | `monitor_alert.json` | 解消済み → **重大障害（規則）** → 将来のリスクか → 時期 / 顧客影響（**Sev0/1 ガード**）→ ノイズか | 当番呼び出し・予防 Task・P1 Bug・閾値見直し |
| 🎫 ADO チケット | `ado_workitem.json` | **重大バグ（規則）** → 受け入れ条件・再現手順（規則）→ 着手可能か → 優先度 | P1 固定・情報不足コメント・優先度設定・人のトリアージ |
| 📝 議事録 | `meeting_item.json` | ラベル（`決定:` `タスク:` `リスク:` `共有:`）は規則、なければ行の種類 → 担当 → 期限 | 決定ログ・Task・Risk 登録と対策手順 |

進め方の型（`playbooks/`）: スケジュール変更 / 障害対応 / スコープ変更 / 人員調整 / リリース判定 / ステークホルダー対応 / リスク対応 / 要件明確化。

グラフも型も JSON なので、**自分のチームの判断ポイントを足せます**。閉路・到達不能ノード・ルート漏れ・不正なしきい値は `python -m kimeru validate` が弾きます。

