# リファレンス

コマンド・設定・環境変数・出力ファイル・入力形式・判断グラフの書き方・評価の実行方法です。概要は [README](../README.md) を見てください。

### コマンド

- 判断の正誤を付ける・週の集計・係数の調整: `kimeru review` / `kimeru digest --week [--share]` / `kimeru calibrate [--apply | --revert | --allow-wider]` / `kimeru config show --share`（[下の節](#判断の正誤の確認と係数の調整)、[1 週間の試し方](trial-week.md)）

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
python -m kimeru push test|status                 # 通知の経路（iPhone 用）の試験と状態（[docs](push-notification.md)）
```

### Teams の本文を全文で読む

既定では、チャット一覧の 1 行のプレビューだけを読みます。長い依頼は途中で切れ、その前のやり取りも見ません。`kimeru config set read_full 1` にすると、**必要な件だけ**、そのチャットを開いて直近のメッセージ（既定 5 件）を読みます（読み取りだけ。入力欄には触りません）。

- 開くのは、①プレビューが途中で切れている（末尾が「…」。設定 `preview_cut_len` に長さを入れると、その長さ以上も切れているとみなします。既定 0 = 記号だけ）とき、②プレビューでの判断が、人の確認か通知になるとき、のどちらかだけです。ただの挨拶や、自動で決まって通知の要らない件は開きません。プレビューが `read_min_preview`（既定 12。12 より小さくはできません）字より短い件は、開いたチャットが正しいか確かめられないので、開かずにプレビューで判断します
- 全文を読むかどうかを先に決めてから、writer の下書きを **1 回だけ** 書きます（判断は、プレビューと全文の 2 回になることがあります）
- 1 サイクルに開く件数（`read_max_open`、既定 3）と時間（`read_budget_sec`、既定 60 秒）に上限があります。超えた分は、次のサイクルに回します（失敗としては数えず、`schedule status` に「次のサイクルに残したファイル」として別に出ます）。次のサイクルは、プレビューでの判断をやり直さず、読む所から続けます（下書きは、読んだあとに 1 回）。どちらかを 0 にすると、読まずに、プレビューで判断します（待たせません）。人の操作と重なって回した件は、最初に回したときから 24 時間、`read_max_defer`（既定 12）回まで回し、超えたらプレビューで判断します。`read_max_defer=0` は「延期しない」で、最初の 1 回は読み、そのとき読めなければ（操作中、Teams が使用中、件数や時間の上限）、次のサイクルに回さず、プレビューで判断します（読まない設定ではありません）
- 開いたことは、**一覧が選択を報告する画面では、一覧の選択の状態と画面の題名の両方** で確かめます。**選択を報告しない画面では、題名だけでは足りず**、読んだ文がプレビュー（先頭 40 文字）と合うことも確かめます（プレビューが 12 文字未満なら、どの画面でも開きません。「名前:」または「名前：」（空白は有っても無くても）を外したあとの本文で数えます）。選択を報告しない画面で、開いているチャットの題名が読むチャットの題名と同じとき（開いたかの判定と同じ照合です）は、画面を動かさず、何も読まず、プレビューで判断します（何も操作していないので、自分とのチャットへ移さず、通知もせず、延期にも数えません）。確かめられない、または読んだ内容がプレビューと合わないときは、何も使わず、プレビューで判断します（承認の投稿と記録に「プレビューだけで判断しました」と出ます）
- 読み終えたとき・読めなかったとき・途中で失敗したときの、どの場合も、元に開いていたチャットへ戻します（元のチャットは、一覧の選択と画面の題名の両方で覚えます）。時間切れや、操作の確認の例外でも、自分とのチャットへ移す処理を通ります。戻せなければ自分とのチャットへ移し、それもできなければ PC に通知します（`KIMERU_TOAST=0` や通知に失敗したときは、自分とのチャットへ「[kimeru 通知]」として投稿します）。何も動かしていないときは、「開いたままの可能性」とは知らせません。戻れたかどうかは、判断の結果に関係なく、記録（`read_full.returned`・`restore`）に残り、戻せなかった件は投稿と朝のまとめに出ます
- **開く前に、読み取りだけで確かめます**: キーボード・マウスが `read_idle_sec`（既定 30 秒。`idle_sec` は読む処理に影響せず、`idle_sec=0` でも 30 秒です）止まっていること、Teams のウィンドウ（ポップアウトしたチャットを含む）が前面にないこと、Teams の入力欄（Edit / Document）にフォーカスがないこと（フォーカスの場所が取れないときは、使用中として扱います）。どれかに当たれば、何も開かずに次のサイクルに回します。開いたあとも、画面を操作する直前ごとに `read_idle_sec` で確かめます。元のチャットへ戻す処理だけは、別に短く（3 秒の無操作、待ちは最長 20 秒。`read_idle_sec` が 3 未満ならそれに合わせます）確かめます。読む途中であなたが作業を再開しても、同僚のチャットを開いたまま残さないためです。チャットの画面へページを切り替えたとき（Activity・Calendar から）も操作とみなし、戻せなかった件は「戻せなかった」と報告します。Teams を前面に出してクリックする経路は、既定では使いません（`read_click=1` のときだけ）。固定のリンクで Teams が前面に出たときも、終わったあと、元の前面のウィンドウへ戻します
- **開いたチャットは、Teams で既読になります。** 開いて読んで「対応は不要」だった件は、朝のまとめに、件数とチャットの名前で出ます（未読の印を失って見落とさないため）
- 判断モデルには、`text` を `judge_text_max`（既定 1200 文字）で切って渡します。全文は、writer の材料と、承認の投稿の「元（全文）」に使います。直前のやり取りは、writer だけが見ます（判断モデルには渡しません）
- 全文を保存するのは、**確認待ちの間だけ**（`out/full_text.json`）。承認か却下のあとは消え、記録に残るのは、これまでと同じ長さの要約です。確認待ちや未投稿のままの件も、`full_text_keep_days`（既定 7 日）で消え、その件は、プレビューだけで判断したものとして扱います。全文は `.jsonl` に書きません（m365 の依頼文も、記録には抜粋で作ったものを残し、全文のものは `full_text.json` に置きます）
- 同じチャットの続きのメッセージは、前の件がまだ確認待ちなら、同じ番号の投稿に「続きのメッセージ」として加わります
- 行動の題名（ADO の題名など）は、1 行で、`title_max`（既定 100 文字）までです
- 実機での確認: `run-check.cmd T23`（プレビューの長さの分布だけを記録。`preview_cut_len` の目安も出ます）、`T24`（既読のチャット 1 件を開いて読み、元へ戻れるか、読んだ文がプレビューと合ったか、選択を報告する画面か）。T24 は、読んだあとに「N 秒以内に Teams の入力欄をクリックしてください」と出して待ち（既定 8 秒。`KIMERU_T24_FOCUS_SEC` で 3〜60 秒）、そのあとで `diag` を取ります（ポップアウトした窓を確かめるときは、その窓を前面にして、その入力欄をクリックします）。結果シートの `T24-diag` には、フォーカスの種類（type）、topPid、fgPid、それぞれが Teams のプロセスか、`teamsInUse` を、数字と真偽だけで出します（本文は出しません）。NG のときも、スクリプトの生の出力は結果シートに書きません

### 判断の速さ

- `decisions.jsonl` の `perf` に、1 イベントごとの **判断の呼び出しの回数・質問の数・秒数・writer の秒数** を残します。サイクルごとの合計は `daily.log.jsonl` に、中央値と最大は `kimeru digest`（と `digest --week`）に出ます
- 判断の順番は、設定 `priority`（既定 `monitor.alert,teams.chat,ado.workitem.created,meeting.item`）。1 サイクルの時間の上限は、設定 `cycle_budget_sec`（既定は自動運転の間隔）
- 設定 `plan_lean=1` にすると、進め方（plan）は、不要と判断した手順の期限を聞きません（既定は無効: 呼び出しが増えるため。速くなるかは Kev で測ってから決めます）。最終的な行動は変わりません
- 設定 `batch_questions=1` にすると、グラフの judge の質問を **1 回の呼び出し** にまとめます（既定は無効。規則だけで決まる経路は、どちらでも 0 問）。最終的な行動は、判断モデルが質問を互いに独立に答えるなら、1 問ずつと同じです
- `batch_questions=1` のとき、規則の結果で通らない枝の質問は、聞きません（規則で外れた質問まで聞くと、質問が増えるため）
- 全文を読んで判断し直した件（1 回目はプレビュー、2 回目は全文）は、`perf` に、2 回の合計を残します
- 測る: `python eval/measure_speed.py --backend kev [--repeat N]`（`sequential` / `lean` / `batch` の 3 通りを、呼び出し・質問・秒数・最終的な行動の違いの表で比べます）。この PC で Kev が起動しているかを先に確かめ、起動していなければ（または接続先がこの PC でなければ）、1 行の案内を出して終わります。Jev は受け付けません（性能の数値は公開できません）

### 判断の正誤の確認と係数の調整

自分の業務の判断で、kimeru の判断が合っているかを測り、係数を調整します。**判断モデルは呼びません。Teams も使いません。** 手順は [1 週間の試し方](trial-week.md)。

- `kimeru review [--limit N]`: 自動で決まった判断を 1 件ずつ、端末で「合っている / 違う / 分からない」と答えます。違うときは、どの質問の答えが違い、正しい答えは何かを選びます。結果は、状態フォルダの `reviews.jsonl`（正誤と、その判断をした判断モデルの名前）と `fixtures_user.jsonl`（`eval/fixtures.jsonl` と同じ形の評価用の例。score の正解は、幅で持ちます）に残ります。元のイベントは、`decisions.jsonl` にあるとおり（既定は要約の長さ。長い依頼の全文を例に残すには、設定 `record_event_full=1`）
- `kimeru digest --week [--share]`: 直近 7 日の件数、自動と人の割合、規則で決めた件数、重大な通知、承認の結果、判断の一致率。`--share` は、Issue に貼れる形です（数値と環境だけ。同梱のグラフ以外の名前は「追加のグラフ N」に伏せ、本文・人名・件名・ID・パスは出ません）。**一致率は、Jev の判断を数えません**（期間や出力先が違っても）。Jev の性能の数値は、公開できません（TypeSafe の利用規約）
- `kimeru config show --share`: Issue に貼れる設定の一覧です。パス、ADO の組織とプロジェクト、サブスクリプション、メールの宛先、接続先の URL は出さず、「設定あり」とだけ出します。1 行目に、kimeru の版、OS、Python、画面の倍率が出ます。不具合の報告には、`config show` ではなく、これを貼ってください
- `kimeru calibrate [--apply | --revert | --allow-wider] [--min-questions N]`: 記録した回答を、別の係数で読み直して、`conf_scale` と `noul_scale` の変更を探します（条件は 1 週間の試し方）。**判断モデルごとに分けて**探し、適用します。Jev・Kev・CLM の判断を確かめた記録だけが対象で、stub など判断モデルの分からない記録からは、探さず、適用せず、理由を出します。`--apply` は、状態フォルダの `thresholds.json` に書きます。グラフと `profiles.py` は書き換えません。`--revert` は、このファイルを消します

状態フォルダ（`%LOCALAPPDATA%\kimeru`、`KIMERU_STATE_DIR` で変えられます）のファイル:

| ファイル | 中身 |
|---|---|
| `config.json` | 設定ファイル（上の節） |
| `reviews.jsonl` | `kimeru review` の正誤（判断の識別子、正誤、時刻、判断モデルの名前）。本文は入りません |
| `fixtures_user.jsonl` | `kimeru review` で作った評価用の例（元のイベントと、正しい答え）。`python eval/run_eval.py score answers.jsonl --fixtures <このファイル>` で読めます |
| `thresholds.json` | `kimeru calibrate --apply` が書いた係数の上書き（判断モデルごと）。`eval/` のスクリプトは、既定では読みません（`--user-thresholds` を付けたときだけ読み、出力にそのことを出します） |

### 設定ファイル

秘密でない設定は、状態フォルダの `config.json` に置けます。優先する順番は、**コマンドの引数 > 環境変数 > 設定ファイル > 既定値** です。起動のときに 1 回だけ読みます。

```powershell
python -m kimeru config set writer m365      # 設定ファイルに書く
python -m kimeru --out <出力先> config show   # 項目ごとの今の値と出どころ（arg / env / file / default）
python -m kimeru config unset writer
python -m kimeru config path
```

- API キーとトークンは、設定ファイルに書けません（書くと、使われず、警告が出ます）。`show` は、それらが設定されているかどうかだけを出します
- 設定ファイルが壊れているときは、どこが壊れているかを示して、終了コード 2 で止まります。ただし `config path`、`schedule remove` / `status`、`--help`、`demo --replay` は、壊れていても動きます
- **空の環境変数は、未設定と同じ**です（設定ファイルの値、なければ既定値が使われます）。空にして上書きすることはできません。設定ファイルの空の値も、コマンドラインの空の値も、空白だけの値も同じです。これは、すべての設定で同じです（判断モデルの接続先が空でも、モデル名が空でも、既定値になります。空の接続先が別の接続先に落ちることはありません）
- 選べる値が決まっている項目（`backend`・`writer`・`toast`、0/1 の項目、`brief_hour`）は、`config set` のときと、読むときに検査します。読むときは、まず書き方の違いを直します（大文字・小文字、前後の空白、`on` / `off`、`true` / `false`、`yes` / `no`。`Copilot` は `copilot`、`on` は `1`）。それでも知らない値のとき、**`backend` だけ**は、送り先が変わるので、何が違うかを示して終了コード 2 で止まります（直すための `config` コマンドは動きます）。それ以外の項目は、既定値で続け、理由を `config` の警告（標準エラー）と `daily.log.jsonl`（`warnings`）に残します。自動運転は止まりません
- 引数は、省略形（`--back`）を受け付けません（設定に届かないため）。正式な名前で書きます
- Kev と CLM の接続先（`kev_url`・`clm_url`）は、この PC（`127.0.0.1`、`localhost`、`::1`）だけです。ほかのアドレスは、判断の本文が PC の外へ出るので、`kev_url_remote_ok=1`（CLM は `clm_url_remote_ok=1`）を明示しない限り、理由を示して止まります
- `config set push_webhook_body @ファイル名` は、ファイルの中身（JSON）を本文の形として書きます。JSON として正しくないと、書きません。設定ファイルを手で編集して、`{...}` を足さないでください（JSON が 2 つになって、すべてのコマンドが止まります）
- `config set` / `unset` は、ファイルにある、この版が知らない項目を残します。秘密のような名前の項目は、`set` はできませんが、`unset` で消せます
- `daily.log.jsonl` は 256KB を超えると、新しい半分だけに切り詰めます。`schedule status` は末尾だけを読みます
- `schedule install` は、そのとき効いている設定を設定ファイルに保存し、起動行に判断モデルの種類（`--backend stub` も）を必ず付けます。ユーザー環境変数の `KIMERU_BACKEND` があっても、自動運転の判断モデルは変わりません。自動運転は、シェルの設定を引き継がないので、この保存が引き継ぎです。タスクの「前回の結果」には、`daily` の終了コードが出ます
- `daily` は、サイクルごとに、効いている設定の名前と出どころを `daily.log.jsonl` に残します（値は、writer・通知など秘密でないものだけ）。writer が未設定のときは、定型文で動くことも残します
- 自動運転が設定を引き継いだかは、1 サイクルあとに `python -m kimeru --out <出力先> schedule status` で確かめます（最後のサイクルの時刻、失敗した手順、判断待ちの件数、設定の出どころ）

### 環境変数

| 変数 | 意味 |
|---|---|
| `KIMERU_BACKEND` | 既定の判断モデル（`stub` / `jev` / `kev`） |
| `KEV_API_KEY` / `CLM_API_KEY` | Kev / CLM に鍵が要るときのキー（**秘密**: 環境変数だけ。表示には、あるかどうかだけ） |
| `KIMERU_PRIORITY` | 判断の順番（種類のコンマ区切り。既定 `monitor.alert,teams.chat,ado.workitem.created,meeting.item`） |
| `KIMERU_CYCLE_BUDGET` | 1 サイクルの時間の上限（秒。未設定は自動運転の間隔） |
| `KIMERU_PLAN_LEAN` / `KIMERU_BATCH` | 不要な手順の期限を聞かない（既定 0）／グラフの質問を 1 回にまとめる（既定 0） |
| `KIMERU_READ_FULL` / `KIMERU_READ_MAX_OPEN` / `KIMERU_READ_BUDGET` / `KIMERU_READ_MESSAGES` | チャットを開いて全文を読む（既定 0）、1 サイクルに開く数（既定 3。0 なら読まない）、読む秒数の上限（既定 60。0 なら読まない）、開いたチャットから読む末尾のメッセージ数（既定 5） |
| `KIMERU_READ_IDLE_SEC` / `KIMERU_READ_CLICK` / `KIMERU_READ_MAX_DEFER` | 全文を読むために開く前と、開いたあとの操作の直前に、キーボード・マウスが止まっているべき秒数（既定 30、`0` で確かめない。`KIMERU_IDLE_SEC` は読む処理に影響しない。戻す処理は別に 3 秒。`read_idle_sec` が 3 未満ならそれに合わせる）／Teams を前面に出してクリックする経路を使う（既定 0）／人の操作と重なって回せる回数の上限（既定 12。超えたらプレビューで判断。0 は延期しない: 最初の 1 回は読み、読めなければプレビューで判断） |
| `KIMERU_READ_MIN_PREVIEW` / `KIMERU_PREVIEW_CUT_LEN` / `KIMERU_FULL_TEXT_KEEP_DAYS` | これより短いプレビューの件は開かない（既定 12 字。これより小さくはできない）／この長さ以上のプレビューも「切れている」とみなす（既定 0 = 末尾の記号だけ。`T23` の結果で決める）／確認待ちの全文を残す日数（既定 7） |
| `KIMERU_RECORD_EVENT_FULL` | `1` で、`decisions.jsonl` に元のイベントの全体（本文などは 2,000 文字まで）を残す。既定 0 は、要約の長さ（120 文字）まで（[SECURITY.md](../SECURITY.md#記録に残る範囲と期間)） |
| `KIMERU_JUDGE_TEXT_MAX` / `KIMERU_TITLE_MAX` | 判断モデルに見せる `text` の文字数（既定 1200）／行動の件名の文字数（既定 100） |
| `KIMERU_KEV_URL` / `KIMERU_KEV_URL_REMOTE_OK` | Kev の接続先（既定 `http://127.0.0.1:8009/v1`。空は既定）。この PC 以外の接続先は、`KIMERU_KEV_URL_REMOTE_OK=1` を明示したときだけ使えます |
| `TYPESAFE_API_KEY` | Jev を使うときのキー |
| `KIMERU_WRITER` | 文面の下書き（`copilot` / `codex` / `grok` / `claude` / `cmd` / `m365` / `m365-auto`。未設定なら定型文） |
| `KIMERU_WRITER_MODEL` | writer のモデル名（`copilot` / `claude`。省略可） |
| `KIMERU_CODEX_MODEL` / `KIMERU_CODEX_EFFORT` | `codex` のモデル名（既定 `gpt-6-luna`）と推論の強さ（既定 `low`）。空は未設定と同じで、既定値になります |
| `KIMERU_GROK_MODEL` | `grok` のモデル名（省略可） |
| `KIMERU_TOAST` | `0`（または `off`）で PC の Windows 通知を止める。`detail` で、PC の通知だけに、確認待ちの件名などを含める（iPhone などの経路には、常に件数と番号だけ） |
| `KIMERU_PUSH` / `KIMERU_PUSH_MIN_MINUTES` | 通知の経路（`teams_webhook,webhook,outlook` のコンマ区切り）と、同じ経路への最小の間隔（分、既定 5）。経路を有効にした最初のサイクルの始めに、それまでの確認待ちと通知は「通知済み」として記録するだけで送りません（そのサイクルで新しく投稿された分からは送ります。経路を外していた間の分は、有効にし直しても送りません）。一覧の名前は、環境変数と設定ファイルでは、大文字小文字と前後の空白を読み替えます（`config set push` は、正確な経路名だけを受け付けます）。経路名でないものは無視して警告し、残りの経路は使います |
| `KIMERU_PUSH_TEAMS_URL` / `KIMERU_PUSH_WEBHOOK_URL` / `KIMERU_PUSH_WEBHOOK_KEY` | 経路の URL（https だけ）と鍵。秘密なので、環境変数だけ。表示にも記録にも出ません |
| `KIMERU_PUSH_WEBHOOK_BODY` | 汎用の webhook の本文の形（JSON、`{text}` と `{key}`）。`config show` は中身を出しません |
| `KIMERU_AZ` | `az.cmd` の場所（`kimeru` の隣の `az`、`C:\az` も探します） |
| `KIMERU_SELF_MARKER` | 自分とのチャットの表示が「(あなた)」でないときの言葉（例: `自分`） |
| `KIMERU_BRIEF_HOUR` / `KIMERU_ADO_ORG` / `KIMERU_ADO_PROJECT` / `KIMERU_SUBSCRIPTION` | 朝のまとめの時刻（既定 8）、ADO の組織とプロジェクト、Azure Monitor のサブスクリプション（`daily` の引数の既定値） |
| `KIMERU_CLM_URL` / `KIMERU_CLM_URL_REMOTE_OK` | CLM（試験用）の接続先（既定 `http://127.0.0.1:8700/v1`）と、この PC 以外を許す明示の設定（既定 0） |
| `KIMERU_EXECUTE` / `KIMERU_EXECUTE_SIGNATURE` | 承認のあとに実行する種類（実装済みは `ado.comment` だけ。既定は無し）と、コメント末尾の kimeru の 1 行（既定 1、`0` で外す） |
| `KIMERU_SEND_READY` / `KIMERU_OPEN_CHAT_LINK` | 返信を承認したら送る文面だけを自分とのチャットに返す（既定 1）／相手とのチャットを開くリンクも付ける（実験的。既定 0） |
| `KIMERU_IDLE_SEC` | Teams を操作する前に、キーボード・マウスが止まっているべき秒数（既定: `post`・`send` は 3、teams-copilot は 4。`0` で待たない。他のチャットを開いて読む処理には効かず、そちらは `KIMERU_READ_IDLE_SEC`（既定 30）だけで決まる） |
| `KIMERU_STATE_DIR` | （`schedule install` が、設定されていれば自動運転の起動行に引き継ぎます）状態フォルダ（設定ファイル、自分とのチャットの目印、writer の休止の印、診断）。未設定なら `%LOCALAPPDATA%\kimeru` |
| `KIMERU_COPILOT_EXE` / `KIMERU_CLAUDE_EXE` / `KIMERU_CODEX_EXE` / `KIMERU_GROK_EXE` | 各 CLI の場所（PATH に無いとき） |
| `KIMERU_WRITER_CMD` | `KIMERU_WRITER=cmd` のコマンド（プロンプトを標準入力で受け、答えを標準出力へ） |
| `KIMERU_WRITER_NO_REST` / `KIMERU_M365_NO_REST` | `1` で、writer の休止（月間上限・失敗のあと）を無視する（試験用） |
| `KIMERU_DEBUG_WRITER` | `1` で、writer が JSON を返さなかったときの答えの先頭を表示する（架空のサンプルだけで使う） |
| `KEV_DTYPE` | Kev の計算方式（`bf16` / `fp32`。未設定なら CPU に合わせて自動） |

自動運転（`schedule install`）が引き継ぐもの: そのとき効いている設定（引数・環境変数）は設定ファイルへ保存され（既定値と同じ値を指定したときも、ファイルの古い値を置き換えます）、`KIMERU_STATE_DIR` は起動行に付きます。**引き継がない**もの: `KIMERU_WRITER_CMD` と `KIMERU_*_EXE`（この PC の場所やコマンドなので、ユーザー環境変数に置いてください）、秘密の環境変数（同じくユーザー環境変数）。

### 出力ファイル（`out/`）

`push_state.json`: 通知の経路ごとの状態（通知済みの印、最後の成功と失敗、休み）。URL は入りません。`python -m kimeru --out <出力先> push status` で読めます。

| ファイル | 中身 |
|---|---|
| `decisions.jsonl` | 全判断の経路・回答・記録した行動・元のイベントの抜粋（既定は本文などが 120 文字まで） |
| `queue.jsonl` | 人の確認待ち |
| `notices.jsonl` | 自動で決めたが知らせる重大な判断（`[kimeru 通知]`） |
| `approvals.json` / `approvals.log.jsonl` | 承認依頼の番号・状態（書く文面と宛先、実行の状態、送り直し待ちの投稿）と、OK/NG などの記録 |
| `executions.jsonl` | 承認した ADO コメントの実行の記録（実行中・完了・失敗・結果不明・閉じた。組織・プロジェクトを含む） |
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
python eval/run_eval.py score answers.jsonl --fixtures <状態フォルダ>\fixtures_user.jsonl   # 自分で確かめた判断で採点する（tune も同じ）
```

`eval/` のスクリプトは、`kimeru calibrate --apply` が状態フォルダに書いた係数を、既定では読みません（数値が、その PC の状態で変わらないように）。読むのは `--user-thresholds` を付けたときだけで、出力にそのことが出ます。



| 入力 | グラフ | 判断ポイント | 行き先 |
|---|---|---|---|
| 💬 Teams チャット | `teams_chat.json` | 意図 → 進め方 / 判断期限 | 受領返信・手順ごとの Task・Bug 化・人の確認 |
| 🚨 監視アラート | `monitor_alert.json` | 解消済み → **重大障害（規則）** → 将来のリスクか → 時期 / 顧客影響（**Sev0/1 ガード**）→ ノイズか | 当番呼び出し・予防 Task・P1 Bug・閾値見直し |
| 🎫 ADO チケット | `ado_workitem.json` | **重大バグ（規則）** → 受け入れ条件・再現手順（規則）→ 着手可能か → 優先度 | P1 固定・情報不足コメント・優先度設定・人のトリアージ |
| 📝 議事録 | `meeting_item.json` | ラベル（`決定:` `タスク:` `リスク:` `共有:`）は規則、なければ行の種類 → 担当 → 期限 | 決定ログ・Task・Risk 登録と対策手順 |

進め方の型（`playbooks/`）: スケジュール変更 / 障害対応 / スコープ変更 / 人員調整 / リリース判定 / ステークホルダー対応 / リスク対応 / 要件明確化。

グラフも型も JSON なので、**自分のチームの判断ポイントを足せます**。閉路・到達不能ノード・ルート漏れ・不正なしきい値は `python -m kimeru validate` が弾きます。

