# 実環境検証の範囲（ADR-0031 / 共有基盤の ORM 化）

この差分は「配備の仕組み」そのものなので、実機で回す以外に確かめようがない。
以下は本物の OCI に対する実行結果で、**生ログは `logs/` に置いてある**（OCID はマスク済み。
ファイル名の末尾が ORM の job ID）。

**やっていないことが1つある。この差分の中心的な主張なので、はっきり書いておく。**

「internal-dev の principal から public-dev の資源を叩いて **403 になる**」ことは**測っていない**。
確認したのは IAM の定義（動的グループの matching rule とポリシーの付与先）だけで、
**定義上は閉じているが、挙動としては未検証**。

実測するには、片方の環境に resource principal を持つワークロード（Container Instance か
Function）を立て、そこから他方のバケット／ADB を叩いて 403 を得る必要がある。
`jetuse-` プレフィックスの検証資源作成は認められているので**権限上の制約は無い**。
やらなかったのは単に工程を積まなかったからで、正当化しない。**この差分の中心的な主張が
未実測のまま残っている**ことを、レビューでも3回続けて指摘されている（review-9/10/11 の major）。
**次に public-dev へアプリを配備するとき、その場で1本測るのが最も安い**（両環境に CI が建つため）。
手順:

```
# public-dev の CI から自分のバケットを読む → 200 が期待値
# internal-dev の CI から public-dev のバケットを読む → 403 が期待値
oci os object list --bucket-name jetuse-pubdev-app-data --auth resource_principal
```

ADR-0031 の open item にも載せた。

## internal-dev の移行（既存 35 資源を作り直さずに ORM へ）

| | 内容 | 結果 |
|---|---|---|
| 1 | 移行前の前提確認（ローカル state と実体の一致） | `terraform plan` → **No changes** |
| 2 | 空 state に対する import の dry run（ORM 初回 plan の予行） | `28 to import, 4 to add, 2 to change, **0 to destroy**` |
| 3 | ORM 側の plan | 同一（`Plan: 28 to import, 4 to add, 2 to change, 0 to destroy`） |
| 4 | ORM 側の apply | `Apply complete! Resources: 1 added, 0 changed, 0 destroyed`（残りは前段で適用済み） |
| 5 | **完了条件**: imports.tf 抜きで再 plan | **No changes** |
| 6 | ORM state と旧ローカル state の突き合わせ | 差は `time_offset.spa_par[0]` の1件のみ（`spa_par_expiry` 明示による意図どおりの不在）。`vcn_id` / `private_subnet_id` / `app_nsg_id` / `adb_id` / `apigw_hostname` は一致 |

## public-dev の新規構築

| | 内容 | 結果 |
|---|---|---|
| 7 | 初回 plan | `32 to add` ← **IAM が 0 件**。既定 false のまま渡していなかった |
| 8 | IAM を渡して再 plan | `38 to add`（動的グループ3・ポリシー2を含む） |
| 9 | apply | **失敗**。`DynamicResourceGroups` の quotaExceeded（テナンシに 50 本・48 本は他プロジェクト） |
| 10 | 既存 `jetuse-internal-dg` を共用して再 plan | `1 to add`（前段で 35 資源は作成済み。残りはランタイムポリシー1本）。**この共用構成は最終形ではない** —— シナリオ16 で public-dev 専用 DG に差し替えた |
| 11 | apply | `Apply complete! Resources: 1 added, 0 changed, 0 destroyed` |
| 12 | **完了条件**: 再 plan | **No changes** |

`jetuse-internal-dg` の matching rule は 5 コンパートメント全部（`public-dev` 含む）を
既に含んでいたため、IAM 側の変更は不要だった。

## 道具の実測

| | 内容 |
|---|---|
| 13 | `ops/orm-stack.sh <env> state` が **state だけを stdout に出す**（進捗は stderr）。`ops/dev-env-up.sh` が JSON として読めることを確認 |
| 14 | `ops/start-adb-if-stopped.sh` が **シカゴの ADB を見つけて起動**する（修正前は既定リージョン=大阪しか見ず「nothing to do」で終了していた） |
| 15 | `_region.sh` の対応表 4 リージョンと fail-closed（未対応リージョンは exit 1） |

## この工程で見つけて直した欠陥

**どれも実行して初めて分かった。** 机上では出ない。

| 症状 | 原因 |
|---|---|
| `STATE?: unbound variable` | 変数展開の直後に全角括弧。zsh がその先頭バイトまで変数名として読む |
| 413 RequestEntityTooLarge | `cp -R modules` が `.terraform` のプロバイダキャッシュ 502MB を同梱（実体は 328KB） |
| `Plan:` 行が読めない | ジョブログがページングで打ち切られる。`--all` と要約抽出が要る |
| apply が 409 IncorrectState | ADB が夜間停止。プリフライトで投げる前に止める |
| **`start-adb-if-stopped.sh` が無効** | 既定リージョンしか見ず、シカゴ移行後は対象を見つけられないまま正常終了 |
| `date -Is` が落ちる | GNU 専用。macOS(BSD date) では invalid argument |
| スタック変数が消える | env の付け忘れで `spa_par_expiry` が消え、PAR が置き換え対象に戻る |
| 変数を消せない | 上の対策（マージ方式）が生んだ穴。`${VAR:-}` では「未設定」と「空」が同じになる |
| `state` が JSON でない | 進捗メッセージが stdout に混ざる |
| **IAM 抜きの環境ができる** | `enable_dynamic_group` の既定が false。渡さないと黙って権限無しの環境が建つ |
| `ops/` にリージョン直書き | 私の修正が AGT-06 の集約を壊した。`make test` が検出（`test_ops_region.py`） |

## 生ログ（logs/）

ファイル名は `orm-<env>-<action>-<job id 末尾12桁>.log`。ORM のジョブ履歴から辿れる。

| ログ | 判定 |
|---|---|
| `orm-internal-dev-apply-vj2fhqscne5q.log` | **FAILED** — ADB 停止で 409 IncorrectState（プリフライトを入れる契機） |
| `orm-internal-dev-apply-4ic62senmpsq.log` | `Apply complete! Resources: 0 added, 1 changed, 0 destroyed` |
| `orm-internal-dev-plan-ykbsfcvq2xyq.log` | 取り込み完了後の **No changes** |
| `orm-internal-dev-plan-avgrdwb2id3a.log` | レビュー是正後も **No changes**（回帰していない） |
| `orm-public-dev-plan-g3uqon73mteq.log` | `32 to add` ← **IAM が 0 件**だった回 |
| `orm-public-dev-plan-jmeu2xhzb7zq.log` | `38 to add`（IAM 5点を含む） |
| `orm-public-dev-apply-tc6vu6lar67q.log` | **FAILED** — DynamicResourceGroups quotaExceeded |
| `orm-public-dev-apply-ymd4lxwo3uea.log` | `Apply complete! 0 added, 1 changed, 0 destroyed`（ポリシーを専用 DG へ差し替え） |
| `orm-public-dev-plan-uhhxs76b66zq.log` | **No changes** |
| `destroy.log` | `Destroy complete! Resources: 181 destroyed`（`jetuse:public` の撤去） |

## レビューで直した点（review-1 → review-3）

| 指摘 | 対応 |
|---|---|
| 共有 DG が5面を拾い、権限境界が成立しない | 環境ごとに専用 DG。テナンシ直下の blanket 文も削除（シナリオ16） |
| 既存スタック変数の取得失敗を `{}` に潰す | fail-closed。取得・解析どちらの失敗でも止める |
| ADB パスワードが argv に露出 | 環境変数で渡し、0600 の一時ファイルを `file://` で CLI へ |
| ADB プリフライトが `data[0]` 任せ | `${PREFIX}-adb` で絞り、CLI 失敗・複数件も停止 |
| `mktemp` の戻り値に拡張子を後置 | mktemp のパスをそのまま使い、0600 と `trap` で削除 |
| `find_stack` が CLI 失敗・不正 JSON を「0 件」と読む | どちらも非ゼロ終了 |
| `JETUSE_SHARED_DYNAMIC_GROUP` で境界を破れる | override を廃止 |
| スタック所在と配備先リージョンが同一変数 | `ORM_STACK_REGION` と `region` を分離 |
| ADB ヘルパが各リージョンの CLI エラーを握り潰す | env ごとに探索成否と検出数を数え、失敗は非ゼロ |
| `2>&1` で「該当0件」の案内文を値に混ぜる | stderr を別ファイルへ（誤って「複数リージョンにある」と判定していた） |
