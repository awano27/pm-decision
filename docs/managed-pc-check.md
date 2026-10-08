# 管理された PC 動作確認手順

## 自動の確認（ダブルクリック 1 回。何も送らない・質問しない）

`run-auto-check.cmd` をダブルクリックすると、人の操作が要らず、何も送らない確認をまとめて実行します（数分。テストの全件を含む）。

- 中身: 下の「更新の確認」（U0〜U9）と、`run-check.cmd` のうち T0・T1・T3・T6・T7・T8・T12・T13・T14・T16・T23
- 質問にはすべて「いいえ」と答える扱いで、何も送らず、チャットを開かず、何も取得しない。Teams と Kev は起動しない（起動していれば、Teams は読み取りだけ、Kev はこの PC の中で判断）
- 結果は OK/NG と件数だけで、クリップボードと `kimeru-auto-check-result.txt` に入る。末尾に、手で確かめるもの（自分とのチャットへの投稿とスマホからの返信、通知の経路、ADO へのコメント、チャットを開いて読む、文面の下書き）の実行方法が出る
- 結果には PC の環境（Windows・Teams・Python の版、メモリなど）が入る。共有する相手を選び、公開の場所には貼らない

## 更新の確認（ダブルクリック 1 回、送信なし）

最新版を持ち込んだら、まず `run-update-check.cmd` をダブルクリックする。今回の変更をまとめて確かめる（約 15 秒。Kev でのデモを選ぶと数分）。

- 確かめること: デモが `out\demo` に書き `out\` の記録に触れない（U1）、2 回目のデモ（U2）、デモ以外の記録があるフォルダでは消さずに止まる（U3）、`--fresh` は記録を移すだけ（U4）、`run` で書いたフォルダはデモに消されない（U5）、判断モデルに接続できないときは 1 行で止まり何も作らない（U6）、任意で Kev でのデモ（U7）、自動運転の登録時に「実際に投稿する」と表示する（U8。タスクは登録しない）、`run-check.cmd` の T13 が外部への到達確認の前に聞く（U9。N で送らない）、この PC の `out\` と `%LOCALAPPDATA%\kimeru` が変わっていない（U0）
- 何も送らず、Teams にも触れない。一時フォルダと専用の状態フォルダだけを使い、この PC の kimeru の設定と鍵は外して動かす（設定済みの writer も呼ばない）
- 自動運転（daily）が動いていると U0 が NG になることがある。止めてから実行する
- 結果は OK/NG と件数だけで、クリップボードと `kimeru-update-check-result.txt` に入る

## かんたん実行（おすすめ）

持ち込むのはフォルダ 1 つ（分割して運ぶ）、操作はダブルクリック 1 回。

1. **開発 PC**: kimeru・Kev・Azure CLI・Python を 1 つにまとめ、1GB ずつに分割する。
   ```powershell
   powershell -ExecutionPolicy Bypass -File tools\make-bundle.ps1 -Zip -KevSrc <Kev の持ち込み用フォルダ> -AzSrc <az の ZIP 展開先>
   powershell -ExecutionPolicy Bypass -File tools\split-zip.ps1 -Zip C:\develop\kimeru-pc.zip
   ```
   `C:\develop\parts` に分割ファイル（約 8 個）と `JOIN.cmd` ができる。Kev だけ・kimeru だけの更新なら、変わった部分の小さな ZIP を作って上書き展開してもよい。
2. **管理された PC**: 組織で認められた方法で `parts` フォルダを**フォルダごと**好きな場所にコピーする。途中で失敗したら、失敗したファイルだけ取り直す。
   前回の `C:\kimeru-pc` が残っていれば、先に削除する。
3. **管理された PC**: `JOIN.cmd` をダブルクリック。結合 → 破損チェック（`NG` と出たら、そのファイルを取り直す）→ `C:\kimeru-pc` へ展開（約 2 分）→ `START.cmd` の順に自動で進む。
4. `START.cmd` が Kev を起動（最小化ウィンドウ。閉じない）し、az のサインインと動作確認に進む。**質問には明示的に答える**（Enter だけだとその項目は「SKIP」になり、確認したことにならない）。
   - az にサインインしていません → `y`（ブラウザで職場・学校のアカウント）
   - 確認待ち 2 件を自分とのチャットに送信します → `y`。その後、数秒はマウス・キーボードに触らず、iPhone から表示された `OK 番号` と `NG 番号` を **別々のメッセージで** 返信する
   - 自分とのチャットが見つからないとき → Teams で「自分とのチャット」を手で開く（90 秒待つ）
   - ADO の組織名 → `https://dev.azure.com/<組織>/<プロジェクト>` の形で貼ってよい（サインインを聞かれたら `y`）
   - GitHub Copilot / Teams の Copilot に架空のサンプルを書かせますか → 試すなら `y`（Copilot の履歴に残る）
   - PC の通知が見えましたか → 見えたら `y`
5. 終わると結果シートが**クリップボードにコピー**される（`kimeru-check-result.txt` にも保存）。共有する前に目を通す。

結果シートには本文・人名・キーは入らない（件数・OK/NG・エラー文のみ）。ただし、失敗したときのエラー文に組織名などが含まれることがあるので、貼る前に目を通す。一時ファイルと画面構造の出力は自動で削除される。

- 従来どおり `C:\kev`・`C:\az` に置いた場合もそのまま動く（`kimeru` の隣 → `C:\kev` / `C:\az` の順に探す）
- 自動運転を登録したあとは、1 サイクル（5 分）待って `python -m kimeru --out <出力先> schedule status` を実行し、`settings in effect` に writer などの出どころ（file）が出ていることを確かめる
- 特定の確認だけやり直す: PowerShell で `.\run-check.cmd T9`（複数指定可: `T9 T15 T16`。M365 Copilot の出典は `T18`）
- 準備だけ: `.\prepare-bundle.cmd` ／ 確認だけ: `.\run-check.cmd managed`
- Kev が遅い（1 件が数分）ときは、Kev の窓に `precision: fp32` と出ているか確認する。bf16 に対応しない CPU では、起動時に自動で fp32（メモリ約 14GB）になる
- 自動運転（`setup-managed.cmd install`）は、Teams を 5 分ごとに切り替える問題を直すまで使わない

### ローカル判断モデル（kev）を使う場合

社外にデータを出さない判断モデル kev（Kev-4B、Apache-2.0。土台の Qwen3.5-4B-Base も Apache-2.0）を、開発 PC で作った持ち込み用フォルダ（約 11GB）で動かす。インストール・管理者権限・ネット接続は不要（`run-check.cmd` の T13 は外部 3 サイトへの到達確認を含むが、確認してから送り、断っても RAM・CPU・空き容量の確認は行う）。

1. 開発 PC の `C:\develop\kev-bundle` を、組織で認められた方法で管理された PC の `C:\kev` にコピーする
2. `C:\kev\start-kev.cmd` をダブルクリック。`Uvicorn running on http://127.0.0.1:8009` と出たら準備完了（ウィンドウは開いたまま）
3. kimeru フォルダで `.\run-check.cmd managed`（T14 で kev による判断を確認）
4. 常用するなら `setx KIMERU_BACKEND kev`（ユーザー環境変数。管理者権限不要）

目安（開発 PC: Ryzen 9 7940HS の CPU のみ、既定の bf16）: メモリ約 10GB。判断モデルへの 1 回の呼び出しが中央値 3.9 秒、1 イベントで約 20 秒（管理された PC の fp32 では 1 イベント約 45 秒。[動作確認の状況](evaluation.md#動作確認の状況)）。
bf16 命令のない CPU で遅い場合は、`set KEV_DTYPE=fp32` してから `start-kev.cmd` を実行する（メモリ約 14GB）。

### Azure DevOps のチケットを取り込む場合（az のインストール不要版）

1. 開発 PC の `C:\develop\az-bundle\az` を管理された PC の `C:\az` にコピーする（約 260MB、Microsoft 公式の ZIP 版を展開したもの）
2. `C:\az\bin\az.cmd login` でサインイン（Azure portal と同じ職場・学校のアカウント）
3. `.\run-check.cmd managed` の T10 で、組織名・プロジェクト名を入れるとチケットの取り込みと判断まで確認できる
4. 常用するなら `.\setup-managed.cmd install -AdoOrg <組織> -AdoProject <プロジェクト>`

`C:\az` 以外に置いた場合は、環境変数 `KIMERU_AZ` に `az.cmd` のパスを入れる。

### 毎日動く状態にする（確認が OK だったら）

> 注意: 自動運転は、投稿のときに Teams を前面に出すことがあります。使う場合は 1〜2 サイクル様子を見て、作業の妨げになるなら `.\setup-managed.cmd remove` で止めてください。

```powershell
.\setup-managed.cmd install          # Kev の自動起動・KIMERU_BACKEND=kev・5 分ごとの自動運転（管理者権限不要）
.\setup-managed.cmd status           # 状態の確認（Kev の応答・最後のサイクル）
.\setup-managed.cmd remove           # 元に戻す（データは %LOCALAPPDATA%\kimeru に残る）
```

- 自動運転は画面を出さずに動き、記録は `%LOCALAPPDATA%\kimeru` に置く（ZIP を取り直しても消えない）
- Kev が起動する前に届いたイベントは受信箱に残り、Kev の起動後に判断される
- Kev の場所が `C:\kev` 以外なら `-KevDir <場所>`、間隔を変えるなら `-Minutes 10`

#### 時間を限って試す（試験運転）

毎日 `daily --once` を手で動かす代わりに、自動運転を時間を限って入れ、終わったあとに共有できる結果ファイルを 1 つ受け取る。

```powershell
.\setup-managed.cmd trial                                  # 8 時間、5 分ごと。時間が来たら自分で止まる
.\setup-managed.cmd trial -Hours 4 -Minutes 10 -Sample     # 時間・間隔を変える / 架空の作業項目を 1 件入れる
.\setup-managed.cmd trial -AdoOrg <組織> -AdoProject <プロジェクト> -Sample   # ADO の新しい作業項目も読む（読むだけ）
.\setup-managed.cmd status                                 # 試験運転の残り時間
.\setup-managed.cmd remove                                 # 途中でやめる（そのときも結果ファイルを作る）
.\setup-managed.cmd report                                 # 結果ファイルを作り直す（クリップボードにもコピー）
```

- `install` と同じものを入れ、期間（開始・終了予定・間隔・ADO の有無）を `%LOCALAPPDATA%\kimeru\trial-autorun.json` に記録する。実行すると、何が起きるか（何分ごとに自分のチャットへ投稿するか、いつ自分で止まるか、早く止める方法、ADO へは何も書かないこと）を日本語で表示する
- 終了予定の時刻に、1 回だけ動くタスク `kimeru-trial-end`（管理者権限不要・非表示）が `remove` と同じ後始末をして結果ファイルを作る。PC が止まっていた場合は、動き出し次第すぐ実行する（スリープ中でも、次に起きた時点）。それでも動かなかったときは、次の `status` か `report` で止める
- `-Sample`: 架空の ADO 作業項目（番号 999001、題名は「試験:」で始まる、情報不足）を 1 件入れる。最初のサイクルで、項目の行と ADO のリンクを付けた確認待ちとして自分のチャットへ投稿されるので、内容を確かめて `NG 番号` と返信して閉じる。結果ファイルの件数には入らない（`sample` の行に別に出る）。以前の試験で取り込み済みなら入れない
- 結果ファイル `kimeru-autorun-result.txt`（kimeru のフォルダ。クリップボードにも入る）は、件数・OK/NG・短い記号だけで、本文・題名・名前・組織・プロジェクト・パスは書かない。サイクルの実行数と見込み、1 サイクルの所要時間（中央値・最大）、時間切れ、判断役（Kev）が使えなかったサイクル、失敗した手順、取り込んだ種類・判断の内訳・P1 通知・自分のチャットへの投稿と返信の数、Teams を開いた・戻せなかった回数、朝のまとめの有無が並ぶ。末尾の判定は、サイクルが 0・失敗した手順がある・Kev が半数を超えて使えない・配信結果不明がある・Teams の表示を戻せなかった、のどれかで NG になる
- 記録のない項目（Teams を前面に出した回数）は「記録なし」と出る。作業中に画面が切り替わらなかったか、スマホに投稿が届いたかは、結果ファイル末尾の一覧を見ながら目で確かめる
- 実行中に `trial` をもう一度実行すると、期間が新しくなる。ADO への書き込み（設定 `execute`）を有効にしている場合は、承認した件が実際に書かれるので、試験運転の前に確かめる

以下は同じ内容を手動で 1 つずつ行う場合の手順（かんたん実行が途中で止まったときの切り分け用）。

## 手動手順

kimeru が管理された PC（組織のアカウントの Teams・iPhone・組織の ADO / Azure）で動くかを段階的に確かめる手順。
上から順に進め、NG が出た段階で止めて「結果シート」を返す。

- 所要時間: レベル 0〜1 で 30〜40 分、レベル 2〜3 はそれぞれ 10〜20 分
- PC の外へ出る項目は次のとおり。どれも実行前に確認し（`y/N`、または入力。Enter だけなら送らずに SKIP）、それ以外の項目は PC の中だけで動く
  - 宛先が自分だけ: T5・T9・T20（自分とのチャットへの投稿）、T21（設定した通知の経路への試験の 1 行。件数も本文もなし）
  - 宛先が外部のサービス（架空のサンプルだけを送る）: T11（Jev）、T15（GitHub Copilot）、T17（Teams の Copilot。履歴に残る）
  - 宛先が外部のサービス（あなたが入力した語を送る）: T18（M365 Copilot に、会議名かメールの件名の一部。結果シートには書かない）
  - 組織のサービスへの書き込み: T22（試験用の作業項目に ADO コメント 1 件）
  - 到達確認・取得: T13（pypi.org・github.com・huggingface.co へ HEAD。本文なし）、T6（python.org からの Python 取得。Python が無いときだけ）、T10（az のサインインと ADO・アラートの読み取り）
- 本文・人名・トークンは結果シートに書かない（件数・OK/NG・エラー文だけ）

## 0. 始める前に（必ず確認）

| # | 確認すること | 理由 |
|---|---|---|
| 0-1 | 組織のルールで、GitHub からのスクリプト取得と PowerShell スクリプトの実行が許されているか | 許されていなければここで中止 |
| 0-2 | Teams の画面内容を自動で読み取ることが組織のルールに反しないか | T1〜T3 は画面の構造を読む |
| 0-3 | Jev（TypeSafe、社外クラウド）へ業務データを送ってよいか | 未確認なら T11 は**架空データのみ**で実施 |
| 0-4 | 定期の自動運転（`daily`）を動かしているなら、止めてあるか（止めなくても当たらないが、念のため） | `tools\check.ps1` の試験（番号は T5 が 100〜998、ほかが 100〜898 の乱数）の投稿は `[kimeru 試験 #N]` で始まり、`daily` は承認の投稿にも区切りにも使わず、その下の返信も試験のものとして読み捨てる。iPhone の返信は、試験の投稿より下に打つこと |
| 0-5 | 状態フォルダを使う Windows アカウントは 1 つだけか | ロックの持ち主を調べる処理は、開けないプロセスを kimeru ではないと見なす。別のアカウントが同じ状態フォルダを使うと、生きたロックを奪う |

## 1. 準備

1. GitHub の `awano27/pm-decision` を開き、**Code → Download ZIP** で取得して任意のフォルダ（例: `C:\work\kimeru`）に展開する
   （git が使えるなら `git clone https://github.com/awano27/pm-decision.git kimeru` でもよい）
2. PowerShell を開き、展開したフォルダへ移動する

```powershell
cd C:\work\kimeru
```

3. 以降のコマンドはすべてこのフォルダで実行する

## レベル 0: PowerShell だけで確認（インストール不要）

### T0 環境チェック

```powershell
$PSVersionTable.PSVersion
Get-ExecutionPolicy -List
$ExecutionContext.SessionState.LanguageMode
Get-AppxPackage -Name MSTeams | Select-Object -ExpandProperty Version
```

| 見るところ | OK の条件 | NG のとき |
|---|---|---|
| PSVersion | 5.1 以上 | 記録して続行 |
| LanguageMode | `FullLanguage` | `ConstrainedLanguage` なら **T1〜T5 は動かない**（組織ポリシー）。結果を返して中止 |
| MSTeams のバージョン | 何か表示される（新しい Teams） | 何も出なければ旧 Teams。記録して続行 |

### T1 Teams の画面構造を読めるか（読み取りのみ）

1. Teams を開き、どれかの**チャット**を表示する
2. 実行:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\probe-teams.ps1 -Label chat
```

3. `wrote probe-chat.txt (N elements, M ms)` と出れば OK。N が 100 未満なら Teams の読み込み完了を待って再実行
4. `probe-chat.txt` を開き、1 行目の `elements=` と `types:` の行だけ結果シートに写す

### T2 Copilot の会議まとめ画面を読めるか（読み取りのみ）

> 不要になった: 議事録は Copilot の要約を手で `.txt` にして渡す（[minutes-format.md](minutes-format.md)）。持ち込み検証の確認セットからも外した。

1. Teams で Copilot のまとめ（要約）がある会議を開き、**まとめ（要約）タブ**を表示する
2. 実行:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\probe-teams.ps1 -Label recap
```

3. `probe-recap.txt` の `copilot/recap-like UI labels:` の下に何行出たかを記録
4. 可能ならアクション項目を展開して `-Label recap2` でもう一度
5. **`probe-recap.txt` は中身を確認してから共有**（ボタン名に参加者名が入ることがある。気になる行は消してよい）

### T3 自分とのチャットを見つけられるか（読み取りのみ）

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\teams-self.ps1 -Action status
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\teams-self.ps1 -Action open
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\teams-self.ps1 -Action read
```

| コマンド | OK の出力 |
|---|---|
| status | `{"teams":true,"ok":true,...}` |
| open | `{"ok":true,"selfChatOpen":true}`（Teams が自分とのチャットに切り替わる） |
| read | `{"ok":true,"posts":[...],"replies":[...]}` |

`self chat not found` が出たら、Teams の**チャット**タブを表示してから open を再実行。それでも出る場合は次を実行して結果を返す（名前は伏せ字で出る）:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\teams-self.ps1 -Action diag
```

見つからない場合は、Teams で自分とのチャットを**一度だけ手で開いて**から次を実行する（表示名をこの PC のローカルにだけ覚え、以後は自動で開ける）:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\teams-self.ps1 -Action learn
```

`parenMarkers` に自分とのチャットの印（例: `自分`）が出ている場合は、`-SelfMarker 自分` を付けても試せる。

### T4 入力欄への貼り付け（送信しない）

**実行中の数秒間はマウス・キーボードに触らない。**

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\teams-self.ps1 -Action post -Text '[kimeru 試験 #0] 判断が必要（管理PCテスト）\n返信: OK 0 / NG 0 / 保留 0'
```

- `{"ok":true,"typed":true,"sent":false}` で、自分とのチャットの入力欄にテスト文が入っていれば OK
- `Teams is not the foreground window` が出たら、Teams をクリックして前面に出してから再実行

### T5 送信と iPhone からの返信（外部送信あり: 宛先は自分のみ）

1. 送信:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\teams-self.ps1 -Action send
```

2. **iPhone** の Teams に通知が来るか確認（来なくても自分とのチャットに表示されていれば記録して続行）
3. iPhone の Teams で自分とのチャットを開き、`OK 0` と返信する
4. PC で読み取り:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\teams-self.ps1 -Action read
```

5. `"replies":["OK 0"]` と `"posts":["0"]` が出れば OK

## レベル 1: Python で kimeru 本体を確認

### T6 Python の有無

```powershell
python --version
py --version
```

どちらかで 3.10 以上が出れば OK（以降 `python` を出た方に読み替える）。
`Python` とだけ表示される場合は、Windows 標準の「Microsoft Store を開くだけのショートカット」で、Python は入っていない。
その場合は**かんたん実行**（`run-check.cmd`）を使うと、確認のうえ python.org のインストール不要版（zip 展開のみ・管理者権限不要）を
このフォルダの `.python` に置いて続行できる（手動なら以降の `python` を `.\.python\python.exe` に読み替える）。

### T7 テストと検証（外部通信なし）

```powershell
python -m unittest -q
python -m kimeru validate
```

- `OK` と、`ok  playbook ...` 8 行 + `ok  ...` グラフ 4 行が出れば OK
- 失敗したら、最後の 20 行を結果シートに貼る

### T8 サンプルで判断を動かす（外部通信なし）

```powershell
python -m kimeru --out out-test run examples\teams_chat.json examples\monitor_alert.json examples\ado_workitem.json examples\meeting_minutes.json
python -m kimeru --out out-test brief
```

- `[DECIDE]` / `[ADVISE/人の確認]` の行と `plan:` の行が出れば OK
- brief は `[kimeru brief 日付] 今日の進め方` で始まる 3 項目が出れば OK

### T9 承認の流れ（T5 が OK の場合のみ。外部送信あり: 宛先は自分のみ）

```powershell
python -m kimeru --out out-test notify            # 貼り付けのみ。入力欄を目で確認
python -m kimeru --out out-test notify --send     # 送信
```

T8 のサンプルでは確認待ちが 2 件できるので、`[kimeru 試験 #N]` と `[kimeru 試験 #N+1]` の 2 通が投稿される（試験の印。N は乱数の番号）。
iPhone から `OK 1` と `NG 2` を**別々のメッセージで**返信してから:

```powershell
python -m kimeru --out out-test approvals
```

`#1 -> approved` と `#2 -> rejected` が出れば OK（`execute` を設定していない限り、承認された処理は記録のみで、実際には何も書き込まれない。T9 は `execute` を設定していない状態で行う）。
もう一度 `approvals` を実行して何も出なければ、二重処理しないことも確認できる。

`--send` を付けない `approvals` は、返信を読んで承認を記録するだけです。承認のあとに出る投稿（実行の結果、`[kimeru 送信用 #N]`）は、入力欄に**貼り付けるだけで、送信しません**（送信待ちとして残り、次の `approvals --send` か `daily --send` で、まとめて 1 回送られます）。T9 と T20 の確認は、`--send` なしで行い、投稿が入力欄に貼られることを目で見ます。送信まで確かめるときだけ `approvals --send` にします。
`approvals` は同時に 1 つだけ動きます。定期の自動運転が動いている最中に打つと、「another approvals run is in progress」と出て、何も読まず、何も承認しません（少し待って打ち直します）。

### T20 聞き返し・下書きの読み取り（任意）

`.\run-check.cmd T20` で、架空の確認待ち 2 件に「聞き返し N」「下書き N <文面>」を返信し、Teams の画面から読めるかを確かめます。手順の中の `approvals` も、T9 と同じく `--send` なしです。返信を読んで記録するだけで、承認のあとの投稿は貼り付けだけです。

## 通知の試験（任意。外部送信あり: 宛先は自分のみ）

```powershell
.\run-check.cmd T21
```

- 環境変数と設定ファイルのどちらで設定した経路も試します。経路が未設定なら、確認の質問も送信もせず、SKIP と記録します
- 固定の 1 行（件数も本文もありません）を、設定した経路に送ります。iPhone や指定した先に届いたかを聞かれます
- 設定は [iPhone への通知](push-notification.md) を見てください

## レベル 2: Azure CLI で取り込み（任意）

### T10 ADO と監視アラートの取り込み（読み取りのみ）

```powershell
az --version
az login
# 例: https://dev.azure.com/contoso/Payments なら組織名 contoso、プロジェクト名 Payments（自分の値に置き換える）
python -m kimeru --out out-test pull ado --org "contoso" --project "Payments" --inbox inbox-test
python -m kimeru --out out-test pull alerts --subscription (az account show --query id -o tsv) --inbox inbox-test
python -m kimeru --out out-test watch inbox-test --once
```

- `az` が無い・`az login` が組織の設定で拒否される → 記録して終了
- `ado: N new -> inbox-test` が出れば OK（直近 24 時間に作成されたチケット数）
- `HTTP 401/403` → 権限不足。エラー文を記録
- watch で各チケット・アラートの判断結果が出れば OK（試し実行のみ）

## レベル 3: Jev で判断（任意。0-3 の確認後）

### T11 Jev バックエンド（外部送信あり: 宛先は Jev）

キーは**この PowerShell セッションだけ**に設定する（ファイルに書かない・チャットに貼らない）:

```powershell
$k = Read-Host -Prompt "TypeSafe API key" -AsSecureString
$env:TYPESAFE_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($k))
python -m kimeru --backend jev --out out-jev run examples\teams_chat.json
```

- `plan: スケジュール変更の判断 / 1.遅延原因の復旧見込みを担当者に確認（今日） → ...` のような行が出れば OK
- 実データで試すのは 0-3 で許可を確認してから

## 後片付け

```powershell
Remove-Item -Recurse out-test, out-jev, inbox-test -ErrorAction SilentlyContinue
Remove-Item probe-*.txt
```

自分とのチャットのテスト投稿は、必要なら Teams 上で手動削除する。

## 結果シート（これをコピーして埋めて返す）

```
T0  PS=      LanguageMode=         Teams=
T1  OK/NG  elements=     types=
T2  OK/NG  recap系ラベル数=     （recap2: ）
T3  status=    open=    read=
T4  OK/NG  エラー:
T5  送信=    iPhone通知=あり/なし    read replies=
T6  python=
T7  OK/NG  （失敗時は最後の20行）
T8  OK/NG
T9  OK/NG  approvals出力=          2回目の出力=
T10 az=    login=OK/拒否    ado件数=    alerts件数=    エラー:
T11 OK/NG  エラー:
T20 OK/NG/SKIP  （聞き返し・下書きの読み取り）
T21 OK/NG/SKIP  届いた=はい/いいえ
T22 OK/NG/SKIP  コメント 1 件だけ=はい/いいえ
T23 OK/NG/SKIP  （プレビューの長さの分布。文字数だけ）
T24 OK/NG/SKIP  （開いて読む。既読になる）
気づいたこと:
```
