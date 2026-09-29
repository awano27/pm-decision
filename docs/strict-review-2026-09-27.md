# kimeru 厳格レビューと改善アドバイス（2026-09-27）

- 対象: HEAD e07b67c。graphs / teams / writer / eval / ops / product / code の 7 観点。前回レビュー（`docs/codebase-product-review-2026-09-26.md`）の項目は、修正が不完全・誤りの場合だけ再掲した。
- 方法: 各指摘を検証者 2 名が StubBackend / ReplayBackend / 偽ブリッジ / ローカルソケット / PowerShell 5.1 で再現または反証した。実 Teams・Kev の実推論・実テナント・`claude -p` は未実行。【要確認】は、検証者間で重大度か事実認定が割れたもの、または実機未確認の前提に依存するもの。

## 総評

- 良い点は 3 つ。エンジンは堅い（循環拒否、`render` は 1 回置換で波括弧注入は不成立、unittest 105 件成功）。ADO・当番呼び出し・相手への返信は dry-run 固定で外に出ない（自分チャットへの投稿だけは実送信）。`eval/README.md` は限界を正直に書いている。
- 製品としては「PM が 1 日に得るもの」がほぼ無い。OK と返しても何も実行されず、確信して決めた障害・P1・当日判断は通知されない。iPhone に届くのは迷った雑件だけである。
- 無人運転は成立していない。文書どおりの導入コマンドが PowerShell 5.1 で通らない。動けば 5 分ごとに Teams を自分チャットへ切り替え、失敗時はクリップボードを書き換えたままにする。止まっても誰にも知らされない。
- 安全性の主張は裏付けが弱い。安全網の正規表現は fixture の文面に合わせた固定語で、ホールドアウトは規則に一度も当たっていない。「誤った行動 0」は採点規則の産物である。
- 本番の入力は評価と形が違う。Teams は最後の 1 行プレビュー、ADO は既定 priority 入り、アラートは説明が空になりうる。いずれも一度も測られていない。
- テストが緑でも月曜の成功は保証されない。実 `teams-self.ps1`、プロキシ、Kev の通信断、貼り付けモード、編集された議事録をテストは通らない。
- 最大のリスクは、確信した判断ほど誰にも届かない設計のまま、金曜に「自動で決める・取りこぼし 0・PC の外に出ない・OK で実行」と言うこと。どれも条件付きでしか成り立たない。

## P1

- **確信して decide に着いた判断ほど PM に届かない** — `kimeru/graph.py:243`、`kimeru/cli.py:86`、`kimeru/brief.py:57-81`
  - 何が起きるか: `needs_human` は advise かつ queue=true のときだけ真になる。advice 付きの decide 終端 12 個（page / set_p1_critical / decision_today など）は `decisions.jsonl` に残るだけ。Sev1「customers cannot pay」、ADO「本番で全ユーザーがログインできない」、Teams「今日中に判断お願いします」の 3 件で投稿 0 件、brief は「対応が必要な項目はありません」だった。実行器は dry-run（`kimeru/actions.py:18-20`）なので、何も実行されず誰にも知らされない。plan 成立時だけ翌朝の brief に手順が出る。writer の有無で挙動が変わる点（`cli.py:74-87`）も未文書。
  - 直し方: 承認不要の「[kimeru 通知]」区分を作り、advice 付き decide を自分チャットへ投稿する（番号空間は承認と分ける）。`brief.collect` に直近 24 時間の decide+advice を入れ、重大を最上位に固定する。上の 3 イベントを `tests/test_daily.py` の回帰テストにする。

- **5 分ごとに Teams を自分チャットへ切り替える（確認待ち 0 件でも）** — `tools/teams-self.ps1:138-152,209`、`kimeru/daily.py:66`、`kimeru/notify.py:162`
  - 何が起きるか: `collect` は pending の有無を見ずに `bridge.read()` を呼ぶ。read / post は必ず `Open-SelfChat` を通り、`Select()` で切り替えて元に戻さない。5 サイクルで read 5 回を再現した。投稿は 1 サイクルの上限が無く、キュー 10 件なら 10 回連続で前面化を試みる。入力中・会議中・画面共有中の判定は無い。
  - 【要確認】切り替え後に打鍵が自分チャットへ流れるか、非表示実行からの `SetForegroundWindow` が拒否されるかは実機未検証。
  - 直し方: posted かつ pending/held があるときだけ collect する。`GetLastInputInfo` で無操作 60 秒以上、`SHQueryUserNotificationState` が BUSY / PRESENTATION でないことを確認し、満たさなければ次サイクルへ延期する。選択中のチャットを覚えて戻す。1 サイクルの投稿は 3 件まで。`schedule` に pause / resume を足す。

- **既定の `setup-company.cmd install` が PowerShell 5.1 で必ず失敗する** — `tools/setup-company.ps1:103-104`
  - 何が起きるか: ADO 未指定だと `$extra` が空文字列になり、5.1 は空引数を捨てる。argv は `--extra` で終わり、argparse が exit 2、スクリプトはロールバックする。空白入りのプロジェクト名も `unrecognized arguments` で失敗する。`setup-company.cmd` は 5.1 固定で、`docs/company-pc-test.md:49` の既定手順そのもの。`tests/test_setup.py` は `cli.schedule` を直接呼ぶので検出できない。
  - 直し方: 引数を配列で組み、値があるときだけ `--extra` を足す。根本的には `--ado-org` / `--ado-project` を専用引数にする。偽 python で ps1 を通す回帰テストを足す。

- **Kev へのループバック通信が社内プロキシへ送られる** — `kimeru/backends.py:44-46`
  - 何が起きるか: 既定 opener のため、`HTTP_PROXY` か Windows の手動プロキシがある PC では `POST http://127.0.0.1:<port>/v1/systemone` と state 本文がプロキシへ出る。Kev には届かない。プロキシの応答が 403 なら `done/*.error` へ退避され、503 / 407 なら受信箱に残ったまま無言で待ち続ける。`backends.py:70-72` と `README.md:66` の「PC の外に出ない」に反する。PAC のみの環境は対象外。
  - 【要確認】`setup-company.ps1:43` の `Invoke-WebRequest` は迂回するため「Kev 応答 OK」と食い違う見込みだが、静的確認のみ。
  - 直し方: ループバック宛ては `build_opener(ProxyHandler({}))` を使う。`KIMERU_KEV_URL` が非ループバックなら明示フラグなしで拒否する。`HTTP_PROXY` を設定した単体テストを足し、月曜の T14 より前に入れる。

- **安全網の正規表現が普通の障害報告を外し、機能要望と検証環境を P1 にする** — `graphs/ado_workitem.json:149-165`、`graphs/monitor_alert.json:354-359`
  - 何が起きるか: 取りこぼし側は「できなくなっています」「出来ない」「落ちています」「Production is down」「is unavailable」が規則 no になり、モデル任せになる。受け入れ条件の無い 1 行の障害報告は ready=no から `request_info` に進み、priority を通らない。誤爆側は決定的で、「Export all users to CSV」「クラッシュレポートの収集機能を追加」「ログイン画面でコピーができない」が `set_p1_critical` になる。exclude は「…環境」の形だけで、ステージングで / STG / UAT / dev / sandbox は P1 になる。ホールドアウトの規則一致は ADO 0/8、アラート 0/12 で、安全網は未知の文面で未測定。
  - 直し方: 「範囲の手がかり × 故障の語幹」の組で判定する。英単語には単語境界を付け、`all users` / `outage` 単独では yes にしない。Bug 以外での一致は人へ回す。exclude を拡張する。当たるべき / 当たってはいけない 50 文以上の表形式テストを置き、ホールドアウトに規則対象の事例を入れる。

- **時刻ラベルが変わるだけで同じメッセージが再イベント化される** — `kimeru/pull.py:190,208,216-219`
  - 何が起きるか: sig は `sha1(time|preview)`。time を 17:40 → 昨日 → 金曜日 → 9/25 と変えると毎回発行され、`daily.cycle` 通しで [kimeru #1]〜[#4] の 4 投稿になった。`OWN_PREFIXES` に「自分:」が無く、自分の発言が相手発のイベントになる。メンション印が残る間は雑談も 1 通ずつイベントになり、author はグループ名になる。
  - 【要確認】実 Teams のラベルの種類と自分の発言の接頭辞は実機未確認。
  - 直し方: sig と ID は (chat id, 正規化した preview) だけから作る。自分の接頭辞は `teams-self.ps1` の `$markers` と定義を共有する。メンションは False→True のサイクルの 1 回だけ発行する。3 ケースを `tests/test_pull.py` に追加する。

- **入力欄に何か残ると投稿が永久に失敗し、キュー全体が黙って止まる（前回 2 番の修正が不完全）** — `tools/teams-self.ps1:257-270`、`kimeru/notify.py:111-116`
  - 何が起きるか: 貼る前の空確認も失敗時の後始末も無い。不一致で Fail しても貼った文が残り、次回は追記されて二度と一致しない。notify は最初の失敗で raise し、3 サイクルとも先頭 1 件しか試行しなかった。通知先は `daily.log.jsonl` と終了コードだけ。貼り付けのみモードは posted が立たず、`collect` は OK を無視して毎回貼り直す（`notify.py:114,166,176`、`daily.py:77`）。`docs/company-pc-test.md:227-228` の「notify → notify --send」は必ず失敗する。
  - 【要確認】検証者の 1 名は、貼り付けモードの件を手動手順限定として P3 と評価した。
  - 直し方: 貼る前に入力欄が空でなければ `box_not_empty` で中止する。貼り付けから送信までを try/finally で後始末する。同じエラーが 3 回続いたらトーストと brief 先頭で知らせる。投稿長は 3,500 字で切る（`GetText(4000)`、218 行）。貼り付けモードは 1 件だけにするか廃止し、手順書 T9 を直す。

- **Teams の入力は一覧の「最後の 1 行プレビュー」で、評価が想定する形と違う** — `kimeru/pull.py:208-220`、`graphs/teams_chat.json:56-57,133,155`
  - 何が起きるか: text はプレビュー、author はチャット名。連投の末尾「よろしくお願いします」だけが見え、fyi なら `log_fyi` で依頼が kimeru から消える。none / unsure なら「返信＋Task 起票」の承認依頼になる。プレビューが 3 回変わると同じ会話が 3 回判断され、返信 3 通と Task が計画された。グループでは Task 題名が「（チャット名さんの依頼）」になる。fixture の Teams 48 件は全て全文形で、teams-ui 形は 0 件。前回 5 番の Teams 側は未対応。
  - 【要確認】グループのプレビューが「名前: 」で始まること、切り詰め長は実機未確認。元メッセージは Teams に残るため、検証者の 1 名は P2 と評価した。
  - 直し方: `source=teams-ui` の match を先頭に置き、fyi / none / status は自動終端へ流さず「新着あり・最後の 1 行のみ」の通知にする。同じ chat_id は 1 項目に集約する。プレビュー先頭の「名前:」を author に分離する。teams-ui 形の fixture で Kev を測り直す。

- **「正しい行動」はラベルの無いノードでモデル自身の回答を正解に流用している（696c90c の修正は不完全）** — `eval/e2e.py:85-92,107-108`
  - 何が起きるか: Oracle が need_* / due_* / first をモデル回答で埋めるため、actions 比較は同語反復になる。teams-1 で正反対の plan 2 通りがどちらも correct だった。何を答えても correct になる件が 14/99 と 5/48。StubBackend で 47/99 と 20/48（Kev は 62/99 と 35/48）。ホールドアウトは Teams が intent、ADO が ready しかラベルが無く、priority / urgency / playbook は 0 件。
  - 直し方: fixture に `expect_terminal` / `expect_priority` / `expect_playbook` / 期限帯を直接書く。ラベル無しのモデル判断を通った件は unverified として correct から外す。README の表に Stub とランダムの基準値を併記する。

- **fixture の state が本番の state と違う** — `kimeru/events.py:68,164-165`、`kimeru/pull.py:26-28,148,218-220`
  - 何が起きるか: 本番の ADO は priority / area / created_by が state に入り、判断モデルへ渡る（ADO の既定値は 2）。fixture 147 件では 0 件。アラート fixture 37 件は全て説明に影響が平文で書かれているが、本番の pull は context を signalType と alertState に削り、説明は空になりうる。`pull.py:12` 自身が live tenant 未実行と書いている。
  - 【要確認】priority=2 がモデルをどちらへ寄せるか、空の説明で page_advice が増えるかは未測定。
  - 直し方: fixture を「生ペイロード → `events.normalize` → state」の経路で作る。月曜に実イベント 20〜30 件の state を匿名化して保存し、1 回だけ測る。

## P2

### グラフと入力

- **ADO グラフが起票者の Priority を読まずに上書きし、同じ値を判断材料に渡す** — `graphs/ado_workitem.json:26-40,115`、`kimeru/events.py:68`
  - 何が起きるか: Priority 1 の Bug が priority スコア 0.6 で `set_p3` になり、無通知・承認なし。type / created_by を見るノードが無く、Task や Epic にも `request_info` が発火する。`System.Tags` は固定値。現版は dry-run で、ボードには出ない。
  - 直し方: `ado.update` を有効にする前に、「下げない・2 段以上の変更は人へ」の guard、種別フィルタ、`ado.add_tag` を入れる。fixture に priority=1/2/3 を入れて再測定する。
  - 検証者の注記: 「priority を判断材料から外す」と「起票者の値を尊重する」は方向が逆なので、どちらかに決めること。

- **重大度ガードが impact にしか無く、Sev0/Sev1 が future_risk 経由で P3 に落ちる** — `graphs/monitor_alert.json:73-79,146-150,219`、`kimeru/graph.py:90-92`
  - 何が起きるか: Sev0「SQL storage at 97%…」は future_risk=yes、horizon<3 で `prevent_backlog`（無通知）。noul ノードには guards を付けられない。`tune_rule` は queue=false・actions 無しで、説明が空の Sev2 が何も残さず終わる。
  - 【要確認】発火は模擬回答によるもので、Kev が実際にそう答えるかは未確認。
  - 直し方: `critical_outage` の直後に severity の match を置き、Sev0/1 は最低でも `page_advice` にする。`tune_rule` は queue=true か P3 Task にする。説明が空のアラートは人へ回す。

- **議事録は箇条書きの形が少し違うだけで行が無言で消える** — `kimeru/events.py:74,80`、`kimeru/daily.py:38-40`、`graphs/meeting_item.json:66-76,155`
  - 何が起きるか: 「・決定：」「■」「●」「１．」「①」「【決定】」の 8 行がイベントにならなかった。0 件のファイルは done へ移り、ログも出ない。担当・期限は Task に入らない。リスク 1 行で Issue 1 + Task 5 が確認なしに計画される。
  - 直し方: NFKC 後に記号類を任意で剥がし、空でない行は全て項目にする。読み取り N / 項目 M / 無視 K を出し、0 件は `.empty` に置く。担当・期限を名前付きグループで取り出す。per_step に上限を付ける。

- **議事録の重複排除キーが行番号ベースで、編集して再投入すると新しい行が捨てられる（前回 P1-4 の dedup は議事録に対して誤り）** — `kimeru/events.py:83`、`kimeru/cli.py:69-71`、`kimeru/daily.py:40`
  - 何が起きるか: 先頭に 2 行追記して同名で再投入すると、新しい「中止決定」「顧客連絡」は 0 件で、旧 2 行が二重判断される。末尾への追記だけなら正常。title と date が同じ別会議でも衝突する。done/ の同名ファイルは上書きされる。
  - 直し方: id を `sha1(title|date|正規化した行本文)` にする。done/ へはタイムスタンプ付きで移す。編集再投入の回帰テストを足す。

### Teams ブリッジと承認

- **書き込み先の確認がウィンドウタイトルの正規表現だけ** — `tools/teams-self.ps1:61-68,139,200,231`
  - 何が起きるか: `-match` は大文字小文字を区別せず、「Sato Ken (ME)」「Design review (you)」が True になる。真なら 48:notes の選択を確認せずに貼って送信する。取り消せない誤送信になる。先頭の ms-teams ウィンドウを取るだけで、別アカウントやポップアウトを区別しない。
  - 【要確認】到達条件は稀。別テナントへの書き込みは未検証。
  - 直し方: 直前条件を「`title-chat-list-item_48:notes` が存在し `IsSelected`」にする。`-cmatch` と学習済み表示名の完全一致を要求する。ウィンドウが複数で決められなければ中止する。

- **承認ループが閉じていない** — `kimeru/notify.py:67,112-115,184`、`tools/teams-self.ps1:224-227,293`、`README.md:109`
  - 何が起きるか: `posted=True` の根拠は入力欄が変わったことだけ（前回 2 番の送信後確認は未実装）。「OK 7。」「OK 7 8」「承認 7」は黙って捨てられる。投稿文「承認で実行」と README「OK N で送信」は dry-run と矛盾する。「修正 N」の失敗時は毎サイクル writer を呼び直す（`kimeru/writer.py:69` の timeout は 180 秒）。
  - 直し方: 送信後に read で P:N を確認してから posted にする。受理・不受理を次の投稿の先頭 1 行で返す。句読点と複数番号を受理する。投稿文を「承認で実行予定（現在は記録のみ）」に直す。

- **承認しても何も起きない** — `kimeru/notify.py:184`、`kimeru/cli.py:80`、`kimeru/actions.py:18-20`
  - 何が起きるか: OK N は planned を 1 行増やすだけ。定型文のときは返信文・題名・リンクが出ず、type 名だけ（`notify.py:67`）。PM は承認後に同じ作業を手でやり直す。
  - 【要確認】意図的な v0.1 の範囲であり、検証者の 1 名は P1 と評価した。
  - 直し方: 承認済みの `ado.comment` / `ado.update(Priority)` の 1 種類だけ、action 種別ごとの opt-in で本物にする。間に合わなければ位置づけを「仕分けて下書きを渡す」に変え、コピーできる返信全文・ADO の URL・起票題名を投稿に載せる。

- **承認の UX が件数に耐えず、効果を測る記録が弱い** — `kimeru/notify.py:19,63-67,87-91`、`kimeru/brief.py:49,59-63`
  - 何が起きるか: 返信は完全一致のみ。1 件 1 投稿で通し番号が増え続け、pending に期限が無い。plan の手順は 2 日で brief から消える。`approvals.log.jsonl` に時刻が無い。
  - 検証者の訂正: `record.at` と `daily.log.jsonl` から 5 分粒度で復元はできる。ただし集計コマンドは無い。
  - 直し方: パーサを `OK 3 4 5` / `OK 3-5` / `NG 5 理由` に広げる。サイクルごとに 1 投稿へまとめ、7 日で失効させる。`posted_at` / `answered_at` を足し、`kimeru stats` を作る。月曜の試行前に合格ラインを決める。

- **Teams を読めない状態が「新着 0 件」と区別できない** — `tools/teams-self.ps1:172-195`、`kimeru/pull.py:201,211`、`kimeru/daily.py:59`
  - 何が起きるか: 予定表タブ、ポップアウトが先頭、AutomationId の変更のどれでも `ok:true, chats:[]` で正常終了する。brief にも出ない。初回ポーリングは未読を基準に吸収する。learn は T3 失敗時しか呼ばれず、メンション検出は通常無効。前回 5 番の Teams 側は未対応。
  - 【要確認】`メンション|mentioned` の文字列は実機未確認。
  - 直し方: 項目 0 または ID 取得 0 は `ok:false` にし、連続失敗と最終成功時刻を `pull_state.json` に残す。brief と次の投稿の先頭に「読めなかった時間帯」を出す。導入時に必ず learn する。

- **クリップボードを壊す** — `tools/teams-self.ps1:258-265`
  - 何が起きるか: 退避は `GetText()` だけで、画像・ファイルは `Clear()` で消え、書式と数式は失われる。260 行で上書きした後、261 / 263 行の `Assert-Foreground` が Fail すると復元に届かず、投稿文が残る。未投稿がある限り 5 分ごとに繰り返す。
  - 【要確認】実機再現は未実施。前面化が通らないと確認できれば P1 相当。
  - 直し方: `GetDataObject` で全形式を退避し、try/finally で復元する。履歴除外フォーマットを付ける。`GetClipboardSequenceNumber` で本人のコピーを検知する。`ValuePattern.SetValue` が使えるかを月曜に確認する。

- **投稿が描画範囲から外れると、その番号への返信は無視され続ける** — `kimeru/notify.py:133-143`、`tools/teams-self.ps1:276-309`、`tests/test_notify.py:98`
  - 何が起きるか: 「新しい投稿 30 件 + OK 57」は採用されず pending のまま。解除手段も無く、brief が古い pending で埋まる（`brief.py:57-63,82`）。
  - 【要確認】Teams が古い投稿を UIA ツリーから外す件数は未確認。検証者の 1 名は P1 と評価した。月曜に「連続 30 件投稿後に残る P: の数」を測れば確定する。
  - 直し方: 鮮度の根拠を「前回処理した最後の返信より後」に変える。採否を 1 行返す。古い pending は失効させる。

### writer（既定は無効、全て opt-in 前提）

- **承認投稿に元メッセージ・送信者・送信先・定型文が出ない** — `kimeru/notify.py:61-72`、`kimeru/cli.py:82`
  - 何が起きるか: `reply_status` の投稿は ID と下書きだけ。`record.summary` と `template_text` は使われていない。PM は何への返事かを知らずに OK を返す。前回の「製品改善の順序 2」は未実装。
  - 【要確認】検証者の 1 名は P1 と評価した。
  - 直し方: 送信者、チャット種別、元メッセージ、定型文、下書きされた説明文を常に出す。見出しを「文面の確認」に分ける。

- **writer のプロンプトにデータと指示の境界が無い** — `kimeru/writer.py:30,50`、`kimeru/events.py:16`
  - 何が起きるか: 本文中の「PM からの修正指示:」の行が本物と区別できない（偽・真の 2 行が並ぶことを再現）。長さ上限も無い。
  - 【要確認】LLM が従うかは未検証。独立行を作れるのは Graph 形の入力に限る。ADO 由来は writer の対象外。
  - 直し方: 信頼できない値は `json.dumps` した 1 ブロックに閉じ、PM 指示は別キーで渡す。長さを制限する。出力側のガード（P3 の JSON 抽出の項）と必ず組にする。注入 fixture を追加する。

- **`timeout=180` が `.CMD` シム経由では効かず、writer が記録より前に同期実行される** — `kimeru/writer.py:80-81`、`kimeru/cli.py:74,80,85,327`
  - 何が起きるか: timeout 2 秒の指定で 8.2 秒待たされた。固まると判断の記録と通知が出ず、VBS の完了待ちで後続サイクルも止まる。アラートの `page_planned` でも writer が呼ばれる。
  - 直し方: `Popen` と `taskkill /T /F` でツリーごと止める。順序を「記録 → 文面不要の実行 → writer」に変える。`monitor.alert` では writer を呼ばない。1 サイクルの合計予算を置く。

- **社外送信のスイッチが見えない環境変数 1 つ** — `kimeru/writer.py:90-92`、`tools/setup-company.ps1:46-57`、`docs/company-pc-test.md:66`
  - 何が起きるか: `KIMERU_WRITER` は backend=kev と無警告で両立する。status・ログ・投稿のどこにも状態が出ない。未知の名前は無言で None になる。`README.md:111` の「会社契約の経路」は実装が無い。
  - 直し方: `--writer` の明示フラグにして `schedule status` に出す。kev とクラウド writer の併用は許可フラグが無ければ拒否する。`company-check` で変数が設定済みなら警告する。未知の名前はエラーにする。

- **LLM が書いたチケット説明は承認を一度も通らない** — `kimeru/cli.py:77,87`、`kimeru/writer.py:30,47,112-118`
  - 何が起きるか: held は `teams.reply` だけ。`ado.create` の description は LLM 文で上書きされ、原文が消える。`page_planned` では 6 件が無承認になる。アラートは材料が空のまま「メッセージ由来の具体名を使う」と指示している。
  - 直し方: `drafted_by` 付きのアクションは全て保留にして説明文も投稿に出す。原文は description に残し、LLM 文は別フィールドへ入れる。本文の無い種別では writer を呼ばない。

### 評価

- **「誤った行動 0」は採点規則の産物** — `eval/e2e.py:106,112-113`
  - 何が起きるか: 経路のどこかに unsure があれば、確信して間違えた終端も fallback になる。alert-4 が `tune_rule`（ideal は bug_p1）、teams-1 が `decision_later` で fallback。fallback は needs_human=False で人に届かない。
  - 【要確認】Kev の 12 件中何件がこの型かは、結果ファイルが無く確認できない。
  - 直し方: 最初に分岐したノードの edge が unsure のときだけ fallback にする。2 例を `tests/test_e2e.py` に追加して測り直す。

- **ホールドアウトは「調整に未使用」ではない** — `README.md:85`、`eval/README.md:38,49`、`eval/e2e.py:128`
  - 何が起きるか: f9ad8cd、c9c5406、2160260 の各コミットで、質問文の選定と guard の設計に使われている。guard を足す前の初回実測（重大な取りこぼし 1）は `eval/README.md` にしか無い。
  - 直し方: 表の名称を「開発用 99 / 検証用 48（質問文と guard の設計に使用済み）」に改め、初回実測を併記する。本当の試験セットは graphs を見ていない人か、月曜の実イベントで作る。

- **「重大な取りこぼし 0」はモデルをほとんど試していない** — `eval/e2e.py:38,116`、`graphs/ado_workitem.json:150`
  - 何が起きるか: 重大 12 件中 10 件は judge を通らず、規則だけで決まる。全問最低と答える backend でも取りこぼしは 2/99 と 1/48。`SEVERE` に Teams の `incident_bug` が無い。
  - 【要確認】検証者の 1 名は、「モデルに任せない」設計の帰結であり、評価の網羅不足だと指摘した。
  - 直し方: 規則の語彙を使わない重大事象の言い換えセットを 50 件以上作る。規則とモデルの recall を分母付きで分けて出す。

- **発表の見出しが調整用 99 件の数値で、区間・基準値・グラフ別の内訳が無い** — `docs/presentation.md:58`、`README.md:87`、`eval/run_eval.py:98-120`
  - 何が起きるか: 「約 4 割を人に」は 25+12 の合算で、実際に人へ届くのは 25/99。検証用の誤り 1/48（95% 上限 11.1%）に触れていない。
  - 直し方: 見出しを検証用 35/48（区間 58〜85%）、誤り 1、人へ 12 にする。Stub の基準値とグラフ別の表（`e2e.py:141-148`）を併記し、「架空データ N=48、実データ未検証」と明記する。

- **公開している Kev の数値を repo から再現・監査できない** — `eval/e2e.py:70-71`
  - 何が起きるか: Kev の回答と結果が未コミットで、e2e に再採点モードが無い。ラベル範囲内の回答（ado-1 の priority=1.8）が e2e では wrong、`run_eval` では ok になる。本番経路に乗らないラベル付き質問が 39/175。前回の「評価数値の扱い」は未対応。
  - 直し方: `--record` / `--replay` を足し、`eval/results/kev-<日付>-<commit>.jsonl` をコミットする。score ラベルは「許容する終端の集合」にする。

### 運用

- **タスクが AC 電源限定の既定値で登録される** — `kimeru/cli.py:329-330`
  - 何が起きるか: バッテリー駆動では開始されず、切り替え時に停止される。サイクルが起動しないのでログも残らない。停止が貼り付け後なら入力欄の残骸になる。
  - 【要確認】schtasks で登録した実タスクの XML は未確認。影響は遅延で、検証者の 1 名は P1 と評価した。
  - 直し方: `Register-ScheduledTask` で `AllowStartIfOnBatteries` / `DontStopIfGoingOnBatteries` / `StartWhenAvailable` を設定し、実行時間の上限を 15 分にする。チェックに「電源を抜いて 1 サイクル」を追加する。

- **止まっていても PM から見えない（前回 P1-4 の終了コード修正は不完全）** — `kimeru/cli.py:260-261,327`、`kimeru/daily.py:41-48,77-79`、`kimeru/backends.py:48-61`、`tools/setup-company.ps1:47,57`
  - 何が起きるか: VBS が `Run` の戻り値を捨て、wscript は常に 0（実測）。Kev 停止中は judge:0、exit 0 で、brief「対応が必要な項目はありません」が送信された。応答途中の切断（ConnectionReset）や JSONDecodeError / KeyError は `done/*.error` へ退避され、戻すコマンドが無い。az の失効も通知されない。
  - 直し方: `WScript.Quit …Run(…)` にする。ConnectionError 系は `BackendUnavailable` に分類する。`process_inbox` が waiting / parked を返し、error を立てる。`health.json` と連続失敗時の状態通知を 1 通出す。`kimeru retry` を足す。

- **チェックの結果シートにアカウント名・ADO 組織名・サブスクリプション ID が載り得る（e07b67c の匿名化は T3-diag と T12 だけ）** — `tools/company-check.ps1:21,205,220,264-277,335`、`kimeru/cli.py:182`、`kimeru/pull.py:73`
  - 何が起きるか: T10 は inbox のフルパスと失敗 URL をそのまま載せる。Ctrl+C では削除が走らず、実データが一時フォルダに残る。T8 は `--backend` 未指定で `KIMERU_BACKEND=kev` を拾う。`docs/company-pc-test.md:16` の「人名は入らない」は保証されていない。
  - 直し方: T10 は件数だけを抜く。全 Rec に共通のサニタイズ（パス / GUID / URL）を通す。try/finally で削除する。`--backend stub` を明示する。

## P3

- **「unsure は必ず助言へ」は 14 ノード中 9 で偽。validate の検査に穴がある（前回 P2 の修正は不完全）** — `graphs/teams_chat.json:102`、`kimeru/graph.py:25-26,71,75,125,212-217`
  - 何が起きるか: urgency の unsure が `decision_today`（「本日中に判断して返信します」）に着く。`yes_at=0.3/no_at=0.7` の逆転、`yes_at=7`、未知の action type、`{event.auther}`、plan を通らない per_step が全て読み込まれる。同梱 4 グラフには該当なし。
  - 【要確認】検証者の 1 名は P2 と評価した。
  - 直し方: validate に、unsure の行き先制約（例外は理由を明記）、`0 <= no_at < yes_at <= 1`、`actions.KNOWN` との照合、既知フィールドとの照合を足す。urgency の unsure は「期限を確認」の advise にする。

- **本文の「OK n」が kimeru 自身の投稿を経由して承認として読まれる** — `tools/teams-self.ps1:256,286-308`、`kimeru/notify.py:52,64-71,142`
  - 何が起きるか: 本文の行「OK 1」「OK 2」で、無関係な保留 #1 まで approved になった。read は投稿本文と返信を区別しない。
  - 【要確認】検証者の 1 名は既定構成では不到達とした。もう 1 名は、送信者が 1 行に「バックスラッシュ+n」を打つだけで既定構成でも成立すると再現した。実 UIA では未確認。実行器を有効にした時点で P1 相当。
  - 直し方: advice と下書きの各行に引用記号を付ける。read はメッセージ一覧の部分木かつ [kimeru 投稿の外の行だけを返信にする。推測できない照合符号を使う。本文中のバックスラッシュを保護する。

- **ブリッジの失敗が解読不能、`\n` の受け渡しが本文を壊す** — `kimeru/notify.py:43-45,52`
  - 何が起きるか: スクリプト実行前の失敗は cp932 で落ち、JSONDecodeError だけが残る。UNC パス `\\fileserver\new\notes.txt` が 3 行に割れ、全文照合も通過する。
  - 直し方: bytes で受けて utf-8 → cp932 の順に復号し、終了コードと先頭 200 字を例外に入れる。本文は `-TextFile` か Base64 で渡す。

- **番号 N が保存先ごとの連番** — `kimeru/notify.py:78,82,88-91,142`、`tools/company-check.ps1:228`
  - 何が起きるか: out フォルダ 2 つと 1 チャットで、1 通の「OK 1」が両方の #1 を承認する。`approvals.json` を消すと 40 件を再投稿する。
  - 【要確認】検証者の 1 名は、金曜のデモで手動実行と自動運転が重なると起きるとして P2 と評価した。
  - 直し方: 導入固有の照合コードを入れる（例「[kimeru #12 k7f]」）。開始番号を乱数にする。out にロックファイルを置く。

- **状態ファイルが非アトミック書き込み** — `kimeru/notify.py:82,111-116`、`kimeru/pull.py:92`、`kimeru/daily.py:69,79`
  - 何が起きるか: 壊れると notify / approvals / brief が毎サイクル JSONDecodeError で、自動復旧しない。`daily_state.json` の破損は `_step` の外で落ちる。投稿と保存の間で止まると二重投稿になる。
  - 【要確認】頻度は低い。検証者の 1 名は P2 と評価した。
  - 直し方: `pull._drop`（`pull.py:103-105`）と同じく一時ファイル + `os.replace` にする。`.bak` から復元する。決着済みの項目は archive へ移す。

- **判断の一時障害扱いに上限が無い（前回 4 番の「上限付き再試行」は未実装）** — `kimeru/daily.py:41-45`、`kimeru/events.py:69,105`
  - 何が起きるか: 毎回タイムアウトするファイルが 1 つあると、名前順で後ろの alert / teams が止まる（模擬で 12 サイクル再現）。
  - 【要確認】検証者の 1 名は Kev の実測で反証した。2 万字は即 422 で `.error` に退避され、上限未満は prefix キャッシュで次サイクルに回復する。一方、長文が通知なしに `.error` へ入る問題は残る。
  - 直し方: ファイルごとの試行回数を持ち、3 回で退避して確認待ちに載せる。タイムアウトは continue にする。state の本文を上限で切る。

- **writer の隔離フラグが不足** — `kimeru/writer.py:77-81`
  - 何が起きるか: `--tools ""` と `--strict-mcp-config` だけでは、ユーザー設定のフック・プラグイン・個人 CLAUDE.md・環境変数が入る。env の継承は実行で確認した。
  - 【要確認】フックや CLAUDE.md が実際に効くかは未検証。検証者の 1 名は P2 と評価した。
  - 直し方: `--restricted`（または空の `--setting-sources`）を追加し、env は最小集合にする。init イベントで 0 件を確認するスモークテストを足す。README の表現を弱める。

- **「修正 N」の失敗が無言。書き直しの基準が前回の LLM 文になる** — `kimeru/notify.py:147-154`、`kimeru/writer.py:45`
  - 何が起きるか: 失敗しても投稿もログも出ない。`template_text` が LLM に渡らず、定型文へ戻す手段が無い。
  - 直し方: `a.get('template_text') or a['text']` を使う。失敗を投稿する。返信語「定型 N」を足し、回数に上限を置く。

- **JSON 抽出が壊れやすく、「作り話をしない」は実行時に未検証** — `kimeru/writer.py:56,116`、`eval/drafts.py:24,44`
  - 何が起きるか: 貪欲一致のため、後続の波括弧文で None になる。タイトルの全角半角の差で説明が無言で捨てられる。承認文の捏造は数字を含まず、検出できない。
  - 直し方: `raw_decode` で最初の有効な dict を採用する。tasks のキーは連番にする。`apply` に確約語の検査と、Kev の noul 1 問によるガードを入れる。

- **「1 件 3.6 秒」は 1 リクエストを開発機で測った値** — `README.md:87`、`eval/run_eval.py:167-172`、`docs/company-pc-test.md:34`、`eval/README.md:33`
  - 何が起きるか: 本番は 1 イベントで最大 3〜4 回の逐次呼び出しになる。文書間で 2.5 / 3.6 / 3.9 秒と不一致。
  - 直し方: イベント単位の p50 / p95 / 最大をグラフ別に出す。月曜の会社 PC の実測値と機種名に差し替え、数値は 1 か所に集約する。

- **自動化の対象が逆** — `graphs/teams_chat.json:102,133-142,207-215`、`graphs/meeting_item.json:155`、`kimeru/report.py:102`
  - 何が起きるか: PM 名義の「ボードをご確認ください（kimeru 自動返信）」が decide になっている。1 通で返信 1 + Task 5、リスク 1 行で起票 6 件。brief が同じ plan の手順で埋まる。
  - 【要確認】現版は dry-run で不到達。ライブ化の前に必須。
  - 直し方: `teams.reply` は全て下書き → 承認にする。per_step は親 1 件のチェックリストにする。起票前に同題を検索する。

- **発表の語りが実態より強い** — `docs/presentation.md:45,46,58,64`、`README.md:111`
  - 何が起きるか: 「OK で実行され」は dry-run。「クラウドに送るものはありません」は Kev かつ writer 未設定の場合だけ。まとめの「安全側で決定 1」を台本が省略している。Kev の録画は plan 成立 0 回で、現行グラフより古い（ef4145f / 577c337 / 2160260 より前）。
  - 直し方: 条件付きの文言へ書き換える。録画を現行グラフで取り直す。

- **導入手順が複数人への展開に耐えない** — `docs/company-pc-test.md:29,34-35,39`、`tools/prepare-company.ps1:29-32`、`tools/setup-company.ps1:17`
  - 何が起きるか: バンドル作成スクリプトが無く、開発 PC からのコピーを案内している。常駐メモリは約 10GB（fp32 で約 14GB）。
  - 検証者の訂正: `README.md:66-74` に上流からの導入手順がある。社内で通るかは T13 で確認する。
  - 直し方: バンドル作成を tools に入れる。結果シートに CPU 種別と常駐メモリを足す。

- **105 件のテストは壊れる層を通らない** — `tests/fake-teams-self.ps1:21-29`、`tests/test_bridge_e2e.py:61-63`、`tests/test_daily.py:79-80`
  - 何が起きるか: 実 ps1 の構文解析テストが無い。report / watch / digest / `ClaudeWriter._call` は未試験。偽ブリッジは 1 行目しか見ず、状態を持たない。返信の正規表現が 3 か所に重複している（`notify.py:19-20`、`teams-self.ps1:293,299`、fake:29）。
  - 直し方: 全 ps1 の `ParseFile` テストと `-Action status` のスモークテストを足す。返信の解析は Python に一本化する。Kev の録音回答で回帰テストを作る。

- **配布と保守性** — `pyproject.toml:10-18`、`kimeru/graph.py:278-284`、`kimeru/plan.py:29-37`、`kimeru/report.py:114`、`kimeru/cli.py:39-40`
  - 何が起きるか: graphs が 0 件でも validate / run は無出力の rc=0 になる。daily では inbox が判断なしで done に移る。絶対パスの `--out` が HTML に埋まる。未採用の実験用 backend（ClmBackend）、表示名の不一致（`demo.py:17` と `report.py:13`）、版数の分散がある。
  - 検証者の訂正: `.gitattributes` は存在する。LF なのは作業ツリーの一部だけ。
  - 直し方: 0 件ロードは `GraphError` にする（最優先）。scripts を外すか package-data にする。`process` を pipeline モジュールに集約する。

- **検証対象外の申し送り（ops の所見のみ、【要確認】）**
  - inbox/done・approvals.json・decisions.jsonl が同僚のプレビューと氏名を無期限に平文で保持し、削除手段が無い。`docs/company-pc-test.md:69-75` の事前確認表は保存に触れていない。
  - 時刻の基準が decisions（UTC）、daily.log（ローカル）、approvals.log（なし）で不統一。
  - 月曜のチェックは VBS + pythonw + タスク登録の経路を一度も通さない。
  - 非表示の wscript → pythonw → `powershell -ExecutionPolicy Bypass` がキー送信を行う構成は、EDR の検知対象になりうる。情報セキュリティ部門の事前了解が要る。

## 見落とされている前提

| 作者が置いている前提 | 反する証拠 |
|---|---|
| 確信して決めたものは知らせなくてよい | 実行器は dry-run で、確信した経路は何も起きず誰も知らない（`graph.py:243`、`actions.py:18-20`） |
| unsure は必ず人に落ちる | 14 ノード中 9 で decide へ届く（`graph.py:25-26` の docstring と不一致） |
| 規則は未知の文面も拾う安全網 | 活用形・別表記・英語で外れ、ホールドアウトの規則一致は 0 |
| プレビューはメッセージ全文、チャット名は送信者、時刻ラベルは不変 | `pull.py:208-220` |
| 評価の state は本番の state と同じ | priority・空の説明・teams-ui 形は fixture に 0 件 |
| ホールドアウトは未使用、誤った行動 0 は精度 | git 履歴と `e2e.py:106` |
| Teams は kimeru が自由に使える資源で、PM は操作中でない | チェック手順は「触らないでください」が前提（`docs/company-pc-test.md:163`）。常時運転にはその前提が無い |
| Kev なら PC の外に出ない | プロキシ経由（`backends.py:46`）と見えない writer スイッチ（`writer.py:91`） |
| 人の承認が歯止め | 承認投稿に元情報が無い。本文の行が承認として読まれうる。番号空間が衝突する。LLM の説明文は承認を通らない |
| 終了コードで失敗が見える、テストが緑なら動く | VBS が戻り値を捨てる。PS 5.1 の導入経路は未試験 |
| PC は AC 電源につながっている | 登録タスクは AC 限定の既定値（`cli.py:329`） |
| 議事録は `docs/minutes-format.md` の形で来る | 日本語の「・項目」（空白なし）は無言で消える |

## 改善の順序（月曜・金曜・その後）

### 月曜（9/28 会社 PC 確認）の前に直す

1. `setup-company.ps1` の引数を配列化する。
2. `backends.py` でループバック宛てはプロキシを無効にする。
3. `collect` を pending があるときだけにし、無操作判定、投稿上限、入力欄の空確認、クリップボードの try/finally を入れる。
4. sig から time を外し、自分の発言の接頭辞を共有する。
5. VBS を `WScript.Quit` にし、judge の waiting / parked を error にし、brief に「未判断 N 件」を出す。
6. `company-check` の T10 をサニタイズし、try/finally と `--backend stub` の明示を入れる。
7. `KIMERU_WRITER` が未設定であることを確認する。

### 月曜に実機で測ること

- 別チャットで入力中に 1 サイクル走らせる。
- 予定表タブのまま chats を実行する。
- 画像をコピーした状態で post する。
- ポップアウトや会議ウィンドウがある状態で動かす。
- 連続 30 件を投稿した後、timeline に残る P: の数を数える。
- 48:notes の AutomationId が使えるか確認する。
- グループのプレビューの形と切り詰め長、自分の発言の接頭辞と翌日の時刻ラベルを記録する。
- 自分宛てメンションを 1 件作り、検出できるか確認する。
- 電源を抜いて 1 サイクル動くか確認する。
- VBS + pythonw + タスク登録の経路を通す。
- プロキシ設定の有無を記録する（値は出さない）。
- `ValuePattern.SetValue` が使えるか確認する。
- Kev のイベント単位の p50 / p95 を測る。
- 実イベント 20〜30 件の state を匿名化して保存し、1 回だけ測る。

### 金曜（10/2 発表）まで

1. advice 付き decide を通知する。
2. 安全網を語幹化し、表形式テストを置く。
3. ADO の「priority を下げない」guard を入れる。
4. Sev0/1 を `future_risk` の前で固定し、`tune_rule` を queue=true にする。
5. 議事録の箇条書き、id、0 件時の扱いを直す。
6. teams-ui の fyi / none / status を通知へ縮小する。
7. e2e の fallback 定義を修正して再測定し、基準値と区間を併記する。
8. 投稿文、`README.md:85,87,109,111`、`presentation.md:45,46,58,64` を訂正する。
9. 録画を現行グラフで取り直す。
10. 発表中は `schedule` を pause する。

### 金曜に言ってはいけないこと

- 無条件の「重大な取りこぼし 0」「誤った行動 0」。
- 「ホールドアウトは調整に未使用」。
- 「約 4 割を人に回す」（人に届くのは 25/99）。
- 「クラウドに送るものはありません」（Kev かつ writer 未設定のときだけ）。
- 「OK で実行される」「当番を呼びます」。
- 「1 件 3.6 秒」（会社 PC の実測に差し替える）。
- 「Teams のメッセージを読んで判断」（最後の 1 行プレビューのみ）。
- 「裏で静かに動く」「全社員に配布できる」。

### 金曜に言えること

- 架空データの範囲で動作を確認した。
- 迷えば人に回す設計が働く。
- ADO・当番呼び出し・相手への返信は試し実行である（自分チャットへの投稿だけは実送信）。
- 次は PM 3 人・2 週間の試行で、合格ラインは投稿数、NG 率、応答時間で決める。

### その後

1. 承認の照合コード、送達確認、受理通知を入れ、状態ファイルをアトミック化する。
2. validate を強化する。
3. 評価を作り直す（終端の直接ラベル、Kev 録音のコミット、重大事象の言い換えセット）。
4. writer の境界、出力ガード、承認範囲、プロセス隔離を入れる。
5. 承認済み ADO 更新の 1 種類だけをライブ化する。
6. Register-ScheduledTask 化、health 通知、保存期間と削除手段を入れる。
7. PowerShell のテストと CI を整え、パッケージングと死んだコードを整理する。

## やめる／縮める候補

- **`--send` 付きの 5 分自動運転** — 無操作判定、48:notes の選択確認、照合コードが入るまで凍結する。Teams 連携は「読み取りと、本人が前面で実行する投稿」に限る（`teams-self.ps1:209`）。
- **貼り付けのみモード** — 承認に到達できず、入力欄を詰まらせる（`notify.py:114`）。廃止するか、1 件だけにする。
- **writer（`claude -p`）** — 実験機能に降格し、会社 PC と本番デモでは無効にする。`README.md:111` の未実装の経路の記述は削る。
- **`reply_status` の自動返信と、期日を約束する decide の返信** — PM 名義で出る設計であり、ライブ化した瞬間に拒否される（`teams_chat.json:102,133`）。
- **per_step による Task 量産** — 親 1 件のチェックリストに縮める（`teams_chat.json:207`、`meeting_item.json:155`、`monitor_alert.json:209`）。
- **Teams プレビューからの decision / plan / 起票判断** — 本文を読めていない。「新着通知」に縮める（`pull.py:213-220`）。
- **無通知の終端（`tune_rule`、`log_fyi` への直行）** — 発報中のアラートや依頼が何も残さず消える（`monitor_alert.json:146-150`）。
- **Oracle 方式の e2e を見出し数値に使うこと** — 補助指標に下げる（`e2e.py:85-92`）。ノード単位の 175 問も、本番経路に乗る質問だけを分母にする。
- **手動 CLI の既定 `--out out` と自動運転の併用** — 番号空間が衝突する（`notify.py:88-91`）。ロックするか、片方に寄せる。
- **死んだコード** — `graph.match_node`、旧 replies 経路（`notify.py:131-132`）、`pyproject.toml` の scripts 宣言。
- **「全社員に配布」という目標** — PM 3 人・2 週間の試行に下げる。

## 追補: 見落とし点検の結果

本文をまとめた後、別の検証者が「本文が見落としていること」と「本文の言い過ぎ」を点検した。言い過ぎ 3 件は本文に反映済み。
見落としの 1 と 2 は P1 相当で、本文の「確信した判断を自分チャットへ通知する」という直し方の前提を崩す。月曜の実機確認で最初に確かめること。

### 見落とし（本文に無い指摘）

検証方法: HEAD e07b67c を読み取り専用で確認。unittest 105 件成功を再実行で確認。StubBackend・偽 http・偽ブリッジ・ローカルの偽プロキシ・PowerShell 5.1 で再現した。実 Teams・Kev の実推論・ロック画面は未実行。

#### 1. 自分チャットへの投稿は iPhone にプッシュ通知されない前提が抜けている（P1 相当）

- 根拠: `tools/company-check.ps1:160`（「自分宛てのメッセージなので iPhone に通知は来ないことがあります」）、`docs/company-pc-test.md:180`（通知が来なくても続行）、`docs/presentation.md:44`（「iPhone に通知が来ます」と断言）、`graphs/monitor_alert.json:162`（「5分以内に人が確認」）。
- 何が起きるか: 投稿者は PM 本人のアカウントなので、通知が出ない可能性をリポジトリ自身が認めている。`page_advice` が投稿されても、PM が自分チャットを開くまで誰も気づかない。
- なぜ効くか: レビューは「iPhone に届くのは迷った雑件だけ」と、届く前提で書いている。P1 先頭の直し方（advice 付き decide を自分チャットへ通知）も同じ穴に落ちる。
- 状態: 【要実機確認】T5 の「iPhone通知=あり/なし」が製品成立の分岐点だが、合否条件になっていない。
- 直し方: 通知なしなら別経路（本人宛てメール、Teams のアクティビティ通知など）を用意するまで「iPhone で承認」を主張しない。

#### 2. PC がロック・スリープ中は投稿できない（P1 相当）

- 根拠: `tools/teams-self.ps1:49-56,243-246,260-264`（前面化と SendKeys が必須、クリップボード上書きは前面確認より前）、`kimeru/cli.py:329-330`（5 分ごと常時起動、ロック判定なし）。
- 何が起きるか: PM が離席して PC が自動ロックされると、前面化とキー送信が通らず投稿は全て失敗する見込み。蓋を閉じればサイクル自体が走らない。iPhone が必要な時間帯ほど何も届かない。
- レビューとの衝突: 推奨の「無操作 60 秒以上で投稿」は、ロック直前の短い窓に投稿を寄せることになる。
- 状態: 【要実機確認】月曜の測定項目に「Win+L でロックして 1 サイクル」「蓋を閉じて 10 分」が無い。

#### 3. チャット一覧に現れただけの古い・既読のチャットが新着イベントになる（再現済み）

- 根拠: `kimeru/pull.py:209-217`（`prev` が無ければ発行）、`pull.py:220`（`unread` は記録のみで条件に使わない）、`tools/teams-self.ps1:173`（画面に出ている項目だけを返す）。
- 再現: 基準 3 件の後、既読・時刻 9/1 の 1:1 チャット 5 件を追加した一覧を渡すと、5 件すべてがイベントになった。
- 何が起きるか: PM が一覧をスクロールする、検索する、Teams を再起動するたびに、数週間前の会話が判断される。`ask_pm` に落ちれば、その数だけ自分チャットに投稿される。
- 追加の懸念: ボットとの 1:1 も同じ扱いになる可能性がある。【要実機確認】
- 直し方: 初見の chat id は `unread` が真のときだけ発行する。時刻ラベルが当日でないものは基準として吸収する。

#### 4. 判断記録に backend・モデル・グラフ版が無く、既定は stub（再現済み）

- 根拠: `kimeru/cli.py:72-85`（記録項目）、`cli.py:69`（重複排除キーにグラフ版はあるが backend は無い）、`cli.py:114`（既定 stub）、`cli.py:322`（stub なら `--backend` を登録しない）。
- 再現: `decisions.jsonl` のキーは actions / advice / at / event_id / event_kind / executed / graph / needs_human / node / outcome / path / summary だけ。stub で判断した後は、同じイベントを 2 回目に流しても 0 件で、Kev では再判断されない。
- 何が起きるか: 環境変数が反映される前の端末で `python -m kimeru daily` を実行すると、キーワード一致の stub が実メッセージを確信度 0.85 で判断する。記録上は Kev の判断と区別できない。
- なぜ効くか: レビューが勧める「PM 3 人・2 週間の試行」「`kimeru stats`」は、どのモデル・どの版のグラフの結果かを分けられない。
- 直し方: 記録に backend / model / graph_version / profile を入れる。`daily` と `schedule install` は stub を明示指定なしでは拒否する。

#### 5. アラートは解消が届かず、同じルールの連続発報をまとめない（再現済み）

- 根拠: `kimeru/pull.py:171`（Fired のみ取得）、`pull.py:177`（既知 id は捨てる）、`graphs/monitor_alert.json:6-18`（`resolved` ノード）、`kimeru/notify.py:63`（投稿 2 行目に event_id をそのまま出す）。
- 再現: 同一ルールの発報 12 件で、受信箱 12 件、判断 12 件、投稿 12 通になった。全件が解消した次のポーリングは 0 件で、`log_resolved` には pull 経路から到達しない。
- 何が起きるか: 2 分で自動復旧したアラートの「5分以内に人が確認」が確認待ちに残り続ける。フラッピング 1 本で、Teams の前面化が 12 回起きる。
- 副作用: アラートの event_id は ARM のフルパスなので、サブスクリプション ID を含む文字列が自分チャットに毎回投稿される。
- 直し方: 確認待ちのアラートは次サイクルで状態を再取得し、解消済みなら自動で取り下げる。ルールと対象リソースが同じものは 1 件に集約する。投稿には短い ID を使う。

#### 6. 受信箱がファイル名順の直列処理で、承認の読み取りも判断の後ろに並ぶ

- 根拠: `kimeru/events.py:104-105`（sorted）、`kimeru/daily.py:37-40,64-66`（judge → notify → approvals の順）、`kimeru/pull.py:108`（ADO の初回は 24 時間分）、`kimeru/backends.py:76`（タイムアウト 300 秒）。
- 確認: 並び順は「議事録 .txt → ado- → alert- → teams-」になる。
- 何が起きるか: 初回、または計画会議で Task が一括起票された直後は、ADO の判断が終わるまで Sev0 アラートも Teams も判断されない。iPhone から返した OK も読まれない。
- レビューとの違い: レビューはタイムアウト時の停止だけを挙げている。正常時の順序と、サイクル全体の時間予算が無い点は未指摘。
- 状態: 所要時間は【要実機確認】。1 イベントは 1〜4 回の逐次呼び出しになる。
- 直し方: approvals を先頭に移す。種別の優先順を alert → teams → ado → 議事録にする。1 サイクルの判断件数か時間に上限を置く。

### 本文の言い過ぎ（反映済み）

#### 1. プロキシ経由時の失敗の型が言い過ぎ

- 引用: 「Kev には届かず、`RuntimeError`（再試行不可）で全イベントが `done/*.error` に入る」
- 実測（ローカルの偽プロキシ、`HTTP_PROXY` 設定）:

| プロキシの応答 | 例外 | 結果 |
|---|---|---|
| 403 | RuntimeError | `.error` へ退避 |
| 503 | BackendUnavailable | 受信箱に残り、無言で待ち続ける |
| 407 | BackendUnavailable | 同上 |

- 根拠: `kimeru/backends.py:49-57`。502 / 504 も同じ分岐に入る。
- 正しい範囲: プロキシへ state 本文が送られる点は確認できた（偽プロキシが `POST http://127.0.0.1:8009/v1/systemone` を受信）。失敗の型はプロキシの応答次第で 2 通りある。
- 影響: 後者は `judge:0`、exit 0 で、レビュー自身が P2 に挙げた「止まっていても見えない」側に入る。`.error` を探しても何も見つからない。

#### 2. 「外部書き込みは全て dry-run」は自分チャットへの投稿を除外している

- 引用: 「外部書き込みは全て dry-run 固定で誤爆が外に出ない」、「外部への書き込みは全て試し実行である」（金曜に言えること）
- 根拠: dry-run なのはグラフのアクションだけ（`kimeru/actions.py:18-20`）。自分チャットへの投稿は実際のキー送信で行われる（`tools/teams-self.ps1:246,264`、`kimeru/notify.py:111`）。
- 矛盾: レビュー自身が P2 で「取り消せない誤送信になる」と指摘している。投稿文には同僚のメッセージ本文が入る（`graphs/teams_chat.json:166`）。
- 正しい言い方: 「ADO・当番呼び出し・相手への返信は試し実行。自分チャットへの投稿だけは実送信で、宛先確認に既知の弱点がある」。

#### 3. ClmBackend は死んだコードではなく、validate の記述も一部言い過ぎ

- 引用: 「死んだコード — ClmBackend（`cli.py:39-40`）」、「validate はしきい値も終端も検査しない」
- ClmBackend: `--backend clm` で選べる（`kimeru/cli.py:114`）。`eval/e2e.py:36,126,130` と `kimeru/profiles.py:25` が参照している。削除すると e2e の import が壊れる。正しくは「実験用で未採用の backend」。
- validate: judge ノードの `*_conf` は検査している（`kimeru/graph.py:71-73`）。`min_conf=7` は拒否されることを確認した。
- 実際に未検査のもの（受理を確認）: `split_at=9`、plan ノードの `min_conf=7` / `due_min_conf=-3` / `need_at=5`、`queue` キーの無い advise。最後のものは無通知の終端になる。レビューが挙げる `yes_at` / `no_at` は実行していないが、検査条件（`graph.py:71`）に該当しない。
- 補足: `graph.match_node` は本番経路で未使用（テストのみ）で、この部分の指摘は正しい。
