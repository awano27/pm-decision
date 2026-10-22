# 日常利用の不足修正・再検証（2026-10-21）

対象：`C:/develop/新ビジネス/kimeru` の現在の作業ツリー。README対応表は [PLAN.md](PLAN.md)。
開始時点で優先1〜4の先行実装、ado.updateが存在したため、再実装せず残る不具合を修正した。
`review-remediation-2026-10-17/RESULTS.md` のR1〜R8は現行コードに存在。過去のテスト成功は今回の検証に含めない。

## 要件ごとの状態

| 要件 | ローカル実装・統合・検証 | 実機・実データ | 残る境界 |
|---|---|---|---|
| 優先1：定期承認読み取り | DONE | NOT_RUN | 実Teamsの画面復帰、引用/入力欄のUIA構造、本人性は実機で確認が必要 |
| 優先2：匿名化候補〜評価導線 | DONE | NOT_RUN | 実データ・実モデルの評価成績はない。匿名化を自動保証しない |
| 優先3：経路別challengeと確認記録 | DONE（既存実装の回帰） | NOT_RUN | 本人が端末で受信した確認はない |
| 優先4：m365-auto | DONE（合成UI） | NOT_RUN | 全画面・右側パネル・会議チャット併存の実環境検証は未実施 |
| ロードマップの実機・実データ項目 | PARTIAL | NOT_RUN | ローカル導線だけで完了にしない |
| 新規実行アクション | DEFERRED | NOT_RUN | 今回は種類・設計の新規承認がない。既存ado.comment/ado.updateを保全し回帰 |

実機操作を必要とする段階は承認待ちでBLOCKED。ローカル作業を止める理由にはしていない。
READMEの注意書き・m365-autoの「実験的」・未完了チェックボックスは維持した。README自体は変更していない。

## 今回の変更ファイルと理由

| ファイル | 修正理由 |
|---|---|
| `tools/teams-self.ps1` | 読み取り途中の入力・復帰不能・前面復帰失敗で返信を返さず延期。元チャット不明なら操作前に延期。開始直後の例外も復帰処理へ。入力欄/引用識別子/集約Nameを返信から除外 |
| `kimeru/notify.py` | bridgeが成功を返しても復帰状態が不安全なら承認返信を適用しない。固定理由だけを伝える |
| `kimeru/daily.py` | 前面ウィンドウだけ戻せない場合も、チャットの復帰成功と混同せず記録・PC通知 |
| `tests/test_safe_approval_read.py` | 本番PowerShellの開始・終了処理を合成UIで実行し、途中入力、復帰失敗、前面復帰失敗、例外を検査 |
| `tests/test_approval_read_deferral.py` | OK/NG/済/再実行の不正進行防止、復帰後の1回だけの適用、ログと通知を検査 |
| `tests/test_approval_reader_isolation.py` | 入力欄/引用/親の集約本文を除外し、通常のDocument配下の正当な返信は保持 |
| `kimeru/dataset.py` | マニフェスト欠損・改変・件数・来歴・結果の分類不一致を検出。採点実装未照合・全件判定不能でロードマップDONEを禁止 |
| `eval/dataset_eval.py` | 元入力や既存結果を上書きしない。競合作成も排他的保存で防ぐ。NO_INPUTは終了コード1 |
| `tests/test_dataset_path.py` | 上書き・競合・欠損・現在グラフ不一致・採点未照合・判定不能の回帰 |
| `tools/teams-copilot.ps1` | ボタン探索後に入力先一意性・RuntimeId・本文一致を再確認し、変化時は送信も消去もしない |
| `tests/test_copilot_script.py` / `tests/synthetic_copilot_ui.ps1` | 送信直前の2個目の入力欄出現・本文編集を再現 |
| `docs/reference.md` | 新しい延期理由と返信を適用しない条件 |
| `docs/managed-pc-check.md` | m365-autoの送信直前検査と実機の未確認範囲 |
| `docs/evaluation.md` / `eval/README.md` | 新規結果ファイル、現在採点実装の照合手順、NO_INPUTと不整合の扱い |

既存ファイル16件の差分とタスク専用PLAN/RESULTSのみ。既存のルートPLANと以前のタスク記録は変更していない。

## 検証方法と証拠

開始時の追跡済み・未追跡190ファイルを `C:/tmp/kr-jh6h3sl7/baseline` へ保全。
SHA256は同フォルダの `manifest.json`、今回だけの差分は `task.diff`。
最終検証コピーは `C:/tmp/kr-jh6h3sl7/final`。資格情報を渡さない環境変数allowlist、一時HOME/USERPROFILE/LOCALAPPDATA/APPDATA/TEMP、`tests/isolate.py`を使用。

- 優先1の不具合は追加回帰で修正前18失敗（subcaseを含む）を再現。その後、関連テスト成功。
- 優先2は追加回帰で2失敗・2例外、および全件判定不能でもDONEになる失敗を再現して修正。
- 優先4は追加した2条件で修正前に送信されることを再現して修正。
- 優先3/4の関連5モジュールは今回176件成功。優先2の最終対象は28件成功。個別件数は重複するため合算しない。
- PowerShell 2本の構文解析：エラー0。
- `python -B -m kimeru validate`：4グラフ・9プレイブック成功。
- `git diff --check`：作業ツリーで成功。Git管理外の検証コピーでの初回実行は対象誤りで失敗し、正しい作業ツリーで実行し直した。

### 全体検証（最終コード）

`python -B -W error::ResourceWarning -m unittest -q`：**1,014件、1,012成功、1失敗、1 skip（218.090秒）**。
全体検証の状態は **PARTIAL**。今回の変更による失敗は検出なし。

残る失敗は `tests.test_fix12.TestEveryCommandLineInTheDocsIsIntact.test_run_check_command_lines_are_not_broken`。
`docs/reputation-audit.md` のE266行のインラインコードを既存検査が誤ったコマンド接頭辞として扱う箇所で、開始時コピーでも同じ失敗を今回再現した。文書・検査は変更していない。

ログ：`C:/tmp/kr-jh6h3sl7/full-suite-utf8.log`。
実行したコード・テスト・関連文書は検証コピーとSHA256一致（不一致0）。タスク専用PLAN/RESULTSだけは検証後の結果追記を含む。
開始時190ファイルのうち、今回変更した既存ファイルは16、174ファイルは内容不変。欠落0。HEADは開始時と同じ `abf0d70d483a510973549de17756cdeb6de4d679`。

関連対象には `test_notify`、`test_daily`、`test_safe_approval_read`、`test_approval_read_deferral`、`test_approval_reader_isolation`、`test_dataset_path`、評価・校正・privacy、`test_push`、`test_onboarding`、`test_trial`、`test_writer`、`test_copilot_script` と既存 `ado.comment` / `ado.update` の回帰を含む。これらは最終全suiteでも再実行した。


初回の全体テストは検証環境の文字コード不一致（UTF-8出力をcp932で読む）でreader threadに例外が出たため、当タスクのテストプロセスだけ停止した。検証ハーネスにPYTHONUTF8=1を設定し、最終コードをコピーし直して全体を再実行。製品やテストの検査を弱めていない。

## 実機確認と別フェーズ

- 実Teams T3b：本人が作業中の延期、無操作時の元チャット/前面復帰、途中操作優先、例外/競合を確認。本文を含まない日時・Teams版・結果だけ記録する。実機の引用識別情報がないため、識別子のない引用・Document型の入力欄はUnknown。自動運転の注意は撤去しない。
- 実端末：既存 `onboarding challenge` の送信先と経路を具体化して別途承認後、端末で実際に見えたコードを経路ごとに本人が確認。日時・経路・結果・未確認範囲は `onboarding device-check` に記録する。
- m365-auto：既存手順で3レイアウトを環境ごとに確認。合成UI結果を実環境の証拠にしない。
- 実データ評価：元データからローカル候補を作り、人が全内容を確認した後に分割する。実モデル呼び出しと外部送信は別途承認が必要。Jevの結果は非公開・共有不可を維持。
- 実行拡張：新規候補として `ado.create`（1件のタスク起票）や `teams.reply`（1件の返信）があるがPROPOSED。宛先・本文・対象・改訂に紐づく再承認、冪等性、結果不明時の自動再送禁止、手動再実行、監査を設計してから承認を受ける。今回はDEFERRED。既存 `ado.update` を追加実装扱いにはしない。
- writer未設定時の新しい聞き返し動作、投稿時の画面復帰拡張もDEFERRED。今回の通常承認読み取りとは区別する。

実Teams投稿・実ADO更新・業務データ送信・実モデル/有料Jev・スケジュール登録・commit・push・公開は実行していない。
Jev: NOT_RUN（ユーザーの有料モデル禁止に従う）。shadow・モデル/effort設定は変更なし。
