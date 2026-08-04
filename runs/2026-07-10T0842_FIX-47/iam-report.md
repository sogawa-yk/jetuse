# FIX-47 IAM レポート（最終形 — 2026-07-13 改訂）

施主取り決め: **スタックの apply に IAM を含めない**（enable_dynamic_group=false・
enable_runtime_policy=false）。IAM はすべて人間が console で事前作成し、本レポートが正本。
`<TEST>` = jetuse:test コンパートメント OCID（実値はレポートに記載しない）。

## 経緯（apply-1 の学び）

- 当初案（スタックが DG 3件 + policy 2件を作成）は、①エージェントにテナンシルートの DG 作成権限が
  ない ②大阪 VCN 枠超過、で apply-1 FAILED。大阪の部分リソースは destroy 済み。
- 施主判断で **既存 DG 方式**へ変更: `jetuse-deploy-test-dg`（施主管理）が「デプロイに必要な
  最小の一致セット」を定義する DG であり、E2E もこれで実施。テナンシ DG は数の制約があり
  スタックごとに増やさない。
- リージョンは VCN 枠のため us-chicago-1 (ord) へ変更（タスク文書が事前承認済み。
  修正はリージョン非依存・旧公開イメージの ord 存在確認済み）。

## 人間が作成する IAM（すべて施主作業）

1. **Dynamic Group**: 既存 `jetuse-deploy-test-dg` を使用。matching rule が jetuse:test 配下の
   `computecontainerinstance` / `fnfunc` / `autonomousdatabase` / `generativeaisemanticstore` を
   含むこと（= 公開スタックの「既存 DG モード」利用者に求める前提条件と同一）。
2. **テナンシポリシー**（作成済みと申告あり・2026-07-13）:
   - `Allow dynamic-group jetuse-deploy-test-dg to read objectstorage-namespaces in tenancy`
3. **runtime policy**（jetuse:test 内・名前案 `jetuse-spike-fix47-runtime-policy`・20 statements）:
   モジュールの 3 DG 構成を単一 DG に畳んで distinct した結果（重複3文が消えて 23→20）。
   全文は下記「statements 全文」。**FIX-47 の新規は `manage generative-ai-project` 1 文のみ。**

## statements 全文（主語 = jetuse-deploy-test-dg / 対象 = <TEST>）

1. `Allow dynamic-group jetuse-deploy-test-dg to use generative-ai-family in compartment id <TEST>`
2. `Allow dynamic-group jetuse-deploy-test-dg to manage generative-ai-vector-store in compartment id <TEST>`
3. `Allow dynamic-group jetuse-deploy-test-dg to manage generative-ai-vectorstore-file in compartment id <TEST>`
4. `Allow dynamic-group jetuse-deploy-test-dg to manage generative-ai-file in compartment id <TEST>`
5. `Allow dynamic-group jetuse-deploy-test-dg to manage generative-ai-project in compartment id <TEST>` ← FIX-47 新規
6. `Allow dynamic-group jetuse-deploy-test-dg to use autonomous-database-family in compartment id <TEST>`
7. `Allow dynamic-group jetuse-deploy-test-dg to manage objects in compartment id <TEST>`
8. `Allow dynamic-group jetuse-deploy-test-dg to read buckets in compartment id <TEST>`
9. `Allow dynamic-group jetuse-deploy-test-dg to manage ai-service-speech-family in compartment id <TEST>`
10. `Allow dynamic-group jetuse-deploy-test-dg to use ai-service-document-family in compartment id <TEST>`
11. `Allow dynamic-group jetuse-deploy-test-dg to use ai-service-language-family in compartment id <TEST>`
12. `Allow dynamic-group jetuse-deploy-test-dg to read tag-namespaces in compartment id <TEST>`
13. `Allow dynamic-group jetuse-deploy-test-dg to use log-content in compartment id <TEST>`
14. `Allow dynamic-group jetuse-deploy-test-dg to use metrics in compartment id <TEST>`
15. `Allow dynamic-group jetuse-deploy-test-dg to read secret-family in compartment id <TEST>`
16. `Allow any-user to use functions-family in compartment id <TEST> where ALL {request.principal.type = 'ApiGateway', request.resource.compartment.id = '<TEST>'}`
17. `Allow dynamic-group jetuse-deploy-test-dg to read objects in compartment id <TEST>`
18. `Allow dynamic-group jetuse-deploy-test-dg to use database-tools-family in compartment id <TEST>`
19. `Allow dynamic-group jetuse-deploy-test-dg to read database-family in compartment id <TEST>`
20. `Allow dynamic-group jetuse-deploy-test-dg to read autonomous-database-family in compartment id <TEST>`

## スタックが作成する IAM

なし（plan-5 で確認: `oci_identity_*` の managed resource ゼロ。data source の
availability_domains / region_subscriptions 読取のみ）。

## 後始末

runtime policy・テナンシポリシー文・DG の matching rule 追記分は、キュー完了後の destroy と
同タイミングで人間が削除（スタック destroy では消えない — 人間作成のため）。
