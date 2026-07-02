# JetUse Dynamic Group Matching Rules（compact構成）

JetUseのDynamic Group数を抑えるため、社内の3コンパートメントを次の2つの信頼境界として扱う。

| Dynamic Group | 対象コンパートメント | 用途 |
|---|---|---|
| `jetuse-internal-dg` | `jetuse:dev`、`jetuse:public` | 社内開発環境と社内公開環境 |
| `jetuse-deploy-test-dg` | `jetuse:test` | 社外ユーザーのデプロイ・権限問題を再現する環境 |

この構成では、Container Instance、Functions、Autonomous Database、Semantic Storeを環境単位の1つのDynamic Groupへまとめる。

## 置換する値

Matching Ruleにはコンパートメント名ではなくOCIDを指定する。

| プレースホルダー | 値 |
|---|---|
| `<JETUSE_DEV_COMPARTMENT_OCID>` | `jetuse:dev`コンパートメントのOCID |
| `<JETUSE_PUBLIC_COMPARTMENT_OCID>` | `jetuse:public`コンパートメントのOCID |
| `<JETUSE_TEST_COMPARTMENT_OCID>` | `jetuse:test`コンパートメントのOCID |

OCIDの実値はドキュメントやGitリポジトリへコミットしない。

## 1. `jetuse-internal-dg`

`jetuse:dev`と`jetuse:public`に存在するJetUse Runtime / Data Principalを対象にする。

```text
Any {
  all {
    resource.type='computecontainerinstance',
    resource.compartment.id='<JETUSE_DEV_COMPARTMENT_OCID>'
  },
  all {
    resource.type='fnfunc',
    resource.compartment.id='<JETUSE_DEV_COMPARTMENT_OCID>'
  },
  all {
    resource.type='autonomousdatabase',
    resource.compartment.id='<JETUSE_DEV_COMPARTMENT_OCID>'
  },
  all {
    resource.type='generativeaisemanticstore',
    resource.compartment.id='<JETUSE_DEV_COMPARTMENT_OCID>'
  },
  all {
    resource.type='computecontainerinstance',
    resource.compartment.id='<JETUSE_PUBLIC_COMPARTMENT_OCID>'
  },
  all {
    resource.type='fnfunc',
    resource.compartment.id='<JETUSE_PUBLIC_COMPARTMENT_OCID>'
  },
  all {
    resource.type='autonomousdatabase',
    resource.compartment.id='<JETUSE_PUBLIC_COMPARTMENT_OCID>'
  },
  all {
    resource.type='generativeaisemanticstore',
    resource.compartment.id='<JETUSE_PUBLIC_COMPARTMENT_OCID>'
  }
}
```

### 注意点

このDynamic Groupに付与したdev向けPolicyとpublic向けPolicyは、グループに所属するすべてのResource Principalから利用できる。したがって、devのResource Principalがpublicコンパートメントのリソースへアクセスできる構成になる。

次の前提を満たす間だけ共有する。

- dev/publicが同じ社内の信頼境界である。
- publicに社外秘・顧客機密・本番データを格納しない。
- Policyの対象をdev/publicコンパートメント内に限定する。
- 将来publicを社外公開するときはDynamic Groupを分離する。

## 2. `jetuse-deploy-test-dg`

`jetuse:test`だけを対象にし、社外ユーザーから報告されたデプロイ・IAM問題の再現に使用する。

```text
Any {
  all {
    resource.type='computecontainerinstance',
    resource.compartment.id='<JETUSE_TEST_COMPARTMENT_OCID>'
  },
  all {
    resource.type='fnfunc',
    resource.compartment.id='<JETUSE_TEST_COMPARTMENT_OCID>'
  },
  all {
    resource.type='autonomousdatabase',
    resource.compartment.id='<JETUSE_TEST_COMPARTMENT_OCID>'
  },
  all {
    resource.type='generativeaisemanticstore',
    resource.compartment.id='<JETUSE_TEST_COMPARTMENT_OCID>'
  }
}
```

`jetuse:test`はdev/publicのPolicy対象に含めない。デプロイ手順、Dynamic Group構成、Policy文は社外ユーザーへ配布するIAM Bootstrapと同じにし、OCIDとリソース名だけを変更する。

## Resource Typeの対応

| Resource Type | JetUseでの用途 |
|---|---|
| `computecontainerinstance` | FastAPIを実行するContainer Instance |
| `fnfunc` | Functions Routerの各Function |
| `autonomousdatabase` | Select AI / DBMS_CLOUD_AIで使うADB Resource Principal |
| `generativeaisemanticstore` | SQL Search用Semantic Store |

Semantic Storeを使用しない環境でもmatching ruleを残して問題ない。対象リソースが存在しなければDynamic Groupのメンバーにならない。

## Dynamic Groupへ含めないPrincipal

### API Gateway

API GatewayはDynamic Groupへ含めず、Functionsを呼び出す各コンパートメントに条件付きPolicyを設定する。

```text
Allow any-user to use functions-family in compartment id <COMPARTMENT_OCID>
where all {
  request.principal.type='ApiGateway',
  request.resource.compartment.id='<COMPARTMENT_OCID>'
}
```

このPolicyはdev、test、publicそれぞれのコンパートメントOCIDで作成する。

### Resource Managerのデプロイ担当者

Deploy to Oracle Cloudを実行する担当者はOCI IAMの通常グループへ所属させる。Dynamic Groupには含めない。必要なユーザーPolicyは [Public版 IAM要件](./public-iam-requirements.md) を参照する。

## 社外ユーザー環境

社外ユーザーが自分のOCIテナンシへJetUseをデプロイする場合は、そのユーザーのJetUse専用コンパートメントを対象とするDynamic Groupを1個作成する。社外ユーザー側のDynamic Groupは相手のテナンシに作られるため、JetUse管理側テナンシのDynamic Group上限は消費しない。

## Terraform実装との関係

現在の `infra/terraform/modules/iam` と `infra/orm-bootstrap` は、Runtime / ADB / Semantic Storeを分離するstrict構成である。この文書のcompact構成を自動作成するには、Terraformへ次のような切替を追加する必要がある。

```hcl
dynamic_group_mode = "combined" # 1環境1Dynamic Group
dynamic_group_mode = "strict"   # Runtime / ADB / Semantic Storeを分離
```

社外ユーザーの問題を`jetuse:test`で再現するため、Public配布時の既定値とtest環境の値は同じにする。

## 参考

- [OCI: Writing Matching Rules to Define Dynamic Groups](https://docs.oracle.com/iaas/Content/Identity/dynamicgroups/Writing_Matching_Rules_to_Define_Dynamic_Groups.htm)
- [Public版 IAM要件](./public-iam-requirements.md)
- [IAM Bootstrap詳細](./iam.md)
