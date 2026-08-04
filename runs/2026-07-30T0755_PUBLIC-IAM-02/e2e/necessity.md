# 必要性の検証（各文を外して壊れ方を測る）

## 0. 最初の測定は無効だった（記録として残す）

`oci iam policy update --policy-id … --statements file://… --force` は
**`--version-date` を同時に指定しないと何も更新しない**:

```text
If updating either statements or version date, both parameters must be specified.
```

スクリプトがこの標準エラーを `>/dev/null 2>&1` で捨てていたため、ポリシーは 6 文のまま変わらず、
「文を外しても plan が通る」という**誤った結果**を4ケースぶん出していた。
`oci iam policy get --query 'length(data.statements)'` が 6 のままだったことで発覚。

対策: 各ケースで **意図した文数になったことをアサートしてから** IAM 反映を待つ。
以下は測り直し後の結果。

## 1. デプロイ担当グループのテナンシスコープ文（plan で測定）

コンパートメント3文は常に付与。テナンシ文だけを入れ替え、各ケースで 300 秒待ってから plan。

| ケース | 付与したテナンシ文 | plan | job |
|---|---|---|---|
| NONE | なし | **FAILED** | `…ocawbqna` |
| TENANCIES | `inspect tenancies in tenancy` | **SUCCEEDED** | `…4lmmvlyq` |
| OSNAMESPACE | `read objectstorage-namespaces in tenancy` | FAILED | `…nkioa5lq` |
| COMPARTMENTS | `inspect compartments in tenancy` | FAILED | `…xfko5daa` |

結論: **`inspect tenancies in tenancy` の1文だけが必須**。他2文は代替にならない。

### NONE / OSNAMESPACE / COMPARTMENTS の失敗内容（修正前コード）

```text
Error: Resource precondition failed
  on main.tf line 10, in resource "terraform_data" "region_guard":
  10:       condition     = contains(local.ocir_supported_region_keys, local.deploy_region_key) || (...)
    │ local.deploy_region_key is ""
    │ local.ocir_supported_region_keys is tuple with 4 elements
（メッセージ本文: 「JetUse のワンクリックデプロイは ap-osaka-1 / ap-tokyo-1 / us-ashburn-1 /
  us-chicago-1 のみ対応です…」）

Error: Resource precondition failed
  on main.tf line 17 …（メッセージ本文: 「リージョン … は GenAI が未検証です」）

Error: Iteration over null value
  on providers.tf line 32, in provider "oci":
  32:   region = [for r in data.oci_identity_region_subscriptions.this.region_subscriptions : r.region_name if r.is_home_region][0]
    │ data.oci_identity_region_subscriptions.this.region_subscriptions is null
```

**権限不足でも data source は 401/404 を返さず `null` を返す**ため、利用者には
「このリージョンは未対応」という誤った理由が3件も表示される。実際の原因は
`inspect tenancies in tenancy` の欠落。→ スタックを修正（`fix-verification.md`）。

## 2. Dynamic Group 向けの `read objectstorage-namespaces in tenancy`

| # | 測定 | 結果 |
|---|---|---|
| 2-1 | root の当該 Policy を削除し 7 分待って RAG 一覧・アップロード | どちらも **200** |
| 2-2 | 同じ状態で議事録の音声登録 → ジョブ最終状態 | 登録 200 / ジョブ **`completed`** |
| 2-3 | **この権限を一度も持たない新規プリンシパル**（`manage all-resources in compartment` 1文のみ）で `GetNamespace` | **成功**（コンパートメント指定の有無を問わず namespace を返す） |

2-3 は「取り消しの反映遅延で 2-1/2-2 が通っただけ」という疑いを排除するために実施した。

→ **この1文は不要**。事前作成の依頼対象から外した。

補足: `rag.py` の原本バックアップ/削除は `except Exception` で握り潰すため、ここが権限で落ちても
HTTP 200 のまま静かに劣化する。判断は握り潰さない経路（議事録）と 2-3 に基づく。
