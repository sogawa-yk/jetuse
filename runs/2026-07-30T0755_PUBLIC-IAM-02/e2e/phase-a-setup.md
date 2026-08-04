# Phase A: 制限環境の用意（テナンシ管理者として実施）

実施日 2026-07-30 / テナンシ DEPLOYTEST（ホーム `ca-toronto-1`）/ 配備先 `us-chicago-1`。
OCID・鍵・パスワードは記録しない（リポジトリ規約）。

## 作ったもの

| 種別 | 名前 | 場所 |
|---|---|---|
| コンパートメント | `jetuse-restricted` | root 直下（新規） |
| グループ | `jetuse-deployers` | 既定ドメイン |
| ユーザー | `jetuse-deployer`（API キーのみ・コンソールパスワードなし） | 既定ドメイン |
| Dynamic Group | `jetuse-restricted-dg`（**単一・compact 構成**） | root（テナンシ） |
| Policy | `jetuse-restricted-tenancy-policy` | root（テナンシ） |
| Policy | `jetuse-restricted-deployer-policy` | root（テナンシ） |

`jetuse-restricted-dg` の matching rule（7 resource-type。ホスト型エージェント3型を含む＝PORT-03）:

```text
Any {all {resource.type='computecontainerinstance', resource.compartment.id='<COMPARTMENT_OCID>'},
     all {resource.type='fnfunc', resource.compartment.id='<COMPARTMENT_OCID>'},
     all {resource.type='autonomousdatabase', resource.compartment.id='<COMPARTMENT_OCID>'},
     all {resource.type='generativeaisemanticstore', resource.compartment.id='<COMPARTMENT_OCID>'},
     all {resource.type='generativeaihostedapplication', resource.compartment.id='<COMPARTMENT_OCID>'},
     all {resource.type='generativeaihostedapplicationiam', resource.compartment.id='<COMPARTMENT_OCID>'},
     all {resource.type='generativeaihosteddeployment', resource.compartment.id='<COMPARTMENT_OCID>'}}
```

`jetuse-restricted-tenancy-policy`（1文のみ）:

```text
Allow dynamic-group jetuse-restricted-dg to read objectstorage-namespaces in tenancy
```

`jetuse-restricted-deployer-policy`（6文）:

```text
Allow group jetuse-deployers to inspect compartments in tenancy
Allow group jetuse-deployers to inspect tenancies in tenancy
Allow group jetuse-deployers to read objectstorage-namespaces in tenancy
Allow group jetuse-deployers to manage orm-stacks in compartment id <COMPARTMENT_OCID>
Allow group jetuse-deployers to manage orm-jobs in compartment id <COMPARTMENT_OCID>
Allow group jetuse-deployers to manage all-resources in compartment id <COMPARTMENT_OCID>
```

**意図的に runtime policy は事前作成していない**。スタックの `enable_runtime_policy=true` が
コンパートメント内 policy を作れるか（論点C）を測るため。

## 制限ユーザーがテナンシ権限を持たないことの確認（否定検証）

`jetuse-deployer` の API キーで実行。すべて **404 NotAuthorizedOrNotFound**（OCI は権限不足を 404 で返す）。

| # | 操作 | 結果 |
|---|---|---|
| NEG-1 | root への Dynamic Group 作成 | 404 NotAuthorizedOrNotFound |
| NEG-2 | root コンパートメントへの Policy 作成 | 404 NotAuthorizedOrNotFound |
| NEG-3 | root 直下へのコンパートメント作成 | 404 NotAuthorizedOrNotFound |

肯定側の確認: `oci os ns get`（`read objectstorage-namespaces in tenancy` が効いている）が成功。

## 実機で判明した副次的な事実

- **IAM の書き込みはホームリージョンへ送る必要がある**。プロファイルの既定リージョン
  （`us-chicago-1`）のままだと `iam compartment create` が失敗する。`--region ca-toronto-1` が必要。
  → 案内ページの「ホームリージョンを確認する」チェック項目の根拠。
- 新規ユーザーの API キーは**アップロード直後は 401 NotAuthenticated** になる。
  実測で 2〜3 分後に成功。権限設定の誤りと区別がつかないため、案内に待ち時間を書く価値がある。

## 経路の同一性について

ボタン（コンソール）ではなく OCI CLI で Stack / Job を作成した。**認可経路は同一**
（同じユーザープリンシパル・同じ `resource-manager` API・同じ Terraform 実行環境）。
コンソール固有の UI 権限（`inspect compartments` で画面のコンパートメント選択が出るか等）は
別途 CLI では検証できないため、必要な read 文を案内ページに明記する形で担保する。
