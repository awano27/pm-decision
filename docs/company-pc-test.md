# 会社 PC 動作確認手順

## かんたん実行（おすすめ）

1. 事前確認「0. 始める前に」の 3 点を確認する
2. GitHub の `awano27/pm-decision` → **Code → Download ZIP**。ZIP を右クリック →「プロパティ」→「許可する」にチェック → OK（ダウンロード由来の警告を防ぐ）→ 展開
3. 展開したフォルダの **`run-company-check.cmd` をダブルクリック**
4. 画面の指示に答える（聞かれるのは次だけ。すべて Enter で「いいえ／スキップ」になる）
   - Copilot のまとめ画面を開いたら Enter（無ければ `s`）
   - 自分とのチャットへのテスト送信 → 許可すると、iPhone から指定の `OK 番号` を返信するよう表示される（最大 3 分自動で待つ）
   - 確認待ち 2 件の送信 → iPhone から `OK 番号` と `NG 番号` を返信
   - az の組織名・プロジェクト名、アラート読み取り（任意）
   - Jev（T11）は環境変数 `TYPESAFE_API_KEY` があるときだけ確認する（キーを聞くことはない）
5. 終わると結果シートが**クリップボードにコピー**される（`kimeru-check-result.txt` にも保存）。そのまま貼り付けて返す

結果シートには本文・人名・キーは入らない（件数・OK/NG・エラー文のみ）。一時ファイルと画面構造の出力は自動で削除される。

特定の確認だけやり直す場合は、PowerShell で `.\run-company-check.cmd T9` のように番号を付けて実行する（依存する T3・T6・T8 は自動で実行される。複数指定可: `T5 T9`）。

**月曜の短時間チェック**: `.\run-company-check.cmd monday`（T3・T9・T12 チャット一覧の読み取り・T13 kev の事前チェック。10 分以内）

### ローカル判断モデル（kev）を使う場合

社外にデータを出さない判断モデル kev（Kev-4B、Apache-2.0。土台の Qwen3.5-4B-Base も Apache-2.0）を、開発 PC で作った持ち込み用フォルダ（約 11GB）で動かす。インストール・管理者権限・ネット接続は不要。

1. 開発 PC の `C:\develop\kev-bundle` を、リモートデスクトップ経由で会社 PC の `C:\kev` にコピーする
2. `C:\kev\start-kev.cmd` をダブルクリック。`Uvicorn running on http://127.0.0.1:8009` と出たら準備完了（ウィンドウは開いたまま）
3. kimeru フォルダで `.\run-company-check.cmd monday`（T14 で kev による判断を確認）
4. 常用するなら `setx KIMERU_BACKEND kev`（ユーザー環境変数。管理者権限不要）

目安（開発 PC: Ryzen 9 7940HS の CPU のみ、既定の bf16）: メモリ約 10GB、1 件の判断に中央値 2.5 秒・最大 6 秒。
bf16 命令のない CPU で遅い場合は、`set KEV_DTYPE=fp32` してから `start-kev.cmd` を実行する（メモリ約 14GB）。

### Azure DevOps のチケットを取り込む場合（az のインストール不要版）

1. 開発 PC の `C:\develop\az-bundle\az` を会社 PC の `C:\az` にコピーする（約 260MB、Microsoft 公式の ZIP 版を展開したもの）
2. `C:\az\bin\az.cmd login` でサインイン（Azure portal と同じ会社アカウント）
3. `.\run-company-check.cmd monday` の T10 で、組織名・プロジェクト名を入れるとチケットの取り込みと判断まで確認できる
4. 常用するなら `.\setup-company.cmd install -AdoOrg <組織> -AdoProject <プロジェクト>`

`C:\az` 以外に置いた場合は、環境変数 `KIMERU_AZ` に `az.cmd` のパスを入れる。

### 毎日動く状態にする（確認が OK だったら）

```powershell
.\setup-company.cmd install          # Kev の自動起動・KIMERU_BACKEND=kev・5 分ごとの自動運転（管理者権限不要）
.\setup-company.cmd status           # 状態の確認（Kev の応答・最後のサイクル）
.\setup-company.cmd remove           # 元に戻す（データは %LOCALAPPDATA%\kimeru に残る）
```

- 自動運転は画面を出さずに動き、記録は `%LOCALAPPDATA%\kimeru` に置く（ZIP を取り直しても消えない）
- Kev が起動する前に届いたイベントは受信箱に残り、Kev の起動後に判断される
- Kev の場所が `C:\kev` 以外なら `-KevDir <場所>`、間隔を変えるなら `-Minutes 10`

以下は同じ内容を手動で 1 つずつ行う場合の手順（かんたん実行が途中で止まったときの切り分け用）。

## 手動手順

kimeru が会社の PC（職場アカウントの Teams・会社の iPhone・社内の ADO / Azure）で動くかを段階的に確かめる手順。
上から順に進め、NG が出た段階で止めて「結果シート」を返す。

- 所要時間: レベル 0〜1 で 30〜40 分、レベル 2〜3 はそれぞれ 10〜20 分
- 外部への送信が起きるのは **T5（自分とのチャットへの送信）** と **T11（Jev への送信）** だけ。どちらも宛先は自分 / Jev のみ
- 本文・人名・トークンは結果シートに書かない（件数・OK/NG・エラー文だけ）

## 0. 始める前に（必ず確認）

| # | 確認すること | 理由 |
|---|---|---|
| 0-1 | 社内ルールで、GitHub からのスクリプト取得と PowerShell スクリプトの実行が許されているか | 許されていなければここで中止 |
| 0-2 | Teams の画面内容を自動で読み取ることが社内ルールに反しないか | T1〜T3 は画面の構造を読む |
| 0-3 | Jev（TypeSafe、社外クラウド）へ業務データを送ってよいか | 未確認なら T11 は**架空データのみ**で実施 |

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
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\teams-self.ps1 -Action post -Text '[kimeru #0] 判断が必要（会社PCテスト）\n返信: OK 0 / NG 0 / 保留 0'
```

- `{"ok":true,"typed":true,"sent":false}` で、自分とのチャットの入力欄にテスト文が入っていれば OK
- `Teams is not the foreground window` が出たら、Teams をクリックして前面に出してから再実行

### T5 送信と iPhone からの返信（外部送信あり: 宛先は自分のみ）

1. 送信:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\teams-self.ps1 -Action send
```

2. **会社の iPhone** の Teams に通知が来るか確認（来なくても自分とのチャットに表示されていれば記録して続行）
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
その場合は**かんたん実行**（`run-company-check.cmd`）を使うと、確認のうえ python.org のインストール不要版（zip 展開のみ・管理者権限不要）を
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

T8 のサンプルでは確認待ちが 2 件できるので、`[kimeru #1]` と `[kimeru #2]` の 2 通が投稿される。
iPhone から `OK 1` と `NG 2` を**別々のメッセージで**返信してから:

```powershell
python -m kimeru --out out-test approvals
```

`#1 -> approved` と `#2 -> rejected` が出れば OK（承認された処理は試し実行のみで、実際には何も書き込まれない）。
もう一度 `approvals` を実行して何も出なければ、二重処理しないことも確認できる。

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

- `az` が無い・`az login` が条件付きアクセスで拒否される → 記録して終了（社内ポリシー）
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
気づいたこと:
```
