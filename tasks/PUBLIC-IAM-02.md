# タスク: PUBLIC-IAM-02 専用コンパートメント権限だけでの公開デプロイを成立させる

前提: FIX-58（テナンシ管理者経路の成立・`docs/verification/PUBLIC-DEPLOY-E2E.md`）の次段。
ユーザー指示（2026-07-30）: 「テナンシ管理者権限は無く、専用コンパートメントの to manage 権限だけを
持つ利用者」がデプロイして**完全動作**する状態にし、テナンシ権限が必要な分は案内ページで
「このポリシーが必要です」と提示する。**成果の主眼は必要ポリシーの確定**。

## 目的

`Allow group <deployer> to manage all-resources in compartment id <X>` だけを持つ利用者が、
公開スタック（Deploy to Oracle Cloud ボタン）でデプロイし、FIX-58 と同じ機能水準
（チャット / RAG / DBチャット / OCR / 音声 / TTS / 翻訳 / 管理ダッシュボード / ホスト型エージェント）
に到達できることを**実機で**確認する。到達に必要なテナンシ側ポリシーを、コピペできる形で確定する。

## 決定事項（2026-07-30 ユーザー判断）

1. **事前IAMの提供形態はコピペ用ポリシー文書のみ**。管理者向け第2スタック（zip）や `ops/` スクリプトは作らない。
   → リリースに孤児として残る `jetuse-iam-bootstrap.zip`（2026-07-02・現行 release.yml は生成しない）は復活させない。
2. **ホスト型エージェント（PORT-03）も分割IAM構成のサポート対象に含める**。
   事前作成 DG にホスト型3型、事前作成 policy に `read repos` / `read vss-family` を含めて案内する。
3. DEPLOYTEST テナンシに検証用の制限グループ / ユーザー / 専用コンパートメントを新規作成して検証する（承認済み）。

## 静的棚卸し（コード読解で確定・2026-07-30）

### コンパートメント権限では原理的に不可能（テナンシ管理者の事前作成が必須）

| # | 対象 | 根拠 |
|---|---|---|
| 1 | Dynamic Group 3本 | `modules/iam/main.tf` が `compartment_id = var.tenancy_ocid` で作成（root 固定） |
| 2 | root の `<prefix>-runtime-tenancy-policy`（`read objectstorage-namespaces in tenancy`） | 同ファイル。**`enable_dynamic_group=false` では `count=0` でスタックが作らない**。欠けると実行時に Object Storage 経路（RAG / 議事録 / ADB ウォレット取得）が壊れる |
| 3 | デプロイ担当への tenancy read 3文 | `infra/orm/providers.tf` の `oci_identity_region_subscriptions`（`deploy_region_key` と `home_region` の解決）、`modules/object-storage` と `modules/spa-bucket` の `oci_objectstorage_namespace` |

### 実機で測らないと確定しないもの（本タスクの中心）

| # | 論点 | 影響 |
|---|---|---|
| A | 専用コンパートメントの `manage all-resources` で `oci_identity_domain` を**作成**できるか | 不可なら認証つき配備が成立しない |
| B | 作成したドメインを**管理**できるか（`oci_identity_domains_app` / `_user` / `_grant` / `_setting` と FIX-58 の `UserPasswordChanger` CLI） | 不可なら demo ログイン・`ADMIN_USERS=demo`・エージェント OAuth が全滅 |
| C | `enable_runtime_policy=true`（コンパートメント内 policy 作成）が通るか | 通れば管理者の事前作業が1段減る |
| D | ホスト型エージェント（`oci_generative_ai_hosted_application` / `_deployment`）を作成できるか | 決定2のサポート範囲に直結 |
| E | **destroy** が通るか。ドメイン非アクティブ化（`oci iam domain deactivate`）と `oci identity-domains app patch` は IAM 操作 | 「デプロイできるが消せない」は FIX-58 §9 と同じ欠陥 |
| F | 単一 DG（compact 構成）＋ホスト型3型で実行時権限が足りるか | 分割IAM経路は**一度も実機検証されていない**（`runs/2026-07-28T2226_PUBLIC-DEPLOY-E2E/e2e/SKIPPED.md` §3） |

### ドキュメントの既知の陳腐化（本タスクで直す）

- `docs/setup/public-deploy-dedicated-compartment.md` は FIX-58 / PORT-03 前。手順6が
  「Domain 権限が無ければ `enable_auth=false`」と案内しているが、それだと demo ログイン・
  管理ダッシュボード・ホスト型エージェント（`locals.hosted_agents_enabled` は `enable_auth` 必須）が同時に落ちる。
- `docs/setup/dynamic-group-matching-rules.md` と `public-iam-requirements.md` の matching rule は
  4型（`computecontainerinstance` / `fnfunc` / `autonomousdatabase` / `generativeaisemanticstore`）のみで、
  PORT-03 のホスト型3型が無い。`read repos` / `read vss-family` の記載も無い。
- 事前作成用の runtime policy 文一覧が agentic 6文の抜粋のみ。実際は約20文（正本は `modules/iam/main.tf`）。

## 検証手順（実機・DEPLOYTEST）

配備先 `us-chicago-1`（GenAI 実証済）/ ホーム `ca-toronto-1`。

### Phase A: 制限環境の用意（テナンシ管理者として）

1. 専用コンパートメント `jetuse-restricted` を新規作成。
2. グループ `jetuse-deployers` と検証ユーザーを作成し、API キーで CLI プロファイルを作る
   （ボタン経由のコンソール操作と**認可経路は同一**。差分は記録する）。
3. 上記「原理的に不可能」の1〜3だけを管理者として事前作成する。
   **意図的に runtime policy は作らない**（論点C を測るため）。

### Phase B: 制限ユーザーで apply し、必要ポリシーを確定する

`enable_dynamic_group=false` / `existing_dynamic_group=<事前作成DG>` / `enable_runtime_policy=true` /
`existing_iam_covers_hosted_agents=true` / `enable_auth=true` / `enable_hosted_agents=true`。

失敗を1件ずつ記録し（resource 名・OCI エラーコード・不足権限）、
「テナンシ管理者が足す1文」と「スタック側の修正」のどちらで解くかを都度判定する。

### Phase C: 確定ポリシーだけでゼロからの再現

**新しい**専用コンパートメントに、確定した事前ポリシーのみを作成 → 制限ユーザーで apply →
`ops/e2e/public-deploy.mjs`（38項目）＋ `agents-3sdk.mjs`（9項目）→ **destroy まで制限ユーザーで通す**。

### Phase D: 成果物

- 案内ページの改訂（`docs/setup/public-deploy-dedicated-compartment.md` / `public-iam-requirements.md` /
  `dynamic-group-matching-rules.md`）。**コピペ可能な完全版**（DG matching rule / root policy /
  runtime policy 全文 / デプロイ担当 policy）を載せる。
- 検証レポート `docs/verification/PUBLIC-IAM-02.md`（実機結果・切り分け表・残存リスク）。
- スタック修正が必要な場合はその実装（例: 不足を plan で止める precondition）。方式変更を伴うなら ADR。
- 証跡 `runs/<run-id>/e2e/`（URL / OCID / パスワードはマスク）。

## 完了条件

1. 制限ユーザー（テナンシ権限ゼロ）で apply → E2E → destroy が通る。E2E は FIX-58 の 38項目 +
   エージェント 9項目で、4xx/5xx 0件。
2. 必要なテナンシ側ポリシーが**過不足なく**確定している。「多めに書いておく」は不可
   ＝各文について、無いと何が失敗するかを実機の失敗ログで裏取りする。
3. 案内ページが、管理者へ渡す文をコピペだけで完結できる形になっている（DG matching rule と
   policy 全文。ホスト型エージェント込み）。
4. `make lint && make test` 緑、`terraform validate` / `fmt` 緑（スタック修正を入れた場合）。
5. Codex review PASS。
6. 検証レポートを残し、検証リソースを teardown 済み（残存確認つき）。

## 実施しないこと

- テナンシ管理者向け第2スタック（zip）の復活（決定1）。
- `enable_auth=false` 経路の正式サポート化。認証なしは隔離検証用途のままとする。
- Semantic Store（`enable_semantic_store` / SQL Search）の分割IAM検証。既定 off かつ
  事前作成ストアを指す任意設定のため、案内文の記載のみ対象。
