# 貢献のしかた

## 開発の流れ

```bash
python -m unittest -q          # 全テスト（Windows では PowerShell を使うテストも走る）
python -m kimeru validate      # 判断グラフの検証
```

- 変更は小さく。判断グラフ（`graphs/`）を変えたら、`eval/` の評価も回してください（[eval/README.md](eval/README.md)）。
- 判断の契約（Kev が返す形）、承認番号（`#N`）、「外部への書き込みは記録のみ」は変えません。
- 文面の検査（`writer.unusable()` / `writer.unverified()`）を変えるときは、`tests/text_check_cases.py` に
  **ラベル付きの文例** を足してください（良い文例が拒否されない・悪い文例が通らないことを両方確かめます）。
- Teams の画面操作（`tools/*.ps1`）は、実際の Teams でしか確かめられません。識別ルール（どのチャットに書くか）を
  変える場合は、`tests/test_copilot_script.py` に「通ってはいけない名前」を足してください。

## 公開してはいけないもの

- 実際のメッセージ本文・社名・ユーザーのパス・Azure の ID・トークン
- Jev（TypeSafe）の性能数値（利用規約）。Kev の数値は公開して構いません
- 会社 PC の結果貼り付け（診断ファイル、`実行結果.txt`）をそのままコミットしない

## ライセンス

[LICENSE](LICENSE) に従います。
