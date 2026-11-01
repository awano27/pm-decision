# Kev の持ち込み用フォルダの作り方

ネットにつながる開発 PC で一度だけ作り、管理された PC へコピーして使うフォルダです。できあがりは、インストール・管理者権限・ネット接続なしで Kev（この PC の中で動く判断モデル）を起動できます。`tools\make-bundle.ps1 -KevSrc <このフォルダ>` が前提にしている形です。

> この手順は、作者の開発 PC にある持ち込み用フォルダ（2026-09 作成）の中身から書き起こしたものです。うまくいかなかった点は Issue で知らせてください。上流の手順は [jaredpalmer/kev](https://github.com/jaredpalmer/kev) の README（2026-10 時点で Python 3.12 か 3.13 と uv が前提）に従います。

## できあがりの形

```text
kev-bundle\
  python\                 Python 3.13 の単体版（移動できるもの）＋ torch（CPU 版）＋ kev ＋ fastapi など
  models\kev-4b\          Kev-4B（判断用アダプター。約 0.2GB）。配布元のまま（head.pt を書き換えない）
  hf\hub\models--Qwen--Qwen3.5-4B-Base\snapshots\<版>\  土台のモデル（約 9.3GB）。head.pt が指す版を、Hugging Face の置き場の形で
  start-kev.cmd           起動（オフライン。head.pt の照合・鍵の用意・精度の自動選択のあと）
  kev-models.py           モデルの取得（fetch）と照合（verify。--quick は pickle の head.pt だけ）
  pins.json               固定した版: 上流の Kev のコミット、torch の版、モデル 2 つの版と全ファイルの SHA-256
  pick-dtype.py           この CPU で bf16 が速いかを判定する
  dtype-test.cmd          どちらの精度で起動するかを表示するだけ
```

| | 作者環境での値 |
|---|---|
| ディスク | 約 11.4GB（python 約 1.9GB、土台のモデル約 9.3GB、Kev-4B 約 0.2GB） |
| メモリ（起動後） | bf16 で約 10GB、fp32 で約 14GB |
| 取得にかかる時間 | 未測定（回線による。土台のモデルが約 9.3GB） |
| 起動にかかる時間 | 未測定（初回の読み込みは数分かかる、という観察だけ） |

`start-kev.cmd`・`kev-models.py`・`pins.json`・`pick-dtype.py`・`dtype-test.cmd` は、このリポジトリの `tools\kev-bundle\` にあります。

## 安全のための固定と照合

| 何を | どうしているか | なぜ |
|---|---|---|
| 上流の Kev | `pins.json` の `kev_commit` に固定して入れる | 上流の新しいコミットが、確かめないまま入らないように |
| モデル 2 つ | `pins.json` の版（コミット）で取得し、全ファイルの SHA-256 を照合する。値は Hugging Face の記録（大きいファイルは SHA-256、ほかは git の blob ID）と一致を確かめてから書いた | 配布元のリポジトリが差し替えられても、違うファイルでは作れない |
| `head.pt` | 起動のたびに SHA-256 を照合し、一覧に無い pickle やコードのファイル（`.bin`・`.py` など）がモデルのフォルダにあれば起動しない | torch は `head.pt` を unpickler で読む。torch 2.10 より前の `weights_only=True` には、細工したファイルで任意のコードを実行できる脆弱性があった（CVE-2026-24747） |
| torch | `pins.json` の `torch`（2.13.0）を入れる。上流の Kev は `torch<2.9` を指定しているので、pip が依存の食い違いを表示するが、想定どおり | CVE-2026-24747 などを直した版。kimeru の評価用の 20 件（38 問）で、torch 2.8 と同じ答え（数値の差は最大 0.0066）を確かめた |
| 依存パッケージ | `pins.json` の `security_floors`（aiohttp・filelock・certifi・setuptools）の版以上にそろえる | 2026-10 時点の既知の脆弱性を直した版 |
| Kev の呼び出し | `start-kev.cmd` が Windows のユーザーごとに鍵（`%LOCALAPPDATA%\kev\api-key`）を 1 回作り、Kev は鍵の無い要求を 401 で断る。kimeru と確認用のスクリプトは同じファイルを読む | Kev は CORS ですべてのサイトを許すので、鍵が無いと、この PC のほかのプロセスや Web ページから呼べた |

鍵が要るのは `start-kev.cmd` で起動した Kev だけです。`KEV_API_KEY` を自分で設定していれば、その値を使います（kimeru も同じ）。鍵のファイルの場所は、kimeru の設定 `kev_key_file`（環境変数 `KIMERU_KEV_KEY_FILE`）で変えられます。kimeru は、この PC 以外の Kev（`kev_url_remote_ok=1`）には、このファイルの鍵を送りません。

以前の形のフォルダ（`models\Qwen3.5-4B-Base\` と `localize.py`）は、`build-kev-bundle.cmd` をもう一度実行すると、新しい形に移します。土台のモデルは、ハッシュが一致すれば取得し直さずに移します（`head.pt` は書き換えられているので取得し直す。約 5MB）。

## 作り方（開発 PC、ネットあり）

前提は git と uv です。uv が無ければ、PowerShell で次の 1 行を実行し（[uv の公式の案内](https://docs.astral.sh/uv/getting-started/installation/)）、PowerShell を開き直します。

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

kimeru のフォルダで `build-kev-bundle.cmd` をダブルクリックします（PowerShell から行うなら `powershell -ExecutionPolicy Bypass -File tools\build-kev-bundle.ps1`）。次の順に進みます。

1. 取得先（github.com・pypi.org・download.pytorch.org・huggingface.co）に届くかを確かめる。HEAD の要求だけを送り、本文は送らない。送る前に `[y/N]` で尋ねる。
2. 「約 10GB を取得して `C:\develop\kev-bundle` に作ります。続けますか」と `[y/N]` で尋ねる。
3. 上流の取得、Python 3.13 の用意、torch（CPU 版）と kev の導入、モデル 2 つの取得、起動用のファイルの配置を、順に行う。取得済みのものは飛ばす。止まったときは、足りないものを 1 行で示す。作りかけのまま同じコマンドを再実行できる。
4. 完成の検査をし、この CPU の精度を示し、参照先を設定する。最後に、任意で起動の確認をする（`[y/N]`）。
5. 最後に、`tools\make-bundle.ps1` に渡す次のコマンドを表示する。

オプション: `-Out <場所>`（既定 `C:\develop\kev-bundle`）、`-KevRepo <場所>`（既定 `C:\develop\kev-local`）、`-Yes`（すべての問いに「はい」で答える）、`-DryRun`（何も取得せず、何も作らず、手順と取得先・容量の表だけを出す）、`-NoStartCheck`（起動の確認を省く）。

> `build-kev-bundle.cmd` の通し実行（約 10GB の取得）は、まだ新しい PC で確かめていません。確かめたのは `-DryRun` の出力と、前提が無いときに 1 行で止まることだけです。

<details>
<summary>手作業の手順（build-kev-bundle.cmd が使えないときだけ）</summary>

1. uv を入れる（前の節の 1 行）。上流のリポジトリを取得し、Python 3.13 を用意してから、上流の手順で一度動かす。

   ```powershell
   uv python install 3.13
   git clone https://github.com/jaredpalmer/kev C:\develop\kev-local
   cd C:\develop\kev-local
   uv sync --extra serve
   ```

2. 単体版の Python を持ち込み用フォルダへ写す。`uv python find --managed-python 3.13` が示す（uv が管理する）Python 3.13 のフォルダを、まるごと `kev-bundle\python\` にコピーする。Microsoft Store の Python など uv の管理外のものは選ばない。

3. コピーした Python の pip の印を外す。uv の Python には `Lib\EXTERNALLY-MANAGED` が付いていて、このままでは pip が externally-managed-environment と言って拒否する。消すのは持ち込み用フォルダの中のコピーだけで、uv の元のフォルダには触れない。

   ```powershell
   Remove-Item C:\develop\kev-bundle\python\Lib\EXTERNALLY-MANAGED
   ```

4. 上流の Kev を `pins.json` のコミットにしてから、その Python に kev を入れ、torch を `pins.json` の版にそろえる（kev の指定 `torch<2.9` との食い違いの表示は想定どおり）。

   ```powershell
   git -C C:\develop\kev-local checkout --detach <pins.json の kev_commit>
   C:\develop\kev-bundle\python\python.exe -m pip install "C:\develop\kev-local[serve]"
   C:\develop\kev-bundle\python\python.exe -m pip install "torch==<pins.json の torch>" --index-url https://download.pytorch.org/whl/cpu
   C:\develop\kev-bundle\python\python.exe -m pip install "aiohttp>=3.14.4" "filelock>=4.2.1" "certifi>=2026.10.18" "setuptools>=84.1.0"
   ```

5. 起動用のファイルを置く。

   ```powershell
   Copy-Item <このリポジトリ>\tools\kev-bundle\* C:\develop\kev-bundle\
   ```

6. モデルを固定した版で取得し、照合する（Hugging Face から。約 9.5GB）。

   ```powershell
   C:\develop\kev-bundle\python\python.exe C:\develop\kev-bundle\kev-models.py fetch
   ```

7. 開発 PC で、ネットを切った状態でも起動するか確かめる（鍵は `start-kev.cmd` が作り、kimeru が読む）。`C:\develop\kev-bundle\start-kev.cmd` を実行し、`Uvicorn running on http://127.0.0.1:8009` と出たら、kimeru のフォルダで次を実行する。

   ```powershell
   python -m kimeru --backend kev run examples\teams_chat.json
   ```

</details>

## 管理された PC で使う

- フォルダを短い英数字のパス（例: `C:\kev`）にコピーし、`start-kev.cmd` をダブルクリックする。ウィンドウは開いたままにする。
- `start-kev.cmd` は `HF_HUB_OFFLINE=1`・`TRANSFORMERS_OFFLINE=1` を設定して起動し、127.0.0.1（この PC の中）だけで待ち受ける。起動の前に `head.pt` を照合し（違えば起動しない）、鍵（`%LOCALAPPDATA%\kev\api-key`）を用意する。
- コピーしたあと、1 回だけ全ファイルを照合する: `powershell -ExecutionPolicy Bypass -File <kimeru>\tools\sha256sums.ps1 -Check <コピーしたフォルダ>`。表示される一覧の指紋を、`tools\make-bundle.ps1` が開発 PC で表示した指紋と比べる。
- 持ち込みの全体の手順（kimeru・az と一緒に分割して運ぶ方法）は [managed-pc-check.md](managed-pc-check.md) を見る。
- 組織のルール（持ち込み・AI の利用）を先に確かめる。
