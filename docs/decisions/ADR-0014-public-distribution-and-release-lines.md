# ADR-0014: Public配布のIAM選択とPublic / Internalリリースライン

- Status: Accepted
- Date: 2026-07-01
- Updated: 2026-07-02

## Context

Public 版は GitHub の Deploy to Oracle Cloud ボタンから OCI Resource Manager で配布する。利用者によってはテナンシ IAM 権限を持たず、部門管理者へ必要権限を依頼する必要がある。

同時に、`dev` では `main` から派生した Internal 向けの新機能を開発している。Public 版と Internal 版はいずれも正式リリースであり、Internal 版に Public 機能が含まれても問題ない。

IAMとアプリを別Stackにすると最小権限の責務は明確になる一方、Deployボタン、state、prefix、実行順序が増える。Terraformは実行ユーザーのOCI権限を超えて操作できないため、1つのStack内でIAM作成範囲を選べば、権限差を保ったまま操作を単純化できる。また`main`と`dev`の変更方向が未定義だと、merge conflictと意図しないPublic公開のリスクがある。

## Decision

1. `main` を Public 正式版かつ Deploy to Oracle Cloud の配布元とする。
2. `dev` を Internal 次期版の統合・正式リリース元とする。
3. Public 変更は `main` で完成させ、`main → dev` の forward merge で同期する。
4. Internal 固有変更は `dev` に入れ、Public 化するときだけ対象変更を最新 `main` 上へ選択的に移植する。`dev → main` の全体 merge は行わない。
5. IAMとアプリ本体は1つの`infra/orm` Stackで管理する。
6. 実行ユーザーの権限と既存IAMに応じて、Dynamic GroupとRuntime Policyの作成を独立して切り替える。
7. runtime / ADB / Semantic StoreのDynamic Groupは分離する。
8. Resource Managerがホームリージョンを自動入力しないため、`home_region`を明示入力する。

## Consequences

- デプロイ操作は1つのStackと1つのDeployボタンで完結する。
- テナンシ管理者はIAMとアプリを同時に作成できる。
- 権限が限定された利用者は、管理者が事前作成したIAMに対応するフラグを無効にして同じStackを使用できる。
- 権限のないIAM操作を有効にするとPlan / ApplyがOCIの権限エラーになる。
- JetUseDeployers は専用コンパートメント内のリソースと ORM state を管理できるため、グループ所属とコンパートメント境界の管理が必要。
- Public 変更の後に sync PR が1本増えるが、Conflict を変更直後に解消できる。
- Internal 固有機能を Public 化する際は選択的な移植と Public 向け受け入れ確認が必要。
- Deployボタン用の専用ZIPを`main`から生成するため、`main`は常にデプロイ可能でなければならない。

## Alternatives considered

### IAMとアプリを別Stackに分離

最小権限の責務分離にはなるが、Deployボタン、state、prefix、実行順序が増えて利用者体験が複雑になるため不採用。1つのStack内の作成フラグで権限差を扱う。

### runtime principal を1つの Dynamic Group に統合

Dynamic Group 数は減るが、Container Instance、ADB、Semantic Store が互いの権限の和集合を持つため不採用。SQL Search を使わない場合だけ Semantic Store group を無効にできる。

### `dev` を `main` へ定期 merge

Internal 固有機能まで Public 配布へ入るため不採用。

### Public と Internal を完全に別リポジトリ化

同期コストと二重実装が増え、Internal コードの公開自体は問題ないため不採用。
