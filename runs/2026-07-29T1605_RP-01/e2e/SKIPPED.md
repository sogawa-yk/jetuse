# RP-01 E2E で実施しなかった範囲と理由

tasks/RP-01.md の E2E シナリオ 1〜4 は**すべて実施し、すべて PASS**（`scenario-1..4.md`）。
片付けゲートの否定テスト（`guard.md`）と片付け後の再照会（`teardown.md` /
`post-teardown-state.md`）も実施済み。
以下は ADR-0021「検証計画」に挙がっているが今回実施していない範囲。

## 1. 配備済み dev アプリスタックでの `/api/health` の `dbchat=ok`（ADR-0021 検証計画 5）

**やらなかった理由**: 実行には ①API イメージのビルドと OCIR への push ②開発者専用の
`app` スタック（Container Instance + API Gateway + SPA バケット）の `terraform apply`
が要る。本タスクの変更は「DB の中の資格情報」と「その env 注入」であり、コンテナを
1 セット新設しないと確認できない一方、確認したい振る舞い
（`OCI$RESOURCE_PRINCIPAL` で DBMS_CLOUD / DBMS_CLOUD_AI が通る）は
シナリオ 2・3 で**同じ ADB・同じ資格情報**に対して直接検証済み。
リソースを増やさない方針（loop-config `e2e.dev_env`）とも合わない。

**代わりに確認したこと**:
- `infra/terraform/environments/app` の env 注入は `terraform init -backend=false` →
  `terraform fmt -check` → `terraform validate` が成功（`terraform-validate.log`）。
- 注入値が効く先である `settings.select_ai_credential` の既定を
  `OCI$RESOURCE_PRINCIPAL` に合わせたため、注入が漏れても API キー版へ落ちない。

**残る人間ゲート**: 実際の `terraform apply` は承認が必要（CLAUDE.md）。
配備後の `/api/health` の `dbchat` 確認は、次に dev スタックを更新する人が
そのついでに見れば足りる（新規リスクは無い＝bootstrap.py は従来から
`ENABLE_RESOURCE_PRINCIPAL` を実行しており、本タスクで変えていない）。

## 2. 既存 dev スキーマ（`JETUSE_APP` 等）への適用

**やらなかった理由**: 共有 ADB の既存スキーマを本タスクの検証のために書き換えない
（tasks/RP-01.md 非ゴール「実 ADB の既存スキーマ・共有リソースを壊さない」）。
なお `JETUSE_APP` には既に `OCI$RESOURCE_PRINCIPAL` の EXECUTE が付いている。根拠は
`post-teardown-state.md` の read-only 照会
（`DBA_TAB_PRIVS` で `table_name='OCI$RESOURCE_PRINCIPAL' AND privilege='EXECUTE'` を引いた結果が
`['JETUSE_APP']`）。`bootstrap.py` が起動時に `ENABLE_RESOURCE_PRINCIPAL` を実行しているため。
よって本変更後の `ops/setup-select-ai.py --schema JETUSE_APP` は追加の付与を行うだけで済む。

**残る人間ゲート**: 既存スキーマに残る `JETUSE_OCI_CRED` の削除は ADR-0021 §未解決 2 の
結論どおり**自動では行わない**。消したい場合の手順は `docs/verification/RP-01.md` に記載。
