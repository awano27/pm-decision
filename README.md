# kimeru

PM の毎日の判断ポイントを、イベント駆動の**判断グラフ**で回す OSS。
Teams チャット・監視アラート・Azure DevOps のチケット作成・会議議事録が届くたびに、
グラフのノードで Jev（TypeSafe System One）に型付きの質問を投げ、確信度で分岐し、
すべての経路を **決定（decide）** か **アドバイス（advise）** で終わらせる。

```
event ─► normalize ─► judge(Jev) ─► judge(Jev) ─► decide  … 自動実行（v0.1 は dry-run）
                          │ unsure
                          └──────────────────────► advise  … PM へ。queue=true は人の確認待ち
```

## 設計原則

- **人が入るのは確信度が低い枝だけ**。judge ノードは `unsure` ルートが必須（検証で強制）。
- **Jev は判断だけ、文章は書かない**。返信文・チケット本文はテンプレート（`{event.x}` `{answers.node.choice}`）。
- **グラフはデータ**。`graphs/*.json` を足すだけで判断ポイントを増やせる。閉路・到達不能ノード・ルート漏れは `validate` で弾く。
- **外部への書き込みは既定で dry-run**。`decisions.jsonl` に計画だけ記録する。

## 同梱グラフ

| イベント | グラフ | 判断ポイント（Jev） | 終端 |
|---|---|---|---|
| Teams チャット | `teams_chat.json` | 意図(choice) → 判断期限(score) | 受領返信・判断タスク化・Bug 化・人の確認 |
| 監視アラート | `monitor_alert.json` | 解消済み(noul) → 顧客影響(score) → ノイズ(noul) | page+障害投稿・P1 Bug・内部 Task・閾値見直し助言 |
| ADO チケット作成 | `ado_workitem.json` | 着手可能か(noul) → 優先度(choice) | 情報不足コメント・優先度設定・人のトリアージ |
| 議事録 | `meeting_item.json`（箇条書き1行ごとに展開） | 行の種類(choice) → 担当と期限(noul) | 決定ログ投稿・Task 作成・Risk 登録 |

## 使い方

Python 3.10+、依存なし。

```bash
python -m kimeru validate
python -m kimeru run examples/*.json            # オフライン（キーワードスタブ）
python -m kimeru --backend jev run examples/*.json   # TYPESAFE_API_KEY を環境変数に設定
python -m kimeru watch inbox/                   # inbox/*.json を常駐処理（処理済みは inbox/done/）
python -m kimeru digest                         # 今日の判断件数と人の確認キュー
python -m unittest
```

出力: `out/decisions.jsonl`（全判断の経路と回答）、`out/queue.jsonl`（人の確認待ち）。

入力は各ソースの生ペイロードを自動判別する: Microsoft Graph `chatMessage`、Azure Monitor 共通アラートスキーマ、
Azure DevOps Service Hook `workitem.created`、議事録 `{title, date, text}`。

## judge ノード

```json
{"kind": "judge",
 "question": {"type": "noul|choice|score", "instructions": "...", "criteria": ..., "hints": ...},
 "routes": {...}}
```

- noul: `yes` / `no` / `unsure`。`yes_at`（既定 0.7）、`no_at`（既定 0.3）
- choice: 各選択肢 + `unsure`。`min_conf`（既定 0.6）未満は unsure
- score: `bands: [[上限(未満), node], ...]` + `unsure`。`min_conf`（既定 0.5）
- `hints` はオフライン用スタブ専用。Jev には送られない

## ロードマップ

- v0.2: ライブ実行器（Teams / ADO / on-call）を action 種別ごとに opt-in で解放
- v0.2: Service Hook / Graph change notification を受ける webhook 受信
- v0.3: 人の確認キューの回答を記録し、閾値（`yes_at` / `min_conf`）を自分のデータで調整

## 注意

Jev の性能数値は TypeSafe の利用規約上、公開しないこと（README・Issue・公開 CI ログを含む）。
