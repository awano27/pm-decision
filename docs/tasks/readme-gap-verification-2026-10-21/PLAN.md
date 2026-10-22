# README 日常利用の再検証・残存不足の修正（2026-10-21）

対象は現在の作業ツリー。開始時190ファイルを C:/tmp/kr-jh6h3sl7/baseline にコピーし、manifest.json に SHA256 を保存。HEAD のみのコピーではない。既存 PLAN / RESULTS は上書きしない。

## 制約と受入方針
実 Teams / ADO / 業務データ / 有料モデル / スケジュール / commit / push / 公開は禁止。Jev は有料モデル禁止のため NOT_RUN（shadow・設定不変）。tests/isolate.py と資格情報なしの一時 HOME を用いる。過去の成功は今回の証拠に使わない。

## README 対応表（開始時の静的確認、旧計画から現状に更新）

| # | README の記述 | 実装箇所 | テスト | 不足 | 分類 | 受入条件 |
|---|---|---|---|---|---|---|
| 1 | 依頼から手順・返信案・確認待ちを作る | `cli.process`、`graph.run`、`plan.py`、`notify.format_post` | test_e2e, test_kimeru, test_plan, test_demo | 実依頼での精度は未測定 | 実装済み | stub で examples の 4 種が記録・確認待ちに出る |
| 2 | 4 つの利用例（延期・障害・情報不足・議事録） | `graphs/*`、`playbooks/*`、`events.meeting_minutes` | test_plan, test_demo, test_requirements_flow, test_daily | 実 ADO・実議事録の形式ゆらぎ | 実機未検証 | 実入力で各手順が出る |
| 3 | 朝のまとめ（8 時、上位 3 件、確認待ちと未完了を分ける） | `brief.build`、`daily.cycle` | test_brief, test_work, test_daily_source_coverage | 定刻ではなく 8 時以降の最初の成功サイクル。自動運転が使えない | 部分実装 | 自動運転で 1 日 1 回、2 区分で投稿 |
| 4 | 要件案のファイル保存・承認後の手動進捗 | `requirements.dispatch`、`work.set_state` | test_requirements_*, test_work | 仕様どおり手動 | 実装済み | 未回答は不明、承認だけで done にならない |
| 5 | HTML レポート・週次集計・試行の記録 | `report.build`、`stats.py`、`trial.py` | test_state_views, test_trial | 時間短縮は未測定 | 部分実装 | 5 件以上のペアで集計、不足なら結論不能 |
| 6 | 既定では返信・起票・当番を実行せず記録 | `actions.execute`（dry-run）、`execute.SUPPORTED`（ado.comment / ado.update） | test_execute, test_execute_safety | なし（仕様） | 実装済み | OK 後も外部呼び出し 0 件 |
| 7 | `--send` で自分とのチャットへ投稿 | `notify.notify`、`teams-self.ps1 post/send` | test_notify, test_bridge_e2e, test_delivery_send_once | 投稿は自分とのチャットへ切り替えたまま。実 UI 再検証なし | 実機未検証 | 実 Teams で 1 件投稿、二重投稿なし |
| 8 | **毎日の使い方（定期実行）** | `cli.schedule`、`daily.cycle`、`notify._collect`→`bridge.read()`→`Start-SafeRead` | test_daily, test_notify, test_safe_approval_read, test_approval_read_deferral | 安全な読み取り・復帰・延期処理は現行コードに存在。今回の合成検証と安全性再確認が必要 | 部分実装 | 作業中は延期、開いたら元へ戻す、選び直しを上書きしない（優先 1） |
| 9 | Teams の取り込み（一覧から UIA）・全文読み取り | `pull.pull_teams`、`fulltext.Reader`、`readchat` | test_pull, test_fulltext | 実 Teams の版差・既読化 | 実機未検証 | 実機で一覧から取り込み、全文読み取り後に復帰 |
| 10 | ADO・アラートの取り込み | `pull.pull_ado/pull_alerts`、`http_json` | test_pull, test_pull_redirect_safety | 実テナントの記録は 1 回のみ | 実機未検証 | 実 ADO から重複なく取り込む |
| 11 | 判断モデル停止中は受信箱に残し二重判断しない | `daily.process_inbox`、`processed.txt` | test_retry, test_daily | なし | 実装済み | 復帰後に 1 回だけ判断 |
| 12 | 承認返信 `OK/NG/保留/修正/聞き返し` | `notify._collect`、`teams-self.ps1 read`（Walk） | test_notify, test_approval_reader_isolation, test_fix10〜16 | 実 UI 構造・本人性は未確認 | 実機未検証 | 実スマホの返信が 1 回だけ効き、投稿本文のコマンドは拾わない |
| 13 | `修正 N` / `聞き返し N` | `notify._redraft`、ask_back 分岐 | test_writer, test_fix14 | writer 未設定だと修正不可・聞き返し文が作られない。README は条件を書いていない | 仕様未決定 | writer 未設定時の挙動を決める |
| 14 | ADO コメントの実行・`再実行 N-k`・`済 N`・配信不明の保留 | `execute.run_approved`、`notify.delivery_*` | test_execute*, test_delivery_*, test_fix13〜16 | 実 ADO 書き込みは未確認 | 実機未検証 | テスト ADO に 1 件だけ書かれる |
| 15 | writer（copilot/claude/grok/cmd/codex/m365） | `writer.WRITERS`、`get_writer` | test_writer | 実 CLI・アカウントは未確認 | 実機未検証 | 各 CLI で下書き、失敗時は定型文 |
| 16 | `m365-auto`（実験的） | `writer`（COOLDOWN 1800）、`tools/teams-copilot.ps1` | test_copilot_script, test_writer | 合成 UI 検査が存在。今回再検証。実際の複数環境は未確認 | 部分実装 | 合成 UI で全レイアウト・途中変化を検証し、実機は別記録（優先 4） |
| 17 | PC 通知・スマホ通知（件数だけ） | `notify.show_toast`、`push.py`、`onboarding.py` | test_push, test_onboarding | 経路別 challenge / device-check が存在。今回の回帰と実端末記録が必要 | 実機未検証 | 経路別に記録、実機未確認は NOT_RUN（優先 3） |
| 18 | データの行き先（既定ローカル、URL を記録しない） | `backends._local_url`、`push.check_url` | test_evaluation_privacy, test_push | なし | 実装済み | 既定で外部送信 0 件 |
| 19 | ソース ZIP でデモが動く・持ち込みパッケージ | `demo.py`、`resources.py`、`tools/make-bundle.ps1` | test_demo*, test_distribution_assets | make-bundle のテストなし。公開 ZIP は未コミット変更を含まない | 部分実装 | 公開物と作業ツリーの一致は公開時に確認 |
| 20 | ロードマップ：実データ（匿名化）での評価 | `review.py`、`calibrate.py`、`eval/*` | test_review_*, test_evaluation_* | anonymize / dataset / dataset_eval が存在。今回の回帰が必要。実データ評価は NOT_RUN | 部分実装 | ローカル導線を検証。実データ評価がない限り未完了（優先 2） |
| 21 | ロードマップ：スマホ通知の実機確認 | 同 17 | 同 17 | 本人の実機確認がない | 実機未検証 | 本人の記録があるまで NOT_RUN |
| 22 | ロードマップ：`m365-auto` を複数環境で確認 | 同 16 | 同 16 | 実環境の証拠がない | 実機未検証 | 証拠がそろうまで「実験的」 |
| 23 | ロードマップ：実行できる行動を増やす | `execute.SUPPORTED` | test_execute | ado.update の優先度更新も既存作業として存在。追加の種類・設計は今回承認されていない | 仕様未決定 | 既存実装を保全して回帰。新規拡張は PROPOSED / DEFERRED |


## 実行計画
1. 優先1は親が安全な読み取り、復帰失敗・途中入力・例外・ロック・二重処理を確認し必要最小限の修正。
2. 優先2は評価導線担当、優先3〜4は通知・Copilot担当に独立委譲。既存変更を維持し、不足をコードと合成fixtureで修正する。担当外変更は禁止。
3. 親が全差分・契約・境界をレビューして統合。関連回帰後に全 unittest と validate / 差分検査を実行。
4. RESULTS に実装 / ローカル / 実機を分け、今回の証拠と残る判断を記録。実機の証拠なしに注意書き・実験的・ロードマップを完了へ変更しない。

## 依存・リスク
優先2と3〜4は独立。共有CLI/READMEを変更する必要は親へ連絡。多数の既存変更は開始時コピーと比較し保持する。テストは実サービスを模擬し、資格情報を渡さない。既存文書検査の失敗は今回再現して区別する。

## 別フェーズ
新規実行アクションは PROPOSED / DEFERRED。ado.update は既存実装として保全するが、この依頼を追加設計・外部実行の承認にしない。writer未設定時の新しい製品動作と投稿時の画面復帰拡張は DEFERRED。確定済み4優先事項を先に完了する。

## 今回のレビューで確認・修正した不足
- 読み取り中の本人操作・復帰失敗でも返信を適用していた。返信を破棄して延期する契約へ整合。
- 既に自分とのチャットが開いている場合の入力時刻記録、元チャット不明時の事前延期、開始直後の例外の復帰処理を補強。
- 入力欄・識別情報のある引用・親要素の集約本文を承認返信として拾う経路を除外。実Teamsの識別情報は実機未検証。
- 評価の入力上書き・欠損マニフェスト・現在の採点方式未照合・全件判定不能の完了表示を防ぐ。
- m365-autoの送信ボタン探索後にも対象一意性・同一性・本文一致を確認。

新しいアクション種類は追加しない。既存ado.updateは保全・回帰対象のみ。結果と今回の証拠はRESULTS.mdに記録する。
