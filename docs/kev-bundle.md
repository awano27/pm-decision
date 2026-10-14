# Kev の持ち込み用フォルダの作り方

ネットにつながる開発 PC で一度だけ作り、管理された PC へコピーして使うフォルダです。できあがりは、インストール・管理者権限・ネット接続なしで Kev（この PC の中で動く判断モデル）を起動できます。`tools\make-bundle.ps1 -KevSrc <このフォルダ>` が前提にしている形です。

> この手順は、作者の開発 PC にある持ち込み用フォルダ（2026-09 作成）の中身から書き起こしたものです。うまくいかなかった点は Issue で知らせてください。上流の手順は [jaredpalmer/kev](https://github.com/jaredpalmer/kev) の README（2026-10 時点で Python 3.12 か 3.13 と uv が前提）に従います。

## できあがりの形

```text
kev-bundle\
  python\                 Python 3.13 の単体版（移動できるもの）＋ torch（CPU 版）＋ kev ＋ fastapi など
  models\kev-4b\          Kev-4B（判断用アダプター。約 0.2GB）
  models\Qwen3.5-4B-Base\ 土台のモデル（約 9.3GB）
  start-kev.cmd           起動（オフライン。精度を自動で選ぶ）
  localize.py             置いた場所に合わせてモデルの参照先を書き換える（起動のたびに自動）
  pick-dtype.py           この CPU で bf16 が速いかを判定する
  dtype-test.cmd          どちらの精度で起動するかを表示するだけ
```

| | 作者環境での値 |
|---|---|
| ディスク | 約 11.4GB（python 約 1.9GB、土台のモデル約 9.3GB、Kev-4B 約 0.2GB） |
| メモリ（起動後） | bf16 で約 10GB、fp32 で約 14GB |
| 取得にかかる時間 | 未測定（回線による。土台のモデルが約 9.3GB） |
| 起動にかかる時間 | 未測定（初回の読み込みは数分かかる、という観察だけ） |

`start-kev.cmd`・`localize.py`・`pick-dtype.py`・`dtype-test.cmd` は、このリポジトリの `tools\kev-bundle\` にあります。

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

4. その Python に torch（CPU 版）と kev を入れる。

   ```powershell
   C:\develop\kev-bundle\python\python.exe -m pip install torch --index-url https://download.pytorch.org/whl/cpu
   C:\develop\kev-bundle\python\python.exe -m pip install "C:\develop\kev-local[serve]"
   ```

5. モデルを取得して `models\` に置く（Hugging Face から。約 9.5GB）。

   ```powershell
   C:\develop\kev-bundle\python\python.exe -c "from huggingface_hub import snapshot_download as s; s('jaredpalmer/kev-4b', local_dir=r'C:\develop\kev-bundle\models\kev-4b'); s('Qwen/Qwen3.5-4B-Base', local_dir=r'C:\develop\kev-bundle\models\Qwen3.5-4B-Base')"
   ```

6. 起動用のファイルを置く。

   ```powershell
   Copy-Item <このリポジトリ>\tools\kev-bundle\* C:\develop\kev-bundle\
   ```

7. 開発 PC で、ネットを切った状態でも起動するか確かめる。`C:\develop\kev-bundle\start-kev.cmd` を実行し、`Uvicorn running on http://127.0.0.1:8009` と出たら、kimeru のフォルダで次を実行する。

   ```powershell
   python -m kimeru --backend kev run examples\teams_chat.json
   ```

</details>

## 管理された PC で使う

- フォルダを短い英数字のパス（例: `C:\kev`）にコピーし、`start-kev.cmd` をダブルクリックする。ウィンドウは開いたままにする。
- `start-kev.cmd` は `HF_HUB_OFFLINE=1`・`TRANSFORMERS_OFFLINE=1` を設定して起動し、127.0.0.1（この PC の中）だけで待ち受ける。
- 持ち込みの全体の手順（kimeru・az と一緒に分割して運ぶ方法）は [managed-pc-check.md](managed-pc-check.md) を見る。
- 組織のルール（持ち込み・AI の利用）を先に確かめる。
