# JetUse Dynamic Group Matching Rules（compact構成）

JetUseのDynamic Group数を抑えるため、社内の3コンパートメントを次の2つの信頼境界として扱う。

コンパートメントは**「壊してよいか」の1軸**で切っており、ブランチとは対応させない（軸が直交するため。ADR-0028）。

| コンパートメント | 役割 |
|---|---|
| `jetuse:dev` | 壊してよい。開発者ごとの app スタック / ループの E2E / 使い捨て検証 |
| `jetuse:test` | 社外ユーザーの権限を再現し、Public の Deploy を検証する |
| `jetuse:registry` | 公開配布物（OCIR イメージ）の置き場。**環境ではない**。外部ユーザーが認証なしで pull する先 |

> `jetuse:registry` は 2026-08-04 に `jetuse:public` から改名した。中身は `jetuse-api` / `jetuse-fn-router` / `jetuse-agent-{openai,langgraph,adk}` の5リポジトリのみで、すべて公開設定。**「社内公開環境」という旧記述は誤り**だった（社内でも環境でもない）。OCID は不変で、ポリシーは `in compartment id <OCID>` 形式のため改名の影響を受けない。OCIR の URL も `<region>.ocir.io/<tenancy-namespace>/<repo>` でコンパートメント名を含まない。

| Dynamic Group | 対象コンパートメント | 用途 |
|---|---|---|
| `jetuse-internal-dg` | `jetuse:dev`、`jetuse:registry` | 社内開発環境と、公開配布物（OCIR イメージ）の置き場 |
| `jetuse-deploy-test-dg` | `jetuse:test` | 社外ユーザーのデプロイ・権限問題を再現する環境 |

この構成では、Container Instance、Functions、Autonomous Database、Semantic Storeを環境単位の1つのDynamic Groupへまとめる。

> **未解決（2026-08-04・IAM 変更は人間ゲート）**: `jetuse:registry` は OCIR リポジトリしか置かない
> 非実行環境なので、Container Instance / Functions / ADB / Semantic Store を対象にする
> `jetuse-internal-dg` の Matching Rule と、Functions 呼び出し用の `any-user` Policy は
> **現時点で効果が無い**。将来 registry に Resource Principal が誤って配置されると自動的に
> 加入し、dev 側と同じ権限を得てしまう。**registry を両者の対象から外す**のが筋だが、
> IAM 変更は承認が要るため本ドキュメントでは論点として記録するに留める。

## 置換する値

Matching Ruleにはコンパートメント名ではなくOCIDを指定する。

| プレースホルダー | 値 |
|---|---|
| `<JETUSE_DEV_COMPARTMENT_OCID>` | `jetuse:dev`コンパートメントのOCID |
| `<JETUSE_REGISTRY_COMPARTMENT_OCID>` | `jetuse:registry`コンパートメントのOCID（旧 `jetuse:public`。2026-08-04 に改名） |
| `<JETUSE_TEST_COMPARTMENT_OCID>` | `jetuse:test`コンパートメントのOCID |

OCIDの実値はドキュメントやGitリポジトリへコミットしない。

## 1. `jetuse-internal-dg`

`jetuse:dev`と`jetuse:registry`に存在するJetUse Runtime / Data Principalを対象にする。

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
    resource.compartment.id='<JETUSE_REGISTRY_COMPARTMENT_OCID>'
  },
  all {
    resource.type='fnfunc',
    resource.compartment.id='<JETUSE_REGISTRY_COMPARTMENT_OCID>'
  },
  all {
    resource.type='autonomousdatabase',
    resource.compartment.id='<JETUSE_REGISTRY_COMPARTMENT_OCID>'
  },
  all {
    resource.type='generativeaisemanticstore',
    resource.compartment.id='<JETUSE_REGISTRY_COMPARTMENT_OCID>'
  }
}
```

### 注意点

このDynamic Groupに付与したdev向けPolicyとregistry向けPolicyは、グループに所属するすべてのResource Principalから利用できる。したがって、devのResource Principalがregistryコンパートメントのリソースへアクセスできる構成になる。

次の前提を満たす間だけ共有する。

- dev/registryが同じ社内の信頼境界である。
- registryには公開配布用のコンテナイメージだけを置き、社外秘・顧客機密・本番データを格納しない。
- Policyの対象をdev/registryコンパートメント内に限定する。
- registry の OCIR は既に外部から pull 可能（配布に必要）。イメージ以外を置くときはDynamic Groupを分離する。

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

`jetuse:test`はdev/registryのPolicy対象に含めない。デプロイ手順、Dynamic Group構成、Policy文はPublic版のIAM Terraform moduleと同じにし、OCIDとリソース名だけを変更する。

## Resource Typeの対応

| Resource Type | JetUseでの用途 |
|---|---|
| `computecontainerinstance` | FastAPIを実行するContainer Instance |
| `fnfunc` | Functions Routerの各Function |
| `autonomousdatabase` | Select AI / DBMS_CLOUD_AIで使うADB Resource Principal |
| `generativeaisemanticstore` | SQL Search用Semantic Store |
| `generativeaihostedapplication` | ホスト型エージェント本体（PORT-03 / ADR-0019） |
| `generativeaihostedapplicationiam` | ホスト型エージェントの実行時Resource Principal |
| `generativeaihosteddeployment` | ホスト型エージェントの配備単位 |

Semantic Storeを使用しない環境でもmatching ruleを残して問題ない。対象リソースが存在しなければDynamic Groupのメンバーにならない。

ホスト型エージェントを配備する環境では`generativeaihosted*`の**3型すべて**を含める。
`generativeaihostedapplicationiam`が欠けると、配備自体は成功するのに実行時のResource Principalが
Dynamic Groupに入らず、エージェントのGenerative AI / Object Storage / ADB呼び出しが権限エラーになる。
逆にエージェントを配備しない環境では3型を入れない（同じコンパートメントにある無関係な
Hosted ApplicationへJetUseのランタイム権限が付くのを避ける）。

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

このPolicyはdev、test、registryそれぞれのコンパートメントOCIDで作成する。

### Resource Managerのデプロイ担当者

Deploy to Oracle Cloudを実行する担当者はOCI IAMの通常グループへ所属させる。Dynamic Groupには含めない。必要なユーザーPolicyは [Public版 IAM要件](./public-iam-requirements.md) を参照する。

## 社外ユーザー環境

社外ユーザーが自分のOCIテナンシへJetUseをデプロイする場合は、そのユーザーのJetUse専用コンパートメントを対象とするDynamic Groupを1個作成する。社外ユーザー側のDynamic Groupは相手のテナンシに作られるため、JetUse管理側テナンシのDynamic Group上限は消費しない。

デプロイ担当がテナンシ管理権限を持たない（専用コンパートメントの`manage all-resources`だけを持つ）場合、
テナンシ管理者が用意するのは**このcompact Dynamic Group 1本と、デプロイ担当グループへの
`inspect tenancies in tenancy` の1文だけ**でよい。それで認証・ホスト型エージェントを含めて
フル機能でデプロイできる（2026-07-30 実機検証）。
Dynamic Group向けの`read objectstorage-namespaces in tenancy`は**不要**（実測）。
手順とコピペ用のPolicy文は
[専用コンパートメント管理者向けガイド](./public-deploy-dedicated-compartment.md)を参照する。

## Terraform実装との関係

現在の`infra/orm`と`infra/terraform/modules/iam`は、Runtime / ADB / Semantic Storeを分離するstrict構成である。この文書のcompact構成を自動作成するには、Terraformへ次のような切替を追加する必要がある。

```hcl
dynamic_group_mode = "combined" # 1環境1Dynamic Group
dynamic_group_mode = "strict"   # Runtime / ADB / Semantic Storeを分離
```

社外ユーザーの問題を`jetuse:test`で再現するため、Public配布時の既定値とtest環境の値は同じにする。

## 参考

- [OCI: Writing Matching Rules to Define Dynamic Groups](https://docs.oracle.com/iaas/Content/Identity/dynamicgroups/Writing_Matching_Rules_to_Define_Dynamic_Groups.htm)
- [Public版 IAM要件](./public-iam-requirements.md)
- [IAM設定詳細](./iam.md)
