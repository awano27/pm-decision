# 管理された PC・閉域の環境で使う

> 導入する前に、組織のルール（ソフトウェアの持ち込み・画面の自動操作・AI の利用）と IT 部門の承認を確認してください。

**管理者権限なし・インストールなし。持ち込むのはフォルダ 1 つ、操作はダブルクリック 1 回。** 組織・学校・組織が管理する PC のように、ソフトを自由に入れられない環境向けです。

```powershell
# 開発 PC: kimeru・Kev・Azure CLI・Python を 1 つにまとめ、1GB ずつに分割
powershell -ExecutionPolicy Bypass -File tools\make-bundle.ps1 -Zip -KevSrc <Kev の持ち込み用フォルダ> -AzSrc <az の ZIP 展開先>
powershell -ExecutionPolicy Bypass -File tools\split-zip.ps1 -Zip C:\develop\kimeru-pc.zip   # → C:\develop\parts
```

`-KevSrc` / `-AzSrc` の既定値（`C:\develop\kev-bundle` と `C:\develop\az-bundle\az`）は作者の PC の置き場所です。Kev のフォルダは [Kev の手順](https://github.com/jaredpalmer/kev) で作り（`start-kev.cmd` と `models` を含む）、Azure CLI は Microsoft 公式の ZIP 版を展開したものを使います。

1. `parts` フォルダを、組織で認められた方法で管理された PC にコピー（1 ファイルが 1GB なので、途中で失敗しても該当ファイルだけ取り直せます）
2. `JOIN.cmd` をダブルクリック → 結合・破損チェック・`C:\kimeru-pc` へ展開・`START.cmd` を起動
3. `START.cmd` が Kev の起動 → az サインイン（聞かれたら `y`、ブラウザで職場・学校のアカウント）→ 動作確認（Teams・Kev・ADO・Copilot・PC 通知）まで進め、結果シートをクリップボードに入れます。確認の質問に Enter だけ答えると、その項目は「SKIP」になります

詳細と切り分け: [managed-pc-check.md](managed-pc-check.md)

---
