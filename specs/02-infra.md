# specs/02 — Phase 1 基盤（INFRA-01: Terraformモジュール群）

状態: ドラフト（2026-06-10作成。実装と並行で更新し、INFRA-01完了時に確定）
仕様参照: specs/00-architecture.md（確定版）/ ADR-0003〜0005

## [INFRA-01] Terraformモジュール群

### 目的

Phase 0で手作業（CLI/raw-request）構築したdev環境一式をTerraformでコード化し、再現可能にする。ADR-0004（API GW→Object Storage静的ホスティング）とADR-0005（Functions+CI併用）の構成を初めて形にする。

### 前提（依存タスク / 人間の事前作業）

- チェックポイント①承認済み（2026-06-10）
- apply は人間承認制（CLAUDE.md）。エージェントは validate / plan まで。apply検証は人間の承認後に実施
- 既存VCN `develop` は**参照のみ・変更禁止**（サブネット追加も行わない）。dev環境は**新規VCNを作成**する（destroy/再applyの冪等性確認を共有VCNに影響させないため。CIDRは `develop`(10.0.0.0/16) と重複しない 10.1.0.0/16 を既定）

### モジュール構成

```
infra/terraform/
  modules/
    network/            # 新規VCN + サブネット2面（public: API GW / private: CI・Functions）+ IGW/NAT/SGW + NSG
    object-storage/     # バケット3面: 静的サイト(spa) / アプリデータ(app-data) / 音声入出力(speech)
    adb/                # ADB Serverless（ECPU、ウォレットレス=TLS接続は将来課題、当面ウォレット）
    ocir/               # コンテナリポジトリ（api用）。authトークンは管理外（人間がホームリージョンで作成）
    container-instance/ # FastAPI（SSE系）。privateサブネット + NSG
    functions/          # Functions Application（非ストリーミングAPI群の置き場。個々のfnはAPP-01以降）
    api-gateway/        # GW本体 + デプロイメント（ルート3系統、readTimeout=300）
    iam/                # 動的グループ・ポリシー（コンパートメントレベルのみ。applyは人間承認の明示対象）
  environments/
    dev/                # 全体合成。環境値は terraform.tfvars（gitignore）、雛形 terraform.tfvars.example
```

### 各モジュールの要点（Phase 0実証値を仕様化）

| モジュール | 要点 |
|---|---|
| network | 新規VCN `{prefix}-vcn`（10.1.0.0/16既定）+ IGW・NATGW・Service GW。サブネット: `{prefix}-public`（API GW用・regional）/ `{prefix}-private`（CI/Functions用、NAT経由egress）。NSG: apigw=443 ingress from 0.0.0.0/0、app=8000 ingress from VCN CIDR（SPIKE-02のjetuse-spike-nsgと同構成） |
| object-storage | spa: バージョニング無効・非公開（アクセス方式はINFRA-01内で実測比較 → ADR-0004の検証事項）。app-data / speech: 非公開 |
| adb | ECPU 2・auto scaling無効・ライセンス込み最小（スパイクADBと同条件）。ADMINパスワードは変数（tfvars、コミット禁止） |
| ocir | リポジトリ事前作成必須（無いとpush 403 — Phase 0実証）。`{prefix}-api` を作成 |
| container-instance | shape Flex 1 OCPU/8GB、privateサブネット、NSG適用、イメージはOCIR参照（タグ変数） |
| functions | Application のみ（privateサブネット）。個々のfunctionはアプリ側タスク（APP-01）でデプロイ |
| api-gateway | publicサブネット。デプロイメントルート: ①`/api/chat/{p*}` → CI（`readTimeoutInSeconds=300` 明示 — ADR-0003）②`/api/{p*}` → Functions（fn OCIDのmap変数。空ならルート生成しない）③`/{object*}` → Object Storage静的配信（HTTPバックエンド。実測でパスマッピング方式を確定 — ADR-0004） |
| iam | 動的グループ1つ（SemanticStore/CI/Functionsを `Any{}` で統合 — テナンシのIAM数制限対応）+ ポリシー1本6文（GenAI use / DBTools use / DB read / ADB read / Secrets read / Objects manage）。**テナンシレベルが必要なものは docs/setup/ の手順書へ**（エージェント権限外） |

### 命名・変数規約

- すべてのリソース名は `var.prefix` で始める。**エージェントが自分でapply検証する場合は `jetuse-spike-tf` を使う**（CLAUDE.mDの削除可能プレフィックス内）。人間承認後のdev環境は `jetuse-dev` を既定とする
- OCID・パスワード等の実値は `terraform.tfvars`（gitignore済み）のみ。`terraform.tfvars.example` を必ず同期維持
- providerは `oracle/oci`、認証は `~/.oci/config` DEFAULT

### 作業内容

1. モジュール8本と `environments/dev` の実装
2. `terraform fmt -check` / `terraform validate` / `terraform plan` をクリーンに通す（plan時のtfvarsは `.env` の実値から生成、コミットしない）
3. 検証レポート `docs/verification/INFRA-01.md`（plan結果の要約、ADR-0004検証事項の残課題を明記）

### 完了条件（実機検証）

- [ ] `terraform validate` / `fmt -check` クリーン
- [ ] `terraform plan` がエラーなく完走し、作成リソース一覧が仕様と一致
- [ ] （人間承認後）apply成功 → API GW経由で静的index.htmlとCIのヘルスチェックが応答 → destroy/再applyの冪等性確認

### 成果物

- `infra/terraform/`（コード）
- `docs/verification/INFRA-01.md`（plan/apply検証レポート）
- ADR-0004の検証事項の結論（静的配信方式）→ ADR-0004へ追記

### 禁止事項

- OCID・パスワード・実エンドポイントのコミット
- 既存リソース（VCN develop / インスタンス dev / バケット jetuse-oci-source-documents）への変更操作
- 人間承認なしのapply（`jetuse-spike-tf` プレフィックスでの限定的なapply検証を除く）
