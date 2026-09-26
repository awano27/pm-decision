# kimeru

PM の毎日の判断ポイントを、イベント駆動の**判断グラフ**で回す OSS。
Teams チャット・監視アラート・Azure DevOps のチケット作成・会議議事録が届くたびに、
グラフのノードで判断モデル（TypeSafe の Jev、または社外にデータを出さないローカルの Kev）に
型付きの質問を投げ、確信度で分岐し、すべての経路を **決定（decide）** か **アドバイス（advise）** で終わらせる。

```
event ─► normalize ─► match(規則) ─► judge(Jev/Kev) ─► plan(進め方) ─► decide  … 自動実行（現在は dry-run）
                          │ 重大                │ unsure
                          └─► 当番呼び出し/P1    └────────────────► advise  … PM へ（自分とのチャットで OK/NG）
```

## 設計原則

- **人が入るのは確信度が低い枝だけ**。judge ノードは `unsure` ルートが必須（検証で強制）。
- **重大事象はモデルに任せない**。「顧客が支払えない」「全ユーザーがログインできない」などは `match` ノード（規則）で先に判定し、モデルの評価が低くても当番呼び出し・P1 にする。
- **Jev は判断だけ、文章は書かない**。返信文・チケット本文はテンプレート（`{event.x}` `{answers.node.choice}`）。
- **グラフはデータ**。`graphs/*.json` を足すだけで判断ポイントを増やせる。閉路・到達不能ノード・ルート漏れは `validate` で弾く。
- **外部への書き込みは既定で dry-run**。`decisions.jsonl` に計画だけ記録する。

## 同梱グラフ

| イベント | グラフ | 判断ポイント（Jev） | 終端 |
|---|---|---|---|
| Teams チャット | `teams_chat.json` | 意図(choice) → 進め方(plan) / 判断期限(score) | 受領返信＋手順ごとの Task・Bug 化・人の確認 |
| 監視アラート | `monitor_alert.json` | 解消済み(noul) → **重大障害(match)** → 将来のリスクか(noul) → 時期(score) / 顧客影響(score) → ノイズ(noul) | 当番呼び出し＋障害対応の進め方・予防 Task（24h 以内 P1・7 日以内 P2・それ以降 P3）・P1 Bug・内部 Task・閾値見直し助言 |
| ADO チケット作成 | `ado_workitem.json` | **重大バグ(match)** → 着手可能か(noul) → 優先度(score) | P1 固定・情報不足コメント・優先度設定・人のトリアージ |
| 議事録 | `meeting_item.json`（箇条書き1行ごとに展開） | 行の種類(choice) → 担当(noul) → 期限(noul) / リスクの進め方(plan) | 決定ログ投稿・Task 作成・Risk 登録＋対策手順 |

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

## 進め方の型（playbooks）と plan ノード

返信だけでなく「次に何をどの順でやるか」も決める。Jev は計画を書かず、`playbooks/*.json` の型を選び、手順を絞り、並べるだけ。

```json
{"kind": "plan", "playbooks": ["schedule_change", "scope_change"], "routes": {"ok": "...", "none": "...", "unsure": "..."}}
```

1. 型を選ぶ（choice、型 + none）
2. 1 回のバッチで: 各手順が今回必要か（noul）、最初の一手（choice）、各手順の期限（score: 今日 / 今週 / 次スプリント以降）
3. 後続ノードで `{plan.title}` `{plan.summary}` `{plan.first}`、終端の `per_step` で手順ごとのアクション（例: ADO Task 作成）を使える

同梱の型: スケジュール変更 / 障害対応 / スコープ変更 / 人員調整 / リリース判定 / ステークホルダー対応 / リスク対応 / 要件明確化。
`none` や低確信度は従来の経路（例: 急ぎ度の判定）へ戻る。

## ローカル判断モデル（Kev）

社外にデータを出さない場合は、Jev と同じ API のローカルモデル [Kev](https://github.com/jaredpalmer/kev)（Apache-2.0）を使う。

```bash
# Kev 側（別フォルダ。CPU でも動く。初回に重みを取得）
uv sync --extra serve
uv run --extra serve python -m kev.serve --run jaredpalmer/kev-4b --port 8009
# kimeru 側
python -m kimeru --backend kev run examples/teams_chat.json    # 接続先: KIMERU_KEV_URL（既定 http://127.0.0.1:8009/v1）
```

`KIMERU_BACKEND=kev` を設定すると既定のバックエンドになる。Kev 用のしきい値は `kimeru/profiles.py`。

会社 PC 向けには、Python・torch(CPU)・Kev-4B・土台モデルを 1 フォルダ（約 11GB）にまとめ、`start-kev.cmd` だけでオフライン起動できる持ち込み用フォルダを使う（bf16 でメモリ約 10GB）。

**Kev-4B の評価**（架空の PM イベント 99 件、CPU、`eval/e2e.py` で最終的な行動を採点）:

| 正しい行動 | 人の確認へ | 安全側の代替行動 | 誤った行動 | 重大な取りこぼし |
|---|---|---|---|---|
| 52 | 41 | 6 | 0 | 0 |

誤った行動は出ないが、およそ 4 件に 1 件以上を人に回す（特に Teams）。1 件の判断は CPU で中央値 3.6 秒。実データで `eval/run_eval.py tune` による再調整を推奨。

## 1 日の自動運転（daily）

```bash
python -m kimeru --backend kev daily --send               # 5 分ごとに回し続ける
python -m kimeru --backend kev schedule install --minutes 5   # タスクスケジューラに登録（管理者権限不要）
```

1 サイクル: Teams のチャット一覧を取り込み（1 対 1 とメンション）→ 判断 → 人の確認が必要なものを自分とのチャットへ → iPhone などからの OK/NG を反映 → 朝（既定 8 時以降の最初のサイクル）にまとめを 1 回投稿。どれかの段が失敗しても残りは続き、`out/daily.log.jsonl` に残る。

## 取り込み（pull）

```bash
python -m kimeru pull teams --inbox inbox     # 画面のチャット一覧（API・同意なし）。初回は現状を記録するだけ
```

チャットの種類はチャット ID で判定する（`48:notes` = 自分とのチャット、`…@unq.gbl.spaces` = 1 対 1、`19:meeting_…` = 会議、その他の `…@thread.…` = グループ）。取り込むのは 1 対 1 とメンションされたチャットで、プレビューや時刻が前回から変わったものだけ。

アプリ登録も管理者同意も不要。本人の `az login` のトークンで定期取得し、inbox に置く（`watch` が処理）。

```bash
python -m kimeru pull ado --org <org> --project <project> --inbox inbox
python -m kimeru pull alerts --subscription <subscription-id> --inbox inbox
```

- ADO: WIQL で前回以降に作成された作業項目 → Service Hook と同じ形で保存
- アラート: Alerts Management API の Fired（Closed 以外）→ 共通アラートスキーマで保存
- 取得済み ID と前回時刻は `out/pull_state.json`。途中で失敗しても、配達済みの分は再配達しない
- 実テナントでは未検証（API 形状はフィクスチャで試験）

## 朝のまとめ（brief）

`python -m kimeru brief [--post [--send]]` は、未処理の確認待ちと直近の plan の手順（今日・今週）を集め、
「期限（plan で判定済み）→ 1 日遅れたときの損害（Jev の score、期限ラベルは見せない）」の順で並べ、
上位 3 件を自分とのチャット向けの 1 投稿にする。`--post` は貼り付けのみ、`--send` で送信。

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

## match ノード（規則の安全網）

```json
{"kind": "match", "fields": ["title", "description"], "patterns": ["..."], "exclude": ["テスト環境"], "routes": {"yes": "...", "no": "..."}}
```

モデルを呼ばずに正規表現で判定する（NFKC 正規化・大文字小文字無視）。`exclude` に当たれば一致しない。

## 評価

```bash
python eval/run_eval.py live --backend kev --out answers.jsonl   # 判断ポイントごとの正誤
python eval/e2e.py --backend kev                                  # 最終的な行動の正誤・重大な取りこぼし
python eval/compare.py jev=answers_jev.jsonl kev=answers_kev.jsonl
```

詳細は [eval/README.md](eval/README.md)。

## ロードマップ

- v0.2: ライブ実行器（Teams / ADO / on-call）を action 種別ごとに opt-in で解放
- v0.2: Service Hook / Graph change notification を受ける webhook 受信
- v0.3: 人の確認キューの回答を記録し、閾値（`yes_at` / `min_conf`）を自分のデータで調整

## 会社 PC での動作確認

`run-company-check.cmd` をダブルクリックするだけで全段階を自動確認し、結果シートをクリップボードに出す（`run-company-check.cmd monday` で短縮版）。
確認後は `setup-company.cmd install` で Kev の自動起動と 5 分ごとの自動運転を登録する（管理者権限不要、`remove` で元に戻す）。
詳細と手動手順: [docs/company-pc-test.md](docs/company-pc-test.md)

## 注意

Jev の性能数値は TypeSafe の利用規約上、公開しないこと（README・Issue・公開 CI ログを含む）。Kev の数値は公開してよい。

## ライセンス

MIT（[LICENSE](LICENSE)）。Kev と Qwen3.5 のモデルはそれぞれ Apache-2.0。
