# ADR-0014: Public 配布の IAM 分離と Public / Internal リリースライン

- Status: Accepted
- Date: 2026-07-01

## Context

Public 版は GitHub の Deploy to Oracle Cloud ボタンから OCI Resource Manager で配布する。利用者によってはテナンシ IAM 権限を持たず、部門管理者へ必要権限を依頼する必要がある。

同時に、`dev` では `main` から派生した Internal 向けの新機能を開発している。Public 版と Internal 版はいずれも正式リリースであり、Internal 版に Public 機能が含まれても問題ない。

従来の `infra/orm` はアプリリソースと Dynamic Group / Policy を同時に作るため、通常利用者にもテナンシレベルの IAM 権限を要求した。また `main` と `dev` の変更方向が明文化されておらず、後の merge conflict と意図しない Public 公開のリスクがあった。

## Decision

1. `main` を Public 正式版かつ Deploy to Oracle Cloud の配布元とする。
2. `dev` を Internal 次期版の統合・正式リリース元とする。
3. Public 変更は `main` で完成させ、`main → dev` の forward merge で同期する。
4. Internal 固有変更は `dev` に入れ、Public 化するときだけ対象変更を最新 `main` 上へ選択的に移植する。`dev → main` の全体 merge は行わない。
5. IAM は管理者向け `infra/orm-bootstrap` と通常利用者向け `infra/orm` に分離する。
6. Bootstrap は runtime / ADB / Semantic Store の Dynamic Group を分け、通常デプロイ担当グループには JetUse 専用コンパートメント内の権限だけを付与する。
7. Resource Manager がホームリージョンを自動入力しないため、両スタックで `home_region` を明示入力する。

## Consequences

- 通常のデプロイ担当者はテナンシ管理者でなくても JetUse を Apply できる。
- 管理者作業は対象コンパートメントごとの初回 Bootstrap に限定される。
- JetUseDeployers は専用コンパートメント内のリソースと ORM state を管理できるため、グループ所属とコンパートメント境界の管理が必要。
- Public 変更の後に sync PR が1本増えるが、Conflict を変更直後に解消できる。
- Internal 固有機能を Public 化する際は選択的な移植と Public 向け受け入れ確認が必要。
- Deployボタン用の専用ZIPを`main`から生成するため、`main`は常にデプロイ可能でなければならない。

## Alternatives considered

### アプリ stack が IAM も作る

操作は1回だが、全利用者にテナンシ IAM 権限が必要になるため不採用。

### runtime principal を1つの Dynamic Group に統合

Dynamic Group 数は減るが、Container Instance、ADB、Semantic Store が互いの権限の和集合を持つため不採用。SQL Search を使わない場合だけ Semantic Store group を無効にできる。

### `dev` を `main` へ定期 merge

Internal 固有機能まで Public 配布へ入るため不採用。

### Public と Internal を完全に別リポジトリ化

同期コストと二重実装が増え、Internal コードの公開自体は問題ないため不採用。
