# SPIKE-G2 検証レポート: SAMLフェデレーション検証経路の実現可能性(GAP-02)

- 日付: 2026-06-13 / ブランチ: `task/gap-02` / 計画: docs/plan-gap-b.md
- 判断軸（ユーザー指示）: **移行/検証がOCIマネージドサービスで完結できるか**

## 調査結果

jetuse-dev-domain のSAMLメタデータ(`/fed/v1/metadata`、認証不要で200)を実機確認:

| 記述子 | 有無 | 意味 |
|---|---|---|
| `SPSSODescriptor` + `AssertionConsumerService` | あり | **SP**として振る舞える（既知） |
| `IDPSSODescriptor` + `SingleSignOnService` ×2 | あり | **IdP**としても振る舞える（新確認） |

→ **OCI Identity Domainは1つでSAML IdP/SP両対応**。よって:

> **2つ目のIdentity Domain（例: jetuse-idp-test）をIdP役にし、jetuse-dev-domainをSPとして
> SAMLフェデレーションさせれば、外部IdP（Entra/Google）を用意せずOCIマネージドだけで
> 手順書(docs/setup/saml-federation.md)のE2E検証が完結する。**

## ゲート判定: **go（マネージド完結の検証経路あり）**

- 判断軸「マネージドで完結できるか」= **Yes**。外部依存なしでSAML federationを実機検証可能
- 本番の実利用（顧客の Entra/Google 連携）も同じSP設定で、IdPメタデータを差し替えるだけ

## 実行にあたっての前提（人間承認が必要な操作）

CLAUDE.md上、以下は人間承認が必要なため、ゲート承認後に実施:
- **Identity Domain作成**（2つ目の free ドメイン。テナンシのドメイン数上限・ホームリージョン(IAD)作成制約に注意 — INFRA-02の学び）
- **Identity Domain設定変更**（SAML IdP登録・フェデレーション設定）

## 実行プラン（承認後）

1. 2つ目のIdentity Domain `jetuse-idp-test`（free）をホームリージョンに作成、テストユーザー1名作成
2. jetuse-dev-domain に SAML IdP として `jetuse-idp-test` を登録（IdPメタデータ取り込み、NameID=メール、JIT+jetuse-users割当）
3. IdPポリシーに追加（ローカルは残す=切り戻し用）
4. E2E: 手順書のチェックリスト消化（未割当拒否 / JIT作成 / 2回目同一sub / 切り戻し）
5. 検証後、テストドメインは残置 or 削除を人間判断（課金は free のため最小）
