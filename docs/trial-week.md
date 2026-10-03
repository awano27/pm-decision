# 1 週間の試し方

最初の試行では対象 ADO プロジェクトを 1 つに絞り、情報不足の確認依頼を扱います。導入前後の作業時間、下書きの利用状況、通知を本人が受け取ったか、作業を手動で完了したかを別々に記録します。1 週間は任意の観察期間で、改善や精度を約束するものではありません。追加シナリオを試す場合は pilot の結果と分けてください。**trial 集計は本文を共有しません**が、通常の case / `out` のファイルには本文が含まれる場合があります。

通知受理は端末受信の証拠ではなく、端末受信も case 完了の証拠ではありません。これらは本人の確認を別々に記録してください。[要件ドラフト](requirements.md) は case の材料を明示的にローカルへ永続保存するので、作成前に保存先を確認します。

## 準備（初日、10 分）

```powershell
python -m kimeru config set backend kev          # 判断モデル（Kev を使うとき）
python -m kimeru config set writer m365          # 文面の下書き。使えるものを選ぶ（README の「文面の下書き」）
python -m kimeru --out <出力先> config show       # 効いている設定と、その出どころ
python -m kimeru schedule install                # 5 分ごとの自動運転（設定は設定ファイルで引き継がれる）
```

## 毎日（合計 5 分）

1. iPhone で、確認待ちに `OK N` / `NG N` / `保留 N` / `修正 N <指示>` / `聞き返し N` を返す
2. 夕方、PC の端末で、自動で決まった判断を確かめる

```powershell
python -m kimeru review            # 1 件ずつ「合っている / 違う / 分からない」。違うときは、正しい答えを選ぶ（20 件まで。--limit で変える）
```

結果全体が違っていても、誤りを質問単位のラベルにできないときは、確認時に `w`（最終結果が違う）を選びます。これは最終結果の誤りとして記録し、質問単位の正誤ラベルは作りません。

case ごとの before/after の時間と下書き利用状況は手動で計測し、次のローカルコマンドに明示します。これらの値は業務成果の自動測定ではありません。

```powershell
python -m kimeru trial record --case 1 --before-minutes 18 --after-minutes 12 --draft edited --outcome correct
python -m kimeru trial report
```

通知経路の設定状態を読むだけなら `python -m kimeru onboarding status` を使います。通知を送る操作は別の `onboarding notification-test --send` で、送信受理後に端末を本人が確認してから `onboarding confirm TOKEN` を実行します。dry run / status は送信しません。pilot の case を自動的に完了にする機能ではありません。

Teams は使いません。確かめた結果は、状態フォルダの `fixtures_user.jsonl`（評価用の形）と `reviews.jsonl`（正誤と、その判断をした判断モデルの名前）に、PC の中だけで残ります。元のイベントは、既定では要約の長さ（120 文字）までしか記録に残りません。長い依頼の全文を評価用の例に残したいときだけ、`python -m kimeru config set record_event_full 1` にします（記録が、それだけ敏感になります: [SECURITY.md](../SECURITY.md#記録に残る範囲と期間)）。

## 1 週間後

```powershell
python -m kimeru digest --week           # 件数、自動と人の割合、規則で決めた件数、重大な通知、承認の結果、一致率
python -m kimeru digest --week --share   # Issue に貼れる形（数値と環境だけ。追加したグラフの名前は伏せます）
python -m kimeru trial report --share    # 集計のみ。Jev と判断モデル不明の試行は除外
python -m kimeru calibrate               # 係数を調整できるか（モデルは呼びません）
python -m kimeru config show --share     # Issue に貼れる設定の一覧（パス・組織・サブスクリプションは出ません）
```

`trial report --share` は個別 case の計測ペアを出さず、Jev と判断モデルの出所が分からない試行を除いて集計します。導入前の時間が未記録なら、時間差は `Unknown` のままです。承認から引き渡しまでの時間も証拠がない場合は `Unknown` と表示されます。

引き渡しの集計は、ブリッジによる画面読み戻しと、本人が「自分とのチャットに表示された」と申告した記録を区別して保存します。どちらも自分とのチャットへの引き渡し確認であり、ADO 側の作業完了の証拠ではありません。

- `calibrate` は、正誤を付けた質問が **100 問** 以上あるときだけ、変更を提案します。足りなければ、足りない数を表示して、何も変えません。
- 提案は、データを 2 つに分け、片方で探し、もう片方で確かめて、確信して間違える件数が増えないときだけ出ます。
- 既定では、**人に回す件数が増える方向（慎重な方向）** の変更だけを提案します。自動で決める範囲を広げる変更は、`--allow-wider` を付けたときだけです。
- 判断モデルが混ざった記録は、判断モデルごとに分けて探します。stub など、判断モデルの分からない記録からは、探さず、適用せず、理由を出します。
- 適用は、あなたが決めます: `python -m kimeru calibrate --apply`。ローカルの上書きファイル（`thresholds.json`）に保存し、グラフと `profiles.py` は書き換えません。戻すには `python -m kimeru calibrate --revert`。

## 結果の報告（任意）

`digest --week --share` の出力を、[利用結果の報告](https://github.com/awano27/pm-decision/issues/new/choose) に貼ってください。本文・人名・件名・ID は出力に含まれません（貼る前に、目で確かめてください）。
判断の一致率は、Jev の判断を数えません（TypeSafe の利用規約: Jev の性能の数値は公開できません）。不具合の報告には、`config show` ではなく `config show --share` の出力を貼ってください（`config show` には、パスや組織の名前が入ります）。

画面操作や通知の環境ごとの結果は、「動作環境の報告」に書けます。
