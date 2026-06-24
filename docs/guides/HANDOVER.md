# 引き継ぎ資料 — OCI版JetUse プロトタイプ（Phase 0完了時点）

作成日: 2026-06-10
状態: **Phase 0（全スパイク）完了。人間チェックポイント①の承認待ちで停止中。**
次セッションはまずこのファイルと `CLAUDE.md` → `specs/00-architecture.md` を読むこと。

---

## 1. プロジェクト概要

AWSの JetUse（generative-ai-use-cases）相当の生成AIユースケース集をOCI上に構築する。
正本の作業計画書は `docs/plan.md`（Phase 0〜9、約17〜19週分）。開発ルールは `CLAUDE.md`（spec-driven / 1タスク=1ブランチ / 実機検証主義 / `jetuse-spike-*` プレフィックス）。

ユーザー決定事項（前セッションで確定）:
- スコープ第1区切り = Phase 0 全体（完了済み）
- Git = **ローカルのみ**（リモート未設定。PR運用はリモート接続後から）
- ADB = ECPU最小の課金構成で作成済み
- プロビジョニングは与えられた権限内で自由（ただしテナンシレベルIAMは権限なし＝人間作業）

## 2. 実行環境

| 項目 | 値 |
|---|---|
| 作業ディレクトリ | `/home/opc/jetuse`（git: mainブランチ、17コミット） |
| 実行マシン | OCIインスタンス `dev`（VM.Standard.E6.Flex / Oracle Linux 9.7 / **ap-osaka-1** / sudo可） |
| コンパートメント | `jetuse-proto`（OCIDは `.env` の `COMPARTMENT_OCID`。計画書の `jetuse-spike` は存在せず代替 — ADR-0001） |
| OCI認証 | `~/.oci/config` DEFAULT（ユーザー: yuki.sogawa.2@oracle.com）。**鍵ファイル末尾に `OCI_API_KEY` マーカー行あり（流用時は除去必須）** |
| 導入済みツール | Python 3.12（venv: `.venv`）/ Node 22 / Terraform 1.15 / podman 5.6 / OCI CLI 3.85 / mpg123 |
| Python venv | `openai, oci, oci-genai-auth, oracledb, fastapi, httpx, gTTS, websockets, oci-ai-speech-realtime, reportlab, python-docx` 等導入済み |
| 秘密情報 | すべて `.env`（gitignore済み）。ADBウォレット: `/home/opc/adb_wallet/` |

`.env` の主なキー: `COMPARTMENT_OCID` / `PROJECT_OCID`（GenerativeAiProject）/ `ADB_OCID` / `ADB_ADMIN_PASSWORD` / `ADB_QUERY_PASSWORD` / `SEMSTORE_OCID` / `DBTOOLS_ENRICH_OCID` / `DBTOOLS_QUERY_OCID` / `VAULT_OCID` / `SECRET_*_OCID` / `CI_OCID` / `CI_PRIVATE_IP` / `NSG_OCID` / `APIGW_OCID` / `APIGW_ENDPOINT` / `OCIR_TOKEN`

## 3. 完了済み作業（Phase 0）

各スパイクの詳細・実行ログ・採点表は `docs/verification/SPIKE-0X.md` 参照。検証スクリプトは `spikes/`（共通クライアントは `spikes/common.py`）。

| # | 結果要約 |
|---|---|
| SPIKE-01 | OpenAI互換API接続OK（IAM署名）。**API対応はモデル依存**: Responses=gpt-oss/llama、Chat Completions=Gemini、Cohere=ネイティブのみ。TTFT: llama 0.07s / gpt-oss 0.8s / gemini-pro 10s超 |
| SPIKE-02 | **API GW経由SSE成立**（バッファなし、330秒連続OK、readTimeoutは読取間隔）→ ADR-0003で経路確定 |
| SPIKE-03 | Vector Store/File Search採用可。日本語直接検索10/10、指示強制でRAG正答10/10・引用9/10。**docx非対応**。CP/DP 2ホスト構成と`OpenAi-Project`ヘッダ必須を解明 |
| SPIKE-04 | SemanticStore大阪で作成OK。**enrichmentはテナンシIAM未整備でFAILED（人間待ち）**。Select AI比較済み: command-a 9/10, llama 8/10（`spikes/data/spike04_results_*.json`） |
| SPIKE-05 | Conversations/Projects検証OK。一覧APIなし→「履歴の正はADB」（ADR-0002） |
| SPIKE-06 | バッチ/リアルタイムSTT日本語OK（Whisper、話者分離あり、partialなし）。**Phoenix TTSに日本語5ボイス発見**（1.3s/文）→音声チャット成立可能 |
| SPIKE-07 | Redwoodふうギャラリー完成（`packages/web/`、build/lintクリーン）。branding.json差替実証。JetUse流用部品リスト確定（レポート内の表参照） |

ADR: `docs/decisions/ADR-0001`（環境・モデル方針）/ `ADR-0002`（会話状態）/ `ADR-0003`（SSE経路）
チェックポイント①材料: `specs/00-architecture.md`（確定案）+ `docs/comparison/aws-reference.md`（比較表初版）

## 4. 作成済みOCIリソース（すべて jetuse-spike- プレフィックス、残置中）

| リソース | 名前 | 備考 |
|---|---|---|
| ADB (ADW, ECPU 2) | jetuse-spike-adb | **課金中**。SH/SSBサンプルあり。DBユーザー `JETUSE_QUERY`（読取専用）作成済み。Select AIプロファイル `JETUSE_SPIKE_AI` あり |
| GenerativeAiProject | jetuse-spike-project / jetuse-spike-project2 | DP APIの `OpenAi-Project` ヘッダ用 |
| Vector Store | jetuse-spike-vs（vs_kix_3xwx...） | 規程3文書取り込み済み（travel-policy.pdf等） |
| SemanticStore | jetuse-spike-semstore | enrichment未完（IAM待ち） |
| Vault/Key/Secret | jetuse-spike-vault / -key / -adb-*-pw | DBTools接続用 |
| DBTools接続 | jetuse-spike-dbconn-enrich / -query | validate OK |
| Container Instance | jetuse-spike-ci（10.0.1.129:8000） | SSEテスト用FastAPI稼働中 |
| API Gateway | jetuse-spike-apigw + jetuse-spike-sse-dep | エンドポイントは `.env` の `APIGW_ENDPOINT` |
| NSG | jetuse-spike-nsg | 8000/tcp from VCN、443/tcp from any |
| OCIR | kix.ocir.io/<ns>/jetuse-spike-sse:v1 | auth token作成済み（`.env`） |
| バケット | jetuse-spike-speech | STT入出力 |
| 既存（触らない） | VCN `develop` / インスタンス `dev` / バケット `jetuse-oci-source-documents` | 参照のみ・変更禁止 |

UIプレビュー: `packages/web` で `npm run preview -- --port 4173` をバックグラウンド起動済み（再起動後は要再実行）。

## 5. 人間待ち事項（チェックポイント①）— 再開のトリガー

> **2026-06-10 追記**: アーキについて一次フィードバック受領・反映済み（①フロントは静的ホスティング=API GW→Object Storage → ADR-0004、②非ストリーミングAPIはOCI Functions・SSE系のみCI → ADR-0005、③「OCI Generative AI」表記は「OCI Enterprise AI」に統一）。残る承認事項は下記1〜4（1はADR-0004/0005を含む再承認）。

1. **アーキ承認**: `specs/00-architecture.md` の決定11項目 + ADR-0001〜0005
2. **UIルック承認**: `ssh -L 4173:localhost:4173 opc@<このインスタンス>` → http://localhost:4173
3. **SQL Search用テナンシIAM作業**: `docs/setup/iam.md`（統合版: 動的グループ1 + ポリシー1本）
4. **スパイクリソースの残置/削除判断**（特にADBの課金）

## 6. 再開時の次アクション

### 「IAM整備完了」と言われたら（SPIKE-04の完結）
```
BASE=https://inference.generativeai.ap-osaka-1.oci.oraclecloud.com/20260325
SS=$(grep SEMSTORE_OCID .env | cut -d= -f2)
# 1) enrichment再実行（FULL_BUILD, SH）→ SUCCEEDED までポーリング
oci raw-request --target-uri "$BASE/semanticStores/$SS/actions/enrich" --http-method POST \
  --request-body '{"enrichmentJobType":"FULL_BUILD","enrichmentJobConfiguration":{"enrichmentJobType":"FULL_BUILD","schemaName":"SH"}}'
# 2) generateSqlFromNl で spike04_select_ai.py と同じ日本語10問を評価
#    body: {"inputNaturalLanguageQuery": "..."} / SQLは jobOutput.content
# 3) docs/verification/SPIKE-04.md の評価表を更新（Select AIとの比較を完成）
```

### チェックポイント①承認後（Phase 1着手、`docs/plan.md` §3）
- INFRA-01: Terraformモジュール群（SPIKE-02のAPI GW仕様JSON・NSG構成・CI構成を雛形に流用）
- INFRA-02: IAM Identity Domain OIDC（PKCE）
- APP-01: FastAPIスケルトン（`spikes/common.py` のクライアント生成と SPIKE-01の2系統サポート設計を昇格）
- APP-02: React SPAスケルトン（`packages/web/` のトークン・ギャラリー部品を昇格。JetUse流用リストはSPIKE-07レポート参照）
- CI-01: GitHub Actions（**リモート未設定のため、先にユーザーへGitHubリポジトリの有無を確認**）

## 7. ハマりどころ（実機確定の未文書仕様 — 詳細は各SPIKEレポート）

- OpenAI互換は**2ホスト構成**: 推論=`inference.generativeai...:/openai/v1`、Vector Store本体CRUD=`generativeai...:/20231130/openai/v1`
- Files/Conversations等は `OpenAi-Project` ヘッダ（GenerativeAiProject OCID）+ `CompartmentId` ヘッダ必須。`GET /models` は無い（管理APIで列挙）
- Vector Store: CP `completed` 後もDP可視化まで10〜30秒待つ。docxはunsupported_file
- SQL SearchはAPIバージョン **`/20260325`**（CLIにデータプレーンコマンドなし、raw-requestで叩く）
- Speech realtime WHISPER: `modelType=WHISPER`。`shouldIgnoreInvalidCustomizations`/`finalSilenceThresholdInMs` を送ると400。partialなし
- TTS: Phoenix限定。`modelDetails.languageCode: "ja-JP"` を付けないと日本語ボイスが弾かれる
- OCIR: 大阪は `kix.ocir.io`、ユーザー名 `{namespace}/{user}`、auth tokenは**ホームリージョン(us-ashburn-1)で作成**、リポジトリ事前作成必須（無いとpush 403）
- DBMS_CLOUD.CREATE_CREDENTIAL に渡す秘密鍵は `OCI_API_KEY` マーカー行を除去（ORA-20401の原因）
- API GWデプロイは `readTimeoutInSeconds: 300` 明示必須（デフォルト10秒）。SSEにはkeepaliveコメント送出を実装すること
- ADBのSHサンプルスキーマはPUBLIC読取可（個別GRANTはORA-01031になるが不要）

## 8. 約束ごと（忘れやすい点）

- 検証用リソースは `jetuse-spike-*` 必須。既存リソース（VCN develop等）は変更禁止
- OCID・パスワード・エンドポイント実値をコミットしない（`.env` のみ。レポートではマスク）
- 実機検証主義: ドキュメント引用だけで完了にしない。結果は `docs/verification/` へ
- Terraform apply（本番相当）・IAM変更・Identity Domain変更は人間承認制
- コミット前に lint / build を通す（フロントは `npm run build` 成功まで）
