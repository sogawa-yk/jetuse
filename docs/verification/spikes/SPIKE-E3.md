# SPIKE-E3: Oracle "Trusted Answer Search" の正体と適用可否

ENH-06のゲート調査（用語確定が先）。**結論: 正体は確定／本プロトタイプのマネージドADBでは現時点 no-go（A項目=未提供として記録）**。

## 1. 正体の確定（2026-06-17）

**Oracle Trusted Answer Search (TAS)** は **Oracle AI Database 26ai の機能**。
「自然言語の質問 → 事前にキュレーションされた検索ターゲット（レポート/URL/SQLビュー）への
マッピング」を行う **language-to-target mapping system**。

- **LLMを使わず**（生成しない）、**決定的・事前承認済みの結果**を返す
  → ハルシネーションなし・再現性・セキュア（"without the chatbot guesswork"）。
- 基盤は **Oracle AI Vector Search**（質問とターゲット記述・サンプルクエリの意味的ベクトル一致）。
- 構成オブジェクト: **Search Space**(バージョン管理された検索ターゲット集合) / **Search Target**
  (URL/レポート/SQLビュー＋説明＋サンプルクエリ) / **Target Action**(URL or SQL) /
  **Target Inputs**(`:QUARTER` 等のプレースホルダ) / **Target Value Sets**(抽出許容値)。
- 提供形態は3経路:
  1. **Admin APEX アプリ**（検索空間の構築・管理）
  2. **Portal APEX アプリ**（エンドユーザー用UI）
  3. **Search API**（非APEXのカスタム統合用）= PL/SQL **`DBMS_TRUSTED_SEARCH.SEARCH()`**
     （NL質問を受け、構造化JSONを返す）。

→ RAG（取得した断片をLLMが生成）とは別物。**「精選ターゲットへの決定的ルーティング/回答」**。

## 2. 適用可否（本プロトタイプのマネージドADB）

**実機判定（2026-06-17, jetuse-dev-adb）**:
- ADBバージョン: **23.26.2.2.0（26ai）**
- **`DBMS_TRUSTED_SEARCH` パッケージは存在しない**（`all_objects`/`all_procedures` で0件）。
  - 対照: `DBMS_CLOUD_AI`(49) / `DBMS_CLOUD_AI_AGENT`(53) は存在。
- TRUSTED/TAS関連オブジェクトも無し（ヒットした `ADBTASK_*` は無関係）。

→ **TASの統合API（DBMS_TRUSTED_SEARCH）は、現時点で当該 Autonomous Database Serverless に
未提供**。Database 26ai の機能としては存在するが、我々のマネージドADBからは呼べない。

## 3. go/no-go

**no-go（現時点）。A項目（マネージドで未提供）として記録。**
- 実体は明確（26ai機能・Search API=DBMS_TRUSTED_SEARCH.SEARCH・APEXアプリ同梱）だが、
  本プロトタイプのADB Serverlessにパッケージが無く、**そのままでは適用不可**。
- ADB Serverless へ提供（または APEX 同梱アプリの有効化）が確認できた時点で再評価する。

### 自前実装という代替（参考・今回は採らない）
TASの本質は「精選Q（説明/サンプルクエリ）→ターゲット のベクトル一致で決定的に返す」パターン。
これは**我々のADB 26ai AI Vector Search ＋ embeddings（embeddings.py）で自前実装可能**
（精選ターゲット表をベクトル化し、しきい値付きk-NNで最良一致を返す。一致なしは「該当なし」）。
ただし「マネージドのTAS機能を使う」という要件とは別物（再実装）であり、既に3つのRAG方式＋
Select AIがあるため、要件化したときに改めて検討する。

## 4. 参考
- Oracle Trusted Answer Search 製品ページ: https://www.oracle.com/database/trusted-answer-search/
- ドキュメント(26): https://docs.oracle.com/en/database/oracle/oracle-database/26/otasc/trusted-answer-search-overview.html
- FAQ(Search API=DBMS_TRUSTED_SEARCH.SEARCH): https://docs.oracle.com/en/database/oracle/oracle-database/26/otasc/trusted-answer-search-faq.html
- 発表ブログ / 解説（"semantic search without LLMs"）: blogs.oracle.com/database, CIO, InfoWorld
