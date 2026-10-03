# 要件ドラフト

`requirements` は既存 case からローカルの要件案を作り、人が編集・承認したファイルのハッシュを記録します。case 自体の承認状態は問いません。LLM・judge・Teams 投稿・ADO 書き込みは呼びません。要件の検討を依頼されたときの会話用 playbook（[シナリオ](scenarios/requirements-review.md)）とは別の機能です。

## オフライン walkthrough

1. 既存 case がある出力先を指定して、回答 JSON を準備します。case は承認済みでなくても構いません。キーはすべて必須で、値は空文字列を含まない文字列の配列です。

```json
{
  "must_have": ["CSV export includes the selected date range"],
  "optional": [],
  "out_of_scope": ["Changing the source records"],
  "acceptance_criteria": ["The selected date range appears in the exported CSV"],
  "open_questions": ["Which date formats must be supported?"]
}
```

2. `python -m kimeru --out out requirements build 1 --answers answers.json` は `out/requirements/1.md` と `out/requirements/1.json` を作ります。省略した `--answers` では回答配列は空で、空のカテゴリは `Unknown` として Markdown に示され、JSON の `unknown_categories` と `generated_open_questions` に残ります。利用可能な case 材料と参照先も両形式に含めます。pending の `full_text.json` がその時点で残っていれば、この明示的な build 出力にも複製されます。

3. Markdown / JSON を開き、内容を人が確認・編集します。もう一度 build して既存ファイルを置き換える場合は `--force` を明示します。入力 JSON の不正、存在しない case、または上書き拒否では出力を作りません。

4. `python -m kimeru --out out requirements approve 1` で、現在の両ファイルの SHA-256 を `out/requirements/approvals.json` に記録します。`python -m kimeru --out out requirements status 1` が `approved` と表示すれば両ハッシュが一致し、どちらかを編集したあとは `unapproved` と表示します。

この承認はローカルの成果物だけに対するものです。元 case の `OK`、Teams の状態、外部書き込み許可、ADO 登録を変更しません。生成されたファイルは `full_text.json` の通常 TTL に連動せず、手動削除まで残るため、本文を保存する前に保存先と社内ルールを確認してください。

## 1 プロジェクトの初回試行

試行は、対象 ADO プロジェクトを 1 つに絞り、情報不足の確認依頼が来たケースから始めます。比較する前後の対象作業を同じ種類にそろえ、各ケースについて手動で次を記録します。

- kimeru を使わない場合に確認へかかった分数 (`--before-minutes`)
- kimeru の確認後にかかった分数 (`--after-minutes`)
- 下書きをそのまま利用・修正して利用・利用せず (`--draft`)
- 結果が確認済みか、不明か (`--outcome`)

完了と通知受信は、この trial 記録だけから推定しません。通知の送信受理と端末での受信は別に記録し、case の手動完了も担当者が実際に確認します。任意の追加シナリオへ広げる場合は、最初の pilot と結果を混ぜずに記録します。小さな標本や未知の結果から性能・業務効果を保証しません。

詳細なコマンドと share 出力の制限は [1 週間の試し方](trial-week.md) を参照してください。
