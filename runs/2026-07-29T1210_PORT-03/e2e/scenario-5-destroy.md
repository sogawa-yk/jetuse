# シナリオ5: destroy

- Resource Manager の destroy ジョブ: **SUCCEEDED**
- destroy 後の残存確認（`jetuse-p03` プレフィックスのリソース）:

| 対象 | 結果 |
|---|---|
| Hosted Application（ACTIVE / CREATING / FAILED / NEEDS_ATTENTION） | **0 件** |
| Autonomous Database `jetusep03` | TERMINATED |
| Container Instance `jetuse-p03-api` | DELETED |

同じコンパートメントに残っている `jetuse` ADB（AVAILABLE）と `jetuse-api` Container Instance（ACTIVE）は
**別スタック（FIX-58 の検証環境）のもの**で、本タスクの destroy 対象外。プレフィックスで分離されている。

なお、エージェント3本はシナリオ4（`enable_hosted_agents=false` での apply）の時点で既に削除されており、
`terraform_data` の destroy-time provisioner（`scripts/delete_agent.sh`）による
「Application 削除 → Deployment カスケード削除」が機能することを、full destroy の前に確認できている。
