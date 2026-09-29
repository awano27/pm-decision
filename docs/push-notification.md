# iPhone に「確認待ちがあります」を届ける

自分とのチャットへの投稿は、自分の iPhone には通知されません。席を離れていても確認待ちに気づけるように、**件数と番号だけ** を届ける経路を選べます。

- 送るのは、次のような 1 行だけです: `kimeru: 確認待ち 2 件（#41, #42） / 投稿できていない確認待ち 1 件。Teams の自分とのチャットを確認してください`
- 本文・送信者・件名は、含みません（`KIMERU_TOAST=detail` でも）。
- 何も設定しなければ、外へは何も送りません（PC の Windows の通知だけ）。

## 経路

| 経路 | 行き先 | 必要なもの |
|---|---|---|
| `teams_webhook` | あなたが Teams の Workflows で作った流れ（組織の Microsoft 365 の中） | Workflows の流れの URL |
| `webhook` | あなたが指定した URL | URL と、本文の形（必要なら） |
| `outlook` | デスクトップ版の Outlook から、あなた自身へのメール | クラシック版の Outlook にサインイン済みであること |

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

6. 新しい PowerShell で、試しに 1 回送る

```powershell
python -m kimeru push test
```

iPhone の Teams に、「kimeru: 試験の通知です（件数も本文もありません）」が届けば成功です。

## 汎用の webhook（`webhook`）

```powershell
setx KIMERU_PUSH_WEBHOOK_URL "<URL>"
python -m kimeru config set push webhook
python -m kimeru config set push_webhook_body "{\"text\": \"{text}\"}"   # 省略できる。{text} に 1 行が入る
```

本文の形は、JSON になるものだけが使えます（`{text}` は、引用符などを自動で置き換えて入ります）。

## Outlook（`outlook`）

```powershell
python -m kimeru config set push outlook
python -m kimeru config set push_mail_to "<自分のメールアドレス>"
```

デスクトップ版（クラシック）の Outlook が要ります。新しい Outlook では動きません。

## 複数の経路

`python -m kimeru config set push teams_webhook,outlook` のように、コンマで並べます。経路ごとに、通知した内容と成否を別々に持つので、1 つが失敗しても、他の経路と、5 分ごとの自動運転は、影響を受けません。

## 通知の決まり

- 同じ確認待ちを、同じ経路で 2 回は通知しません。
- 同じ経路への通知の間隔は、既定で 5 分以上です（`config set push_min_minutes 10` で変えられます）。間隔の間にたまった分は、まとめて 1 回で通知します。
- 失敗が 3 回続くと、その経路は 30 分休みます（続くと、倍々で最大 6 時間）。休んでいる理由と再開の時刻は、`python -m kimeru push status`、`python -m kimeru config show`、`python -m kimeru schedule status` で見られます。
- 投稿できなかった確認待ちがあるときは、その件数も通知に入ります（投稿できた分とは分けて数えます）。
- URL は、環境変数だけで受け取ります。設定ファイルには書けません。記録・表示・エラー文にも出ません。

## 試験（管理された PC）

```powershell
.\run-check.cmd T21
```

固定の試験の 1 行を送り、iPhone に届いたかどうかを聞かれます。
