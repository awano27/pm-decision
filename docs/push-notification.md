# iPhone に「確認待ちがあります」を届ける

自分とのチャットへの投稿は、自分の iPhone には通知されません。席を離れていても確認待ちに気づけるように、**件数と番号だけ** を届ける経路を選べます。

- 送るのは、次のような 1 行だけです: `kimeru: 確認待ち 2 件（#41, #42） / 投稿できていない確認待ち 1 件。Teams の自分とのチャットを確認してください`
- 本文・送信者・件名は、含みません（`KIMERU_TOAST=detail` でも）。
- 何も設定しなければ、外へは何も送りません（PC の Windows の通知だけ）。

## 経路

| 経路 | 行き先 | 必要なもの |
|---|---|---|
| `teams_webhook` | あなたが Teams の Workflows で作った流れ（組織の Microsoft 365 の中） | Workflows の流れの URL |
| `webhook` | あなたが指定した URL（**https だけ**。http の URL は、送らずに理由を出します） | URL、認証の鍵（必要なら）、本文の形（必要なら） |
| `outlook` | デスクトップ版の Outlook から、**Outlook にサインインしている本人** へのメール（宛先は設定できません） | クラシック版の Outlook にサインイン済みであること |

## Teams の Workflows で流れを作る（`teams_webhook`）

1. Teams で「Workflows」（ワークフロー）を開く
2. 「Webhook 要求を受信したらチャットに投稿する」に当たるテンプレートを選ぶ（名前は Teams の版で変わります）
3. 投稿先に、**自分がメンバーのチャット**（自分とあなたのチャット、または自分だけのグループ）を選ぶ
4. 作成すると、この流れの **URL** が表示される。コピーする（この URL は、あなた宛ての通知を出せる鍵です。誰にも見せない）
5. PowerShell で、環境変数に入れる（今の画面だけなら `$env:`、これからの画面すべてなら `setx`）

```powershell
setx KIMERU_PUSH_TEAMS_URL "<コピーした URL>"
python -m kimeru config set push teams_webhook
```

6. **新しい** PowerShell を開いて（`setx` は、開いている画面には効きません）、試しに 1 回送る

```powershell
python -m kimeru push test
```

iPhone の Teams に、「kimeru: 試験の通知です（件数も本文もありません）」が届けば成功です。

## 汎用の webhook（`webhook`）

```powershell
setx KIMERU_PUSH_WEBHOOK_URL "<https の URL>"
setx KIMERU_PUSH_WEBHOOK_KEY "<認証の鍵>"          # 必要なときだけ。環境変数だけで受け取り、設定ファイルには書けません
python -m kimeru config set push webhook
```

- 鍵は、既定では `Authorization: Bearer <鍵>` のヘッダで送ります。鍵を本文に入れたいサービスでは、本文の形に `{key}` と書くと、そこに入ります（この場合はヘッダには付けません）
- 本文の形は、JSON になるものだけが使えます。`{text}`（1 行の通知）と `{key}`（鍵）が入り、引用符などは自動で置き換えます。省略すると `{"text": "{text}"}` です
- 本文の形は、`config show` に **中身を表示しません**（設定されているかだけ）。引用符が PowerShell 5.1 と 7 で違って渡されるので、コマンドの行には書かず、いったんファイルに書いて、`config set` に `@ファイル名` で渡します。設定ファイルを直接編集しないでください（形を崩すと、すべてのコマンドが止まります）。`config set` は、本文の形が JSON として正しいかを確かめてから書きます

```powershell
@'
{"channel": "me", "message": "{text}"}
'@ | Set-Content -Encoding utf8 webhook-body.json
python -m kimeru config set push_webhook_body "@webhook-body.json"
Remove-Item webhook-body.json
```

## Outlook（`outlook`）

```powershell
python -m kimeru config set push outlook
```

- デスクトップ版（クラシック）の Outlook が要ります。新しい Outlook では動きません
- 宛先は、Outlook にサインインしている本人のアドレスに固定です。設定で、他の宛先は指定できません（複数の宛先にも送りません）
- 宛先は、本人の**メールアドレス**で解決します（表示名では解決しません。同じ名前の連絡先があっても、そちらには行きません）。解決したアドレスが本人のものと一致することを確かめてから送り、確かめられなければ送りません（失敗として扱います）

## 複数の経路

`python -m kimeru config set push teams_webhook,outlook` のように、コンマで並べます。`push` に書けるのは、経路の名前（`teams_webhook`、`webhook`、`outlook`）だけです。URL を誤って書くと、拒まれます（URL は、設定ファイルにも、表示にも、記録にも出ません）。URL は環境変数で渡します。経路ごとに、通知した内容と成否を別々に持つので、1 つが失敗しても、他の経路と、5 分ごとの自動運転は、影響を受けません。

## 通知の決まり

- webhook の送り先が転送（30x）を返したときは、転送先へは進まず、失敗として扱います（鍵のヘッダを、転送先へ付けて送ることはありません）。転送が返る URL は、転送先の URL に直して設定してください。

- 同じ確認待ちと自動決定の通知を、同じ経路で 2 回は通知しません（件数が増えても同じです）。
- 経路を有効にした **最初のサイクルの始めに**、そのときの確認待ちと通知を「通知済み」として記録します。それより前の分は、送りません。そのサイクルで新しく投稿された確認待ちからは、送ります。外した経路の記録は消える（外している間に動いたサイクルで、外した経路の分だけ消えます。有効なままの経路の記録は残ります）ので、その経路をもう一度有効にしても、外していた間の分は送りません。
- 同じ経路への通知の間隔は、既定で 5 分以上です（`config set push_min_minutes 10` で変えられます）。間隔の間にたまった分は、まとめて 1 回で通知します。
- 失敗が 3 回続くと、その経路は 30 分休みます（続くと、倍々で最大 6 時間）。休んでいる理由と再開の時刻は、次のコマンドで見られます（管理された PC の自動運転は、`--out` を `%LOCALAPPDATA%\kimeru` にしています。`--out` を付けないと、別のフォルダを見ます）。

```powershell
python -m kimeru --out "$env:LOCALAPPDATA\kimeru" push status
python -m kimeru --out "$env:LOCALAPPDATA\kimeru" config show
python -m kimeru --out "$env:LOCALAPPDATA\kimeru" schedule status
```
- 投稿できなかった確認待ちがあるときは、その件数も通知に入ります（投稿できた分とは分けて数えます）。
- URL と鍵は、環境変数だけで受け取ります。設定ファイルには書けません。`push test` を含め、記録・表示・エラー文にも出ません（形が崩れた URL でも同じです）。
- `push test` は、1 つの経路で何が起きても、残りの経路も試します。

## 試験（管理された PC）

```powershell
.\run-check.cmd T21
```

設定ファイルで設定した経路も含めて、固定の試験の 1 行を送り、iPhone に届いたかどうかを聞かれます。

## スマホ実機で受信を確認して記録する

`push test` は「経路が受け付けたか」を見るだけで、スマホに届いたことは示しません。届いたことは、本人が実機で見て、経路ごとに記録します。記録は `<out>/onboarding.json` に入り、コードそのものは残らず（ハッシュだけ）、URL・鍵・本文も入りません。

### 手順（本人がスマホを手元に置いて行う）

1. 経路と URL を設定済みにする（上の各節）。管理された PC の自動運転と同じ記録先にするなら `--out "$env:LOCALAPPDATA\kimeru"` を付ける（以降の 4 つのコマンドで同じ値にする）。
2. 試験の通知を送る。**経路ごとに別のコード** が付きます。

```powershell
python -m kimeru --out "$env:LOCALAPPDATA\kimeru" onboarding notification-test --send
```

   - `webhook: accepted` は「経路が受け付けた」という意味で、スマホへの到達ではありません。`failed (...)` の経路は受理されていないので、確認の対象になりません。
3. **15 分以内に**、スマホの通知（Teams / メールなど、経路ごとの行き先）で `kimeru notification test challenge <コード>` を探す。
4. スマホで見えた経路ごとに、見えたコードを入力する。**見えなかった経路のコードは入力しない**（代わりに入れる人や自動処理は作らない。コードは通知にしか出ない）。

```powershell
python -m kimeru --out "$env:LOCALAPPDATA\kimeru" onboarding confirm <スマホで見えたコード> --device phone
```

   - `--device` は、見た場所です（`phone`、`pc`、`other`）。`phone` だけがスマホ実機の確認として数えられます。省略すると `unspecified` で、確認の記録は残りますがスマホ実機の確認には数えません。
   - 同じコードの 2 回目、期限（15 分）切れ、違うコード、受理されなかった経路のコードは、どれも失敗になり、記録は変わりません（2 回目の入力で `--device` を `phone` に書き換えることもできません）。
5. 記録を表示して、日時・経路・結果・未確認の範囲を写す。

```powershell
python -m kimeru --out "$env:LOCALAPPDATA\kimeru" onboarding device-check
```

   届かなかったときは、何も入力しません。受理されたのに確認がない経路として、記録にそのまま残ります（状態は `NOT_RUN` のままです）。

### 状態の読み方

| 実機確認の状態 | 意味 |
|---|---|
| `NOT_RUN` | 本人が `--device phone` で確認した経路が 1 つもない。送信が受理されただけ、旧形式の確認だけ、PC など phone 以外での確認だけ、のときもここ |
| `PARTIAL` | 設定中の経路のうち、一部だけ確認した |
| `CONFIRMED_ON_PHONE` | 設定中のすべての経路を、本人が `--device phone` で確認した |

- `onboarding status` と `trial report` も同じ状態を出します。コードが「成功」と言うことはなく、`NOT_RUN` から変わるのは本人が入力したときだけです。
- 旧形式の記録（1 つのコードを全経路へ送ったもの）の確認は「経路不明」として数え、どの経路の確認にもしません。
- `run-check` の T21 は固定の 1 行を送って「届いたか」を聞く試験で、この記録には入りません。実機確認の記録は、上の手順で残します。

### 記録に残らない範囲（`device-check` が毎回書く）

- 届くまでの遅れ、ロック画面・集中モード・通知オフのときの表示は測っていない
- 確認した端末以外（別のスマホ、タブレット）は未確認
- 確認するのは試験の文（challenge）だけで、実際の件数入りの文面での受信は別
- 確認した後で URL・鍵・端末側の通知設定を変えたら、再確認が要る
