# 体験の検証記録

README の「まず動かす」の手順を、新しい環境に見立てた状態で実行した記録です。誰でも同じコマンドで確かめられます。

## 環境

- 2026-10-18、作者の開発 PC（Windows 11、x86_64）
- Python 3.11.15 と 3.12.13（どちらも追加パッケージなし）。3.10 は CI（GitHub Actions の Windows）で確かめています
- 「新しい環境」の見立て: 作業ツリーのファイルを空のフォルダへ写し（ZIP の展開と同じ中身）、`KIMERU_*` の環境変数と `TYPESAFE_API_KEY` を外し、`LOCALAPPDATA` を空のフォルダに向けた

## 結果

| 確かめたこと | コマンド | 結果 |
|---|---|---|
| 取得の時間 | `git clone https://github.com/awano27/pm-decision.git` | 2.2 秒（6.5 MB。今回の画像を足す前の公開リポジトリで計測。足したのは約 1 MB） |
| 体験（3.11） | `python -m kimeru try` | 終了コード 0、1.74 秒（初回。Python のキャッシュ作成を含む） |
| 体験（3.12） | 同上 | 終了コード 0、1.02 秒 |
| 書き込み先 | `out` の中を見る | `out\try` だけ（`approvals.json`・`approvals.log.jsonl`・`decisions.jsonl`・`report.html` など）。状態フォルダ（`LOCALAPPDATA`）への書き込みは 0 件 |
| 9 件のデモ | `python -m kimeru --backend stub --out out/demo demo --pace 0` | 終了コード 0、0.87 秒。最後の 5 行は README と同じ |
| 外へ届く主な設定を入れた PC での体験 | 下の「設定がある PC」 | 終了コード 0。偽の writer CLI は呼ばれず、利用者の `config.json` は変わらず、状態フォルダに新しいファイルなし。冒頭に「この PC の kimeru の設定ファイルは読みません。環境変数の設定・鍵（9 件）も使いません」 |
| `try.cmd` の中身（Windows PowerShell 5.1） | `powershell -File tools\try.ps1 -NoOpen` | 終了コード 0、体験の出力のあとに終了（`-NoOpen` はブラウザを開かない確認用） |
| Python が無いとき | `PATH` を `C:\Windows\System32` などだけにして同上 | 終了コード 2。「Python 3.10 以上が見つかりませんでした」と、`winget install Python.Python.3.12` と python.org の入れ方を表示 |
| レポートの表示 | 画面の幅 1100px と 375px、キーボードの Tab と Enter | 375px で横にはみ出す要素なし。Tab で「計測の詳細」に移り、Enter で開く。空の記録フォルダ・確認待ち・配信結果不明・不明な状態・長文の表示はテスト（`tests/test_try.py` の `TestThePageWithoutTheHappyPath`）で確かめた |

### 設定がある PC

次の環境変数と、`config.json`（`writer=claude`・`backend=kev`・`execute=teams.reply`・`learn=1`）を置いた状態フォルダで `python -m kimeru --out out/try-poisoned try` を実行しました。

```text
KIMERU_WRITER=copilot  KIMERU_COPILOT_EXE=<呼ばれると印のファイルを作る偽の CLI>
KIMERU_BACKEND=jev  TYPESAFE_API_KEY=not-a-key  KIMERU_EXECUTE=teams.reply,ado.comment
KIMERU_PUSH=webhook  KIMERU_PUSH_WEBHOOK_URL=https://push.example.invalid/hook
KIMERU_KEV_URL=http://kev.example.invalid:8009/v1  KIMERU_KEV_URL_REMOTE_OK=1
```

同じことを `tests/test_try.py` が毎回の試験で確かめます（プロセスの起動とネットワーク接続を試験側でも見張り、0 件であること）。

## 確かめていないこと

- Python 3.10 での手元の実行（CI の Windows 3.10 で試験が通ることで代えています）
- macOS での体験（CI の Linux で試験が通ることで代えています）
- 実際の初めての利用者による手順の実行と所要時間（[初回の観察](first-run-observation.md) の手順で、これから測ります）
- `try.cmd` のダブルクリック（同じ `tools\try.ps1` を PowerShell から実行して確かめました）
