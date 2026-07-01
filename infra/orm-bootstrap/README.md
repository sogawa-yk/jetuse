# JetUse IAM Bootstrap

JetUse の Public ORM スタックを通常の部門ユーザーが適用できるようにする、管理者向けの一回限りのスタックです。

- 実行者: テナンシ IAM を管理できる管理者
- 作業ディレクトリ: `infra/orm-bootstrap`
- 作成物: runtime / ADB / Semantic Store の Dynamic Group と Policy、任意のデプロイ担当グループ用 Policy
- 次の手順: IAM 反映後、通常利用者が `infra/orm` を Apply

詳細と手動設定用の Policy 一覧は [../../docs/setup/iam.md](../../docs/setup/iam.md) を参照してください。
