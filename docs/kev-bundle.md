# Kev の持ち込み用フォルダの作り方

ネットにつながる開発 PC で一度だけ作り、管理された PC へコピーして使うフォルダです。できあがりは、インストール・管理者権限・ネット接続なしで Kev（この PC の中で動く判断モデル）を起動できます。`tools\make-bundle.ps1 -KevSrc <このフォルダ>` が前提にしている形です。

> この手順は、作者の開発 PC にある持ち込み用フォルダ（2026-09 作成）の中身から書き起こしたものです。新しい PC でこの文書だけを見て最初から作り直す確認は、まだしていません。うまくいかなかった点は Issue で知らせてください。上流の手順は [jaredpalmer/kev](https://github.com/jaredpalmer/kev) の README（2026-10 時点で Python 3.12 か 3.13 と uv が前提）に従います。

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

1. 上流のリポジトリを取得し、上流の手順で一度動かす（Python 3.13 と uv が入る）。

   ```powershell
   git clone https://github.com/jaredpalmer/kev C:\develop\kev-local
   cd C:\develop\kev-local
   uv sync --extra serve
   ```

2. 単体版の Python を持ち込み用フォルダへ写す。uv が入れた Python 3.13（`uv python find 3.13` が場所を示す）のフォルダを、まるごと `kev-bundle\python\` にコピーする。

3. その Python に torch（CPU 版）と kev を入れる。

   ```powershell
   C:\develop\kev-bundle\python\python.exe -m pip install torch --index-url https://download.pytorch.org/whl/cpu
   C:\develop\kev-bundle\python\python.exe -m pip install "C:\develop\kev-local[serve]"
   ```

4. モデルを取得して `models\` に置く（Hugging Face から。約 9.5GB）。

   ```powershell
   C:\develop\kev-bundle\python\python.exe -c "from huggingface_hub import snapshot_download as s; s('jaredpalmer/kev-4b', local_dir=r'C:\develop\kev-bundle\models\kev-4b'); s('Qwen/Qwen3.5-4B-Base', local_dir=r'C:\develop\kev-bundle\models\Qwen3.5-4B-Base')"
   ```

5. 起動用のファイルを置く。

   ```powershell
   Copy-Item <このリポジトリ>\tools\kev-bundle\* C:\develop\kev-bundle\
   ```

6. 開発 PC で、ネットを切った状態でも起動するか確かめる。`C:\develop\kev-bundle\start-kev.cmd` を実行し、`Uvicorn running on http://127.0.0.1:8009` と出たら、kimeru のフォルダで次を実行する。

   ```powershell
   python -m kimeru --backend kev run examples\teams_chat.json
   ```

## 管理された PC で使う

- フォルダを短い英数字のパス（例: `C:\kev`）にコピーし、`start-kev.cmd` をダブルクリックする。ウィンドウは開いたままにする。
- `start-kev.cmd` は `HF_HUB_OFFLINE=1`・`TRANSFORMERS_OFFLINE=1` を設定して起動し、127.0.0.1（この PC の中）だけで待ち受ける。
- 持ち込みの全体の手順（kimeru・az と一緒に分割して運ぶ方法）は [managed-pc-check.md](managed-pc-check.md) を見る。
- 組織のルール（持ち込み・AI の利用）を先に確かめる。
