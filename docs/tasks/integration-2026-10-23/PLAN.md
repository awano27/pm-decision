# 公開mainへの統合（2026-10-23）

対象: origin/main 7c4cdebf2f7a0ab6b978a5f0d08abe5b51c8aca3 を基にした隔離worktree。
元の作業ツリーと既存PLANは維持。別履歴を強制mergeせず、承認済み修正と依存実装を移植する。

## 計画・担当
1. 現行mainとローカルHEAD/作業ツリーをファイル単位3-way比較し競合を明示。
2. 親: notify/daily/cli/execute/events/config/pull/Teams安全契約の統合・最終レビュー。
3. 評価担当: graph/calibrate/anonymize/dataset/evalと評価テスト・docs。通知担当: push/onboarding/trial/writer/Copilotと対応テスト。一般担当: brief/report/stats/work/demo/requirements/配布と対応テスト・README等。
4. 各担当が公開mainの新機能を保持し、重複する旧実装は再導入せず必要な修正を統合。新仕様は親へエスカレーション。
5. 関連隔離回帰→全suite→validate/差分検査。元の失敗と統合失敗を区別。テストを弱めない。
6. ユーザーの既存push依頼に基づき、検証済み統合をcommit/push。mainを更新する場合はremoteを再確認し、非fast-forwardを強制しない。

## 制約・受入条件
実Teams/ADO/端末/業務データ/モデル/有料Jev/スケジュールは未実施を維持。tests/isolate.pyと資格情報なし一時HOMEで実施。
承認読み取りの安全性、評価のデータ保全/来歴、経路別通知、m365-auto送信直前確認と公開mainの回帰を両立する。
実機はNOT_RUN、自動運転注意/実験的を維持。統合の結果と残る条件をRESULTS.mdへ記録。
