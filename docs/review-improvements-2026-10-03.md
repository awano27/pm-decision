# レビュー改善の実装・検証レポート — 2026-10-03

このレポートは、Luna が担当したレビュー指摘 C01–C13 / 製品提案 P01–P07 の実装・文書更新と、root による最終レビュー・検証結果をまとめます。全テストと隔離 CLI 検証は合格しましたが、実環境や実業務の結果は別の証拠として扱います。

## C01–C13: コードと検証境界

| 項目 | 実装 | 現時点の確認 |
|---|---|---|
| C01 | ADO work item の Description と ReproSteps を別々の材料として保持し、判断状態にもそれぞれ渡す。 | 異なる値を持つ回帰 fixture、要求生成 source の CLI 確認、全体 suite を確認。 |
| C02 | 状況に関する記述から、今発生中の重大障害と範囲外・将来・正常の記述を分ける。 | 既存の規則・判断回帰あり。実 ADO 記述の網羅性は未確認。 |
| C03 | 投稿は「送った」表示だけで完了扱いせず、成功・入力・送信・対象を照合した読み戻し証拠を要求する。不確実な結果は保留し、自動再送を止める。 | delivery の読み取り・回復 CLI を隔離環境で確認。実 Teams 画面・実送信は未実施。 |
| C04 | Codex writer は no-tool 実行を保証できる起動方法がないため、現状は起動せず定型文に切り替える。 | 動作方針をコード・文書で確認。外部 writer / model の呼び出しはしていない。 |
| C05 | ADO 取り込みにサーバー時刻と重複取得範囲を使い、既知 ID を重複排除する。 | 回帰テストと全体 suite を確認。実 ADO tenant との境界確認は未実施。 |
| C06 | 型付き応答の種類・範囲・有限値を検証し、不正な応答で危険側へ進まない。 | 回帰テストあり。実モデルの応答試験は未実施。 |
| C07 | 続きの材料で以前の下書きや実行案を残さず、新しい改訂として承認を求める。古い改訂の不明な配信結果は、新しい改訂を投稿済みにしない。 | 改訂・配信回復のローカル確認と全体 suite を確認。実 UI は未確認。 |
| C08 | 新たに対象となるグループ言及を検出する。 | 回帰テストあり。実 Graph / Teams 環境は未確認。 |
| C09 | 安全性の採点は途中の不確実分岐だけでなく、最終的に選ばれた行動と安全 fallback を比較する。 | オフライン評価回帰あり。新方式の実 model 評価は **NOT_RUN**。 |
| C10 | 本文が同じ別メッセージを保持し、同一 UI メッセージの重複表現だけを集約する。 | 回帰テストあり。実 UI の全表記パターンは未確認。 |
| C11 | グラフ検証で許す最長実行経路の深さと、実行時の深さ上限を合わせる。 | `python -B -m kimeru --backend stub --out <temporary state> validate` で 4 graphs / 9 playbooks を確認。 |
| C12 | 直近7日の判断・承認・review 集計から未来の日時を除外し、不正または欠けた日時は期間内の証拠として扱わない。 | 日時境界の回帰テストと全体 suite を確認。 |
| C13 | `test_retry` のローカル HTTPServer は終了時に listening socket を閉じ、`test_writer` は共有 TemporaryDirectory を終了時に片付ける。 | 両テストの cleanup を含め全体 suite を確認。 |

## P01–P07: 利用手順と運用境界

| 項目 | 実装・利用例 | 現時点の確認 |
|---|---|---|
| P01 | 最初の試行は ADO プロジェクト1つの情報不足確認に絞る。同種の作業について前後の分数、下書き利用、手動で確認した結果を記録する。 | 手順を文書化。時間短縮や判断精度などの業務結果は **NOT_RUN**。 |
| P02 | 承認済み case の作業状態を手動管理する。例: `python -m kimeru --out out work set 12 in_progress --owner "担当者" --due 2026-10-10 --completion-condition "検収項目を確認"`。`done` はローカル台帳への手動入力で、case の承認、ADO 更新、実行権限を変更しない。 | 親による隔離 subprocess 操作で approved / in_progress / done と未承認 guard を確認。 |
| P03 | 投稿の先頭に判断、次の行動、不足情報、`OK` で起きることを示し、実行対象の本文全体を省略せず表示する。 | 実装に含む。個別の実画面・利用者理解度は未評価。 |
| P04 | 通知経路の送信受理と、端末で challenge を見た本人確認を別記録にする。`onboarding status` と `notification-test` の dry run は送信しない。実送信には明示的に `--send` が必要。 | 親の隔離 CLI 確認で status / dry run の無変更を確認。実通知・端末受信は **NOT_RUN**。 |
| P05 | 手動計測を `python -m kimeru --out out trial record --case 12 --before-minutes 18 --after-minutes 12 --draft edited --outcome correct` で記録し、`trial report --share` は個別計測値を出さず集計する。共有時は Jev と判断元不明の行を除外する。証拠がない時間は `Unknown` のまま。 | 親が共有レポートの privacy CLI 操作を確認。業務効果や因果効果は測定していない。 |
| P06 | `requirements build N --answers answers.json` が既存 case の材料と source references から Markdown / JSON を作る。case 自体の承認は前提にしない。`approve` は両ファイルの現在 hash を記録し、編集後の `status` は未承認を示す。 | 親が Description / ReproSteps を含む build、編集、approve、再編集による hash 無効化を確認。focused test 13件成功。 |
| P07 | 判断モデル、文面 writer、人による承認後の外部書き込みを分ける。Codex writer は現在 fail-closed で定型文へフォールバックし、ほかの writer は設定した実行先と条件に依存する。 | 文書を更新。外部 writer / model は実行していない。 |

### 要件 artifact の保存期間

`requirements build` の Markdown / JSON は `out/requirements/` に明示保存され、通常の確認待ち全文 `full_text.json` の TTL には連動しません。pending 中に取得した全文が artifact に含まれる場合も、その artifact は手動で削除するまで残ります。生成前に保存先を確認し、社内の保存規則に従ってください。hash 承認は成果物の現在内容を確認するローカル記録であり、元 case の承認や外部書き込み許可ではありません。

## 配信回復・引き渡しの具体例

```powershell
python -m kimeru --out out work list
python -m kimeru --out out delivery list
python -m kimeru --out out delivery show outbox outbox
python -m kimeru --out out delivery retry outbox outbox --confirm-not-sent
python -m kimeru --out out delivery confirm outbox outbox --confirm-delivered
```

`delivery list` は本文なしで対象を示し、`show` は該当する保留本文を表示します。`retry --confirm-not-sent` は本人が未送信と確認した場合に保留を解除するだけで、その場で投稿しません。`confirm --confirm-delivered` は期待された本文全体が自分とのチャットに表示されたという本人の申告を記録し、保留を解除します。これは bridge / UI の読み戻しとは別の証拠で、case 承認、作業完了、ADO 実行を意味しません。確認フラグを省いた操作は状態を変更しません。承認から引き渡しまでの所要時間は、両方の時刻が確かな形で記録されていないため **Unknown** です。

## 検証状況

- 親による隔離 subprocess の22操作: work の状態遷移・未承認 guard、requirements の build / edit / approve / 再編集後の hash 無効化、trial share privacy、onboarding status / dry-run の無変更、delivery の list / show / retry / confirm（case / notice / outbox / brief）を確認。これらは実送信を行わない CLI 操作の証拠です。
- 親による `python -B -m kimeru --backend stub --out <temporary state> validate`: 4 graphs / 9 playbooks が受理されました。
- F lane の `python -B -m unittest -q tests.test_requirements_artifact`: 13 tests passed。追加の `python -B -m unittest -q tests.test_pull tests.test_review_decisions tests.test_requirements_artifact`: 62 tests passed。
- root 最終回帰: `python -B -W error::ResourceWarning -m unittest -q` は exit 0、762 tests in 103.458s、`OK (skipped=1)`。ResourceWarning は出ていません。
- 対象差分の `git diff --check` は成功（Git の LF/CRLF 正規化警告を除く）。
- 初回統合で出た known-unsent / unknown の互換性期待、ReproSteps の state 期待値、新テストの isolate 初期化、HTTPServer / TemporaryDirectory cleanup を修正し、最終回帰で解消を確認しました。root は focused fixture / isolation 修正を受理しました。製品コードは、root による22 CLI 操作と validate の後に変更していません。

## 未確認とプロセス改善

実 Teams UI、実通知送信・端末受信、実 ADO tenant / 書き込み、外部 writer / model、現行採点方式での model 精度、pilot の時間短縮・業務効果は **NOT_RUN** です。CLI の隔離 subprocess 結果は、これらの実環境証拠の代わりにはなりません。

Jev は root の判断補助に shadow で使用し、判定結果は適用していません。モデル・effort・設定は変更していません。

次回、共有 Bridge の失敗契約を変えるときは、既存の `tests.test_defaults_and_settings` isolation meta-check を focused lane の早い段階で実行し、加えて次の互換性テスト群を回してください。

```powershell
python -B -m unittest -q tests.test_defaults_and_settings tests.test_fix12 tests.test_fix15 tests.test_execute tests.test_execute_safety tests.test_review_intake
```
