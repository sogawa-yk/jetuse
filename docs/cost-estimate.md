# JetUse 概算費用（OCI公式価格ベース）

「Deploy to OCI」で構築されるJetUse一式の月額概算。価格は**Oracle公式価格表（2026-07-01更新版、PAY_AS_YOU_GO）**から取得した。OCIの従量価格は全リージョン共通（ap-osaka-1も同額）。円建てはOracle公式のJPY建て価格（為替連動ではなくOracle設定値）。月間稼働は730時間で計算。税別。

- 正本: [OCI Price List](https://www.oracle.com/cloud/price-list/)（本書の単価はOracle Cloud Price List APIの取得値）
- 試算ツール: [OCI Cost Estimator](https://www.oracle.com/cloud/costestimate.html)

## 結論（サマリ）

| 構成 | 月額（USD） | 月額（JPY） |
|---|---:|---:|
| 既定構成の常時稼働分 | **約 $517** | **約 ¥80,200** |
| うちADB（2 ECPU） | $490.56（約95%） | ¥76,037 |
| + AI利用の従量分（小規模利用例） | + $10前後 | + ¥1,500前後 |
| + OpenSearch（`enable_opensearch=true`時のみ） | + 約$162 | + 約¥25,100 |

**コストの支配項はAutonomous Databaseの常時稼働（全体の約95%）**。生成AI・音声・OCR等の従量分は小規模利用ではごく小さい。

## 1. 常時稼働リソース（固定費）

| リソース | 構成（Terraform既定） | 単価 | 月額（USD） | 月額（JPY） |
|---|---|---|---:|---:|
| Autonomous Database（ATP Serverless） | 2 ECPU・自動スケールなし・License Included | $0.336 / ECPU時（¥52.08） | $490.56 | ¥76,037 |
| ADBストレージ | 20 GB | $0.1953 / GB月（¥30.27） | $3.91 | ¥605 |
| Container Instance（APIサーバー） | CI.Standard.E4.Flex 1 OCPU / 4 GB | OCPU $0.025/時 + メモリ $0.0015/GB時 | $22.63 | ¥3,508 |
| **小計** | | | **$517.10** | **¥80,150** |

Container Instanceは[Compute同一単価で課金され追加料金なし](https://www.oracle.com/cloud/cloud-native/container-instances/pricing/)（秒課金・最低1分）。

## 2. 常設だが実質ゼロ〜微小のリソース

| リソース | 課金 | 備考 |
|---|---|---|
| VCN / Subnet / Internet・NAT・Service Gateway | 無料 | アウトバウンド転送のみ課金対象（月10TBまで無料、超過 $0.025/GB） |
| Identity Domain（OIDC認証） | 無料 | `license_type = "free"` で作成 |
| API Gateway | $3.00 / 100万コール月 | 月100万コール未満なら$3以下 |
| OCI Functions（ルーター3パス） | $0.20/100万呼出 + $0.1417/1万GB秒 | [月200万呼出+40万GB秒の無料枠](https://www.oracle.com/cloud/cloud-native/functions/)内に収まる想定 |
| Object Storage（SPA・wallet・RAG/音声ファイル） | $0.0255 / GB月 | 数GB規模なら$0.1前後 |
| Logging | $0.05 / GB月 | 無料枠（10GB/月）内に収まる想定 |
| Monitoring | $0.0025 / 100万データポイント | 無料枠内に収まる想定 |

無料枠の最新条件は [Oracle Cloud Free Tier](https://www.oracle.com/cloud/free/) を参照。

## 3. 従量課金（使った分だけ）

### 生成AI（オンデマンド推論）

| モデル | 入力 / 100万トークン | 出力 / 100万トークン |
|---|---:|---:|
| gpt-oss-120b | $0.15（¥23.25） | $0.60（¥93） |
| gpt-oss-20b | $0.07（¥10.85） | $0.30（¥46.5） |
| Gemini 2.5 Flash | $0.30（¥46.5） | $2.50（¥387.5） |
| command-a系（Large Cohere） | $0.0156 / 1万トランザクション（¥2.42）※ | 同左 |

※ Cohere系はトークンでなくトランザクション課金。定義は[公式価格表](https://www.oracle.com/cloud/price-list/)を参照。

### RAG（File Search / Vector Store）

| 項目 | 単価 | 月額換算 |
|---|---|---:|
| File Search / Vector Store ストレージ | $0.0042 / GB時（¥0.651） | 約$3.07 / GB月（¥475） |
| Vector Store 検索 | $0.20 / 1,000リクエスト（¥31） | — |

### 音声・文書・翻訳

| サービス | 単価 |
|---|---|
| Speech 文字起こし（STT） | $0.35 / 文字起こし時間（¥54.25） |
| Document Understanding OCR | $1.00 / 1,000ページ（¥155） |
| Language 翻訳 | $10.00 / 1,000レコード（¥1,550） |
| Language 事前学習済み推論 | $0.25 / 1,000トランザクション（¥38.75） |

※ TTSはPhoenixリージョン限定のため大阪構成では実質未使用。

### 小規模利用例（月間）

1日100チャット（平均入力2,000 / 出力500トークン、gpt-oss-120b）+ RAG 1GB + 検索3,000回 + 文字起こし5時間 + OCR 1,000ページ:

| 項目 | 計算 | 月額 |
|---|---|---:|
| チャット | 入力6M×$0.15 + 出力1.5M×$0.60 | $1.80 |
| RAGストレージ+検索 | $3.07 + 3×$0.20 | $3.67 |
| STT 5時間 | 5×$0.35 | $1.75 |
| OCR 1,000ページ | 1×$1.00 | $1.00 |
| **合計** | | **約$8（¥1,300前後）** |

## 4. オプション: OpenSearch（`enable_opensearch=true`）

[課金はノードのインフラ実費（compute/メモリ/ストレージ）で、データノード2台まではサービス管理料無料](https://www.oracle.com/cloud/search/pricing/)（3台目以降のデータノードのみ $0.25/ノード時）。

Terraform既定構成（master 1 OCPU/32GB + data 2 OCPU/32GB/50GB + dashboard 1 OCPU/16GB = 計4 OCPU / 80GB）を標準compute単価で概算:

- compute: (4×$0.025 + 80×$0.0015) × 730 ≈ **$160.6/月**
- ブロックストレージ 50GB: 約$1.3/月
- サービス料: データノード1台のため$0

**合計 約$162/月（約¥25,100）**。常設課金のため、RAGのOpenSearchバックエンドを使う場合のみ有効化する（既定OFF）。

## 5. コスト削減の選択肢

1. **ADBの停止運用**: 未使用時にADBを停止するとECPU課金が止まる（ストレージのみ）。夜間・週末停止で固定費を50〜70%削減できる。
2. **ADB Developerインスタンス**: 開発検証用途なら $0.0391/時（約$28.5/月）の[Developer構成](https://www.oracle.com/autonomous-database/)が選択肢（容量・機能制限あり。現行Terraformは未対応、要改修）。
3. **`enable_opensearch=false`（既定）** を維持する。
4. **検証スタックは使い終わったらDestroy**する（VCN上限・ADB課金の両面で有効）。

## 出典

- [OCI Price List（正本）](https://www.oracle.com/cloud/price-list/) — 単価はOracle Cloud Price List API（2026-07-01更新）取得値
- [Container Instances Pricing](https://www.oracle.com/cloud/cloud-native/container-instances/pricing/)
- [Search with OpenSearch Pricing](https://www.oracle.com/cloud/search/pricing/) / [FAQ](https://www.oracle.com/cloud/search/faq/)
- [OCI Functions](https://www.oracle.com/cloud/cloud-native/functions/)（無料枠）
- [Oracle Cloud Free Tier](https://www.oracle.com/cloud/free/)
- [OCI Cost Estimator](https://www.oracle.com/cloud/costestimate.html)
