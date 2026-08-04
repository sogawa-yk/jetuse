# destroy（論点E）と後始末

## destroy — 制限ユーザー・**最小ポリシー**で実行

destroy 直前に、デプロイ担当グループのポリシーを**必要性検証で確定した2文だけ**に絞った。

```text
Allow group jetuse-deployers to inspect tenancies in tenancy
Allow group jetuse-deployers to manage all-resources in compartment id <COMPARTMENT_OCID>
```

（`length(data.statements)` = 2 をアサートしてから 300 秒待って実行）

結果:

```text
TERMINAL: SUCCEEDED
Destroy complete! Resources: 181 destroyed.
```

FIX-58 で作り込んだ destroy 経路（バケットの中身の一括削除、OIDC アプリの非アクティブ化、
Identity Domain の deactivate）が、**テナンシ権限を持たない利用者でも最後まで通る**ことを確認した。

## ホスト型エージェントの残存確認

OCI の検索サービス（`search resource structured-search`）は destroy 直後も
`GenerativeAiHostedDeployment` を 3 件 `ACTIVE` として返した。**これは索引の遅延**で、
API に直接問い合わせると実体は削除済みだった。

```text
list_hosted_applications: jetuse3-agent-adk / -langgraph / -openai  → いずれも DELETED
list_hosted_deployments : 3 件                                      → いずれも DELETED
```

残存確認に検索サービスだけを使うと「消えていないように見える」ので、
ホスト型リソースは GenerativeAI API 側で確認する必要がある。

## 後始末（テナンシ管理者として）

| 対象 | 結果 |
|---|---|
| ORM スタック 2本（本体 / precondition 検証用） | 削除 |
| Policy 3本（deployer / DG namespace / namespace 検証用） | 削除 |
| Dynamic Group `jetuse-restricted-dg` | 削除 |
| ユーザー 2（`jetuse-deployer` / `jetuse-nsprobe`）と API キー | 削除 |
| グループ 2（`jetuse-deployers` / `jetuse-nsprobe-grp`） | 削除 |
| コンパートメント `jetuse-restricted` | 削除（`DELETING`。非同期） |

残存確認: テナンシに `jetuse` を含む Dynamic Group・ユーザーは **0 件**。
既存リソース（他コンパートメント・`jetuse-test` 等）には一切触れていない。
