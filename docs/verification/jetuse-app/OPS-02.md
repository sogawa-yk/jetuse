# OPS-02 検証レポート: 可観測性（OCIマネージドサービスへの書き込み）

- 日付: 2026-06-13 / ブランチ: `task/ops-02` / 仕様: specs/15-hardening.md
- 方針: モニタリング/ロギングは**OCIのマネージドサービスに書き込む**（ユーザー指示 2026-06-13）

## 構成

| 対象 | 行き先 | 実装 |
|---|---|---|
| アプリのJSON Linesログ | **OCI Logging カスタムログ** `jetuse-dev-app`（PutLogs直接ingestion、非同期バッチ50件/5秒） | `jetuse_core/obs.py` `OciLogHandler`、`logging.configure`で接続 |
| 呼出数・トークン数 | **OCI Monitoring** カスタム名前空間 `jetuse_dev`（次元: feature/model/status） | `obs.post_metric`、`audit.log_event`から発火 |
| API Gatewayの access/execution | **OCI Logging サービスログ** | Terraform `observability`モジュール |
| Functions invoke（stdout/stderr含む） | **OCI Logging サービスログ** | 同上 |

- すべて**ベストエフォート・非同期**（送信失敗・遅延がリクエストをブロックしない）
- `LOG_OCID` 未設定（ローカル開発）ではstdoutのJSON Linesのみ。CI/FnにはTerraformが自動注入

## Terraform（observabilityモジュール新設）

- `oci_logging_log_group` `jetuse-dev-logs` + カスタムログ`jetuse-dev-app` + サービスログ3本
  （apigw-access / apigw-execution / fn-invoke）、保持30日
- CI/Fnの環境変数に `LOG_OCID = module.observability.app_log_id` を注入

## 実機検証（2026-06-13、完了）

- **GW/Fnサービスログ: ✅**（logging-searchでアクセスログ等を確認）
- **アプリのカスタムログ: ✅**（CIのリソースプリンシパルから `jetuse-dev-app` カスタムログへ。
  source=localhost で jetuse.chat/jetuse.service 等のJSON Linesを20件確認）
- **カスタムメトリクス: ✅**（Monitoring `jetuse_dev` 名前空間に `calls{feature=chat,model=llama-3.3-70b,status=ok}=5` 等。datapoint確認）

### 切り分けの経緯（2つの原因を順に解消）
1. **IAM不足**（`use log-content` / `use metrics`）→ 人間が追加（iam.md「OPS-02」節）
2. **イメージ未更新（真因）**: OPS-02のTerraform applyはLOG_OCID注入でCIを再作成したが、
   **イメージタグは既存(obs.py未収録の0.26.0)を再利用**していた。`oci container-instances container
   retrieve-logs` でCIのstderrを取得し「`oci logging attached` が無い=obs.py不在」を確認 →
   obs.py入りの **0.27.0 / fn 0.1.3** をビルド・デプロイして解消

## 残（IAM適用後の確認項目）

- カスタムログ `jetuse-dev-app` にJSON Linesが届くこと
- Monitoring `jetuse_dev` 名前空間に `calls` / `tokens`（feature/model/status次元）が出ること
- OPS-01の管理ダッシュボードは監査ログ（ADB）が一次ソース。Monitoringはアラーム/長期傾向用に併用
