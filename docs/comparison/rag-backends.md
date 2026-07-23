# OCIにおけるRAGバックエンド比較 — Vector Store/File Search vs Select AI RAG vs Agents KB

日付: 2026-06-11（実機計測ベース。プリセールス転用可）
計測条件: ap-osaka-1 / 同一の日本語社内規程3文書（PDF/txt/md、SPIKE-03文書セット）/ 同一の日本語10問

## 結論サマリ

| | ① OpenAI互換 Vector Store + File Search | ② **Select AI with RAG**（DBMS_CLOUD_AI） | ③ Generative AI Agents ナレッジベース |
|---|---|---|---|
| **本プロトタイプ** | ✅ **採用（RAG-01/02）** | 検証済み・切替候補（RAG-03） | 机上比較のみ |
| 正答率（10問） | **9/10**※1 | **10/10** | 未計測 |
| 正引用 | **10/10**（スコア付きannotations） | 10/10（出典が本文末尾に自動付与） | （出典表示あり） |
| レイテンシ中央値 | 4.3s（TTFT 4.2s、ストリーミング） | **1.6s**（同期・非ストリーミング） | 未計測 |
| 取り込み速度 | 1ファイル約10秒 | 3文書20秒（索引一括構築） | バケット同期（非同期ジョブ） |

※1 ミスの1件は「50キロ」を「50km」と表記した表記揺れで、内容は正答。

## 定量比較の詳細

- ①はアプリ経由（API GW + FastAPI + gpt-oss-120b reasoning + file_searchツール）のエンドツーエンド計測。レイテンシにはモデルの推論時間を含む
- ②はADB直接（DBMS_CLOUD_AI.GENERATE action=narrate、llama-3.3-70b + cohere.embed-multilingual-v3.0）。アプリ経由にすると+数百msの見込み
- ①のTTFT≒全体時間（file_search実行が先行し、本文は一気に出る）。**体感は②が圧倒的に速い**が、①はストリーミング表示で長文時の体感を補える

## 特性比較（実機で確定した事実ベース）

| 観点 | ① Vector Store/File Search | ② Select AI RAG | ③ Agents KB |
|---|---|---|---|
| **DBバージョン要件** | なし | **ADB 23ai必須**（19cは `ORA-20047` で索引作成不可 — 実機確定） | なし（独立サービス） |
| データの置き場所 | Files API（Enterprise AIプロジェクト内） | **Object Storageバケット直結** + ADB内ベクトル表 | Object Storageバケット直結 |
| 文書の更新反映 | ファイル単位でAPI登録（即時） | refresh_rate間隔のバケット自動同期（手動REFRESHも可） | 同期ジョブ |
| 対応形式 | pdf/txt/md（**docx不可** — 実機確定） | pdf/txt/md/docx等（DBMS_CLOUD対応形式） | サービス定義に従う |
| 引用 | annotations+スコア+チャンク本文（UI自由度高） | 本文末尾にSources自動付与（書式固定気味） | 引用表示あり |
| ストリーミング | ○（Responses API） | ×（同期SQL） | エージェントAPI経由 |
| 使用モデル | Responses系（gpt-oss）に限定 | プロファイルで選択（llama等。embeddingも選択可） | サービス管理 |
| 会話文脈との統合 | ○（チャット基盤のツールとして同居） | △（SQL関数。会話状態は自前管理） | エージェント側で管理 |
| ユーザー分離 | ストア分離（per-userで実装済み） | スキーマ/索引/WHERE設計次第 | KB/エージェント分離 |
| 権限・接続 | IAM署名のみ | DB credential + ACL + 23ai | IAMポリシー |
| 構造化データとの融合 | × | **○（SQLと同居 — NL2SQLとの親和性が武器）** | × |

## 採用判断（本プロトタイプ）

**①を主バックエンドとして採用**。理由:
1. チャット基盤（Responses API）のツールとして自然に統合でき、ストリーミング・引用UI・会話文脈が一体で動く
2. jetusedev ADBが19cのため②は現状動かない（23aiアップグレードが前提条件）
3. 日本語検索品質はSPIKE-03で10/10を確認済み

**②への切替/併用を検討すべき条件**:
- ADBを23ai化できる場合で、かつ低レイテンシ（1〜2秒応答）が要件のとき
- 文書の正本をObject Storageバケットで運用したい（バケット直結・自動同期）とき
- **Phase 5（NL2SQL）と統合し、構造化+非構造化の横断回答**をやるとき（②の最大の差別化。DB内で完結する）
- docx対応が必須のとき（①は前処理が必要）

**③が向くケース**: アプリを作らずマネージドなエージェント+KBで完結させたい場合（本プロトタイプはUI/挙動の自由度を優先し対象外）。

## 検証の再現手順

- ①: `packages/api`（RAG-01/02実装）+ docs/verification/jetuse-app/RAG-01-02.md
- ②: `spikes/spike08_select_ai_rag.py`（23ai ADBに対して実行。jetuse-spike-adb23は検証後STOPPED）
- 質問セット: SPIKE-03と同一（spikes/spike03_vector_store.py QUESTIONS）

## 追補（2026-06-11 RAG-03実装後）: 両バックエンドがアプリで切替可能に

- jetuse-dev-adbを26aiへアップグレードし、**アプリの /rag からセレクタで切替可能**（`rag_backend` パラメータ）
- アプリ経由の実測: Select AI 初回28.5s（per-userのprofile+索引の遅延作成込み）/ **2回目以降2.6s**。File Search 4.3s（中央値）
- 同一アップロードが両バックエンドに供給される設計（Files API + `rag/{owner}/` バケット共用）のため、リアルタイムのA/B比較デモが可能

## 追補（2026-06-17 SPIKE-E2）: ④ OCI Search with OpenSearch

ENH-05のゲート調査として4つ目の選択肢を比較。詳細は `docs/verification/spikes/SPIKE-E2.md`。

| 観点 | ④ OpenSearch（OCI Search with OpenSearch） |
|---|---|
| 可用性 | ap-osaka-1 で利用可（CLI/SDK・cluster list成功、2026-06-17実機） |
| 形態 | **常時稼働のマルチノードクラスタ**（master+data+dashboard±search node） |
| **コスト** | **常設課金（アイドルでも）**。最小~$100〜150/月、本番HAで数百ドル/月。①②と異なり「使わなくても」発生 |
| 検索方式 | **ハイブリッド（BM25字句＋k-NNベクトル）**・全文検索・ファセット/集計が強い |
| ベクトル | k-NNプラグイン。埋め込みはOCI GenAI（cohere.embed-v4.0等）を自前で投入 |
| スケール | 大規模コーパス・高QPS・関連度チューニング向き |
| 取り込み | 自前パイプライン（チャンク→埋め込み→index）。①②より実装量が多い |
| 接続 | プライベートサブネットのRESTエンドポイント（basic認証）。CIからの経路設定が要る |
| 本プロトタイプ | **実装・採用可（2026-06-17、ユーザー承認）**。最小クラスタを常設し /rag で選択可。E2E成功 |
| ハマり所 | `security_mode=DISABLED` でも 9200はTLS → https + verify=False 必須。埋め込みはネイティブ `embed_text`(OpenAI互換 /embeddings は400) |

### ①②③④の使い分け（まとめ）
- **①Vector Store/File Search**: チャット基盤に自然統合・ストリーミング/引用UI。実質サーバレスで**常設費なし**。→ 既定採用。
- **②Select AI RAG**: 低レイテンシ・**構造化データと同居**（NL2SQL融合）。稼働中ADBを再利用し**増分の常設費なし**。
- **③Agents KB**: アプリを作らずマネージドで完結したいとき。
- **④OpenSearch**: **ハイブリッド/全文検索・大規模・関連度チューニング**が要件化したとき。ただし**常設クラスタ費**が前提。

## 用語注記（2026-06-11）

本書の「Select AI RAG」は正式には **Select AI with RAG**（`action=>'narrate'` + ベクトル索引）。Oracleの「Select AI」は機能ファミリー総称で、**Select AI (NL2SQL)**（showsql/runsql — Phase 5のSQL Search比較対象）とは別機能。
