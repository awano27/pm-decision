# 名前について（kimeru と pm-decision）

製品名は **kimeru**（「決める」）、リポジトリ名は **pm-decision** です。clone の URL は `https://github.com/awano27/pm-decision` のままで、コマンドは `python -m kimeru` です。

## 現状と決めたこと（2026-10）

- 「kimeru」は、Web 検索では同名の歌手と、意思決定支援の別サイトが上位を占めます。PyPI・npm には同名のパッケージはありません（2026-10-04 時点）。商標上の判断は、検索結果だけではしていません。
- GitHub の検索は、リポジトリ名・説明文・topics を見ます。そこで説明文と topics に「kimeru」を入れました（2026-10-06）。

| 案 | 発見しやすさ | 今あるものへの影響 | 手間 | 判断 |
|---|---|---|---|---|
| A 名前はそのまま、説明を足す（説明文・topics・README 冒頭） | GitHub 内の検索で「kimeru」「pm decision」の両方で見つかる見込み。Web 検索の結果は変わらない | 無し（clone の URL、release 名、Teams に貼られた URL がそのまま使える） | 小 | **実施した** |
| B リポジトリ名を `kimeru` にする | GitHub 内の名前の一致で上がる | GitHub は旧名から転送するが、文書の URL・貼られた URL は旧名のまま残る。後で同名のリポジトリを作ると転送が切れる | 小〜中 | **保留**。A の 2〜4 週間後に、GitHub 内の検索で見つからなければ検討する |
| C 製品名を変える | 衝突しない名前なら Web 検索で上がりうる | コマンド名、状態フォルダ `%LOCALAPPDATA%\kimeru`、環境変数 `KIMERU_*`、自動運転のタスク名、承認の読み取りが頼る投稿の印 `[kimeru #N]` に及び、使っている PC では設定と記録の移し替えが要る | 大 | **採らない**。名前が原因の誤解・問い合わせが記録で 2 件以上あったとき、または権利上の指摘を受けたときに考え直す |

## A の効果の確かめ方

説明文を設定した 2〜4 週間後に、次を実行して `awano27/pm-decision` が含まれるかを見ます。含まれなければ B を検討します。

```bash
gh search repos kimeru --limit 30 --json fullName --jq ".[].fullName"
```

## 共有カードの画像（Social preview）

`assets/readme/social-preview.png`（1280×640、先頭画像から作成）を、GitHub のリポジトリの Settings → General → Social preview → Edit → Upload an image で設定します。設定画面からしか登録できないため、作者が手で行います。
