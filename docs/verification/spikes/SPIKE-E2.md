# SPIKE-E2: OCI Search with OpenSearch によるRAG — 可用性・統合方式・常設コスト

ENH-05のゲート調査。**結論: GO（ユーザー承認のうえ実装・実機E2E成功 2026-06-17）**。
大阪可用・統合方式は明確。**常設クラスタのコスト**が論点だったが、ユーザー承認を得て
最小構成クラスタをプロビジョニングし、アプリの第3のRAGバックエンドとして実装した。

## 実装結果（2026-06-17）
- 最小構成クラスタ `jetuse-dev-opensearch`（master1/data1/dashboard1・2.19.1）をTerraform
  (`modules/opensearch`, `enable_opensearch=true`)でプロビジョニング。private 10.1.1.202。
- アプリ: `rag_opensearch.py`（チャンク→`embed_text`→k-NN index→検索→llama-3.3-70bで回答）。
  `/rag` セレクタに「OpenSearch（k-NN）」を追加。アップロードは3バックエンドへ同時取り込み。
- **E2E成功（ゲートウェイ経由・実トークン）**: 日本語文書をアップロード→OpenSearchバックエンドで
  質問→正答（手当7800円・キーワードZephyrProtocol-2026）+ citation（score 0.867）。
- **ハマり所**: `security_mode=DISABLED` でも **9200はTLS**。平文HTTPだと
  「Server disconnected without sending a response」。**https + verify=False**(私設subnet・
  証明書CN不一致)で解決。埋め込みはOpenAI互換 `/embeddings` 非対応(400)のため
  ネイティブ `embed_text`(cohere.embed-multilingual-v3.0/1024次元)を使用。

## 1. 可用性（ap-osaka-1, 2026-06-17 実機）

- OCI Search with OpenSearch は **ap-osaka-1 で利用可能**。CLI/SDKとも存在
  （`oci opensearch cluster {list,create,...}` / `oci.opensearch.OpensearchClusterClient`）。
- `oci opensearch cluster list`（compartment=jetuse-proto）が**成功**（クラスタ0件）。
  サービス有効・IAMで参照可を確認。
- クラスタ未作成（高コスト・常設リソースのため人間承認前提。CLAUDE.md準拠で作成は保留）。

## 2. アーキテクチャ＝常設マルチノードクラスタ（コストの核心）

`cluster create` のパラメータが示す通り、**常時稼働の複数ノード構成**:
- master node（OCPU/メモリ）× N
- data node（OCPU/メモリ/**ストレージ**）× N
- opendashboard node（OCPU/メモリ）× N
- （任意）専用 search node × N

→ **使用有無に関わらずノードのOCPU/メモリ/ストレージが継続課金**される。
これが File Search（実質サーバレス）/ Select AI RAG（稼働中ADBを再利用）との決定的な差。

### コスト目安（OCI Compute Eシェイプ単価ベースの概算）
- 最小構成（例: master1+data1+dashboard1、各1〜2 OCPU、計~4 OCPU + メモリ + ブロックストレージ）
  → **概ね $100〜150/月の常設**（OCPU ~$0.025/OCPU時 × 4 × 730h ≈ $73 + メモリ/ストレージ）。
- 本番HA（master3 + data3 など）→ **数百ドル/月**。
- いずれも**アイドルでも課金**。夜間停止運用（ADBのような stop/start）は基本想定されない。

> 正確な料金はテナンシの契約単価に依存。`limits value list --service-name` の正式名は未確定
> （`open-search` は404）。本番採用時は OCI Cost Estimator で実シェイプ見積りを取ること。

## 3. 統合方式（RAGとしての使い方）

1. **プロビジョニング**: Terraform `oci_opensearch_cluster`（VCNのプライベートサブネットに配置）。
2. **インデックス設計**: `knn_vector`（OpenSearch k-NNプラグイン）フィールド + 本文テキスト。
3. **取り込み**: 文書をチャンク → OCI GenAI 埋め込み（cohere.embed-v4.0 等）→ ベクトル+本文をindex。
4. **検索**: クエリを埋め込み → k-NN検索、または **ハイブリッド（BM25字句 + ベクトル）** をスコア正規化で統合。
5. **接続/認証**: クラスタはプライベートサブネット。API CI からHTTPS REST（`opensearch-py` か生REST）で
   呼ぶ。OpenSearch security（basic認証）。CIのRP→クラスタへのネットワーク経路（セキュリティリスト）が必要。
6. **アプリ実装**: 新規 `rag_opensearch.py` バックエンド + `/rag` のセレクタに選択肢追加（既存と同型）。

## 4. 既存2方式との差別化（詳細は comparison/rag-backends.md）

OpenSearchが勝つケース:
- **ハイブリッド検索**（字句BM25 + ベクトル）や全文検索・ファセット/集計が要るとき
- **大規模コーパス**・高QPS・厳密な関連度チューニングが要るとき
- 既存のELK/OpenSearchエコシステム資産があるとき

本プロトタイプの実情:
- 既に **File Search（採用中・9/10）** と **Select AI RAG（10/10・26ai）** の2方式が稼働。
- 上記の高度要件は現時点で顕在化しておらず、**常設クラスタ費用に見合う差別化が薄い**。

## 5. go/no-go（ゲート判断）

**条件付きGO**:
- **既定では非採用を推奨**（常設コスト > 現状の便益。既存2方式で要件充足）。
- **採用条件**: ハイブリッド/全文検索・大規模・関連度チューニングが要件化したとき。
- 実装は「**統合設計＋Terraformを用意し、既定は無効（クラスタ未作成）**」の形で先行可能。
  クラスタ作成＝課金発生＝**人間承認が必要**（CLAUDE.md）。

## 6. 次アクション（ユーザー判断待ち）
- (A) クラスタをプロビジョニングして実装・実機計測まで進める（コスト承認が必要）。
- (B) `rag_opensearch.py` + Terraform を**無効状態で先行整備**し、必要時に有効化（課金なし）。
- (C) 今回は調査記録のみで見送り、ENH-06へ進む。
