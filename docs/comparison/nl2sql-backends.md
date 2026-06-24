# OCIにおけるNL2SQLバックエンド比較 — SQL Search vs Select AI (NL2SQL)

日付: 2026-06-11（実機計測ベース。プリセールス転用可）
計測条件: ap-osaka-1 / jetusedev ADB **26ai** / SHサンプルスキーマ / 同一の日本語10問（SPIKE-04質問セット）/ アプリAPI経由（`spikes/cp3_nl2sql_compare.py` で再現可能）

## 結論サマリ

| | SQL Search（SemanticStore + generateSqlFromNl） | Select AI (NL2SQL)（DBMS_CLOUD_AI showsql, llama-3.3-70b） |
|---|---|---|
| **本プロトタイプ** | ✅ **主バックエンド採用** | UIの比較用モードとして併設（SQL-04） |
| SQL生成成功 | 10/10※1 | 10/10 |
| **結果の正答** | **10/10**（※2） | **8/10**（四半期の取り違え1件・誤フィルタでNULL1件） |
| 生成レイテンシ中央値 | **31.6s** | **1.8s** |
| 事前準備 | SemanticStore + enrichment（約5分、スキーマ変更時に再実行） | プロファイル作成のみ（数秒） |

※1 アプリ経由のバッチ実行では10問中2問で一時的なSSE切断が発生（単発再試行では成功・SQL正答。生成品質ではなく伝送層の間欠事象 — backlog #12）
※2 8問はバッチで実行正答、切断2問は単発再実行で正答SQLを確認

## 品質差の実例（2026-06-11実測）

**Q9「2001年で売上が最大だった四半期はいつですか」**
- 正解（DB直接集計）: **2001-Q4**（7,470,897.52）
- SQL Search: `2001-04` ○ / Select AI: `2001-03` ✗ — **SPIKE-04（19c時代）で確認した四半期の取り違え傾向が26aiでも残存**

**Q10「インターネットチャネルでの2000年の売上合計」**
- SQL Search: 1,881,976.76 ○ / Select AI: NULL（フィルタ誤り）✗

一方で**残り8問は両者の結果数値が一致**しており、Select AIの実用性も26ai+llamaで大きく向上している（SPIKE-04時点より改善）。

## 特性比較

| 観点 | SQL Search | Select AI (NL2SQL) |
|---|---|---|
| 精度の源泉 | **セマンティック濃縮**（enrichmentでスキーマの意味情報を事前構築） | スキーマ+コメントをプロンプトに注入 |
| 速度 | 30秒前後（同期APIをSSE+keepaliveでラップ） | **2秒前後**（DB内で完結） |
| インフラ | SemanticStore / DBTools接続 / Vault / IAM動的グループ | DBプロファイル+credentialのみ |
| DBバージョン | 制約なし（19cでも可） | NL2SQL自体は19c可（**RAG用ベクトル索引は23ai+**） |
| スキーマ変更追従 | enrichment再実行が必要 | 即時（都度プロンプト構築） |
| 実行の安全性 | どちらもアプリ側で同一ガード（読取専用ユーザー・SELECT限定・行数/時間上限） | 同左 |

## 採用判断と使い分け

- **正確性が問われる業務質問（経営数値など）→ SQL Search**。30秒の待ちはUI（経過表示+キャンセル）で吸収
- **アドホックな探索・速度優先 → Select AI** も実用域。誤答パターン（期間解釈・フィルタ）があるため、**生成SQLをユーザーに見せて確認してから実行するUI**（本アプリの設計）が安全弁として機能する
- 両者はUIのセレクタで切替可能（/dbchat）— この比較を顧客の目の前で実演できる

## 再現手順

`python spikes/cp3_nl2sql_compare.py <APIGW_HOST> <BEARER_TOKEN>`（SSE切断リトライ付き）

## 補足: SemanticStoreのサービス上の位置づけ（用語整理 2026-06-11）

SemanticStoreは「Enterprise AI **Agents**」の機能ではなく、**OCI Enterprise AIサービス内の独立機能（SQL Search）**。

```
OCI Enterprise AI（サービス本体）
├─ ① OpenAI互換 agentic API（/openai/v1）… Responses/Conversations/Files/Vector Stores 等
│     =「Enterprise AI Agents」と呼ばれる面。チャット短期メモリ・RAGのVector Storeはここ
├─ ② SQL Search（/20260325 独立バージョン）… SemanticStore/enrichment/generateSqlFromNl
│     = DBチャットが使用。①とはAPI体系が別（専用IAMリソースタイプ generativeaisemanticstore、
│       管理は oci generative-ai semantic-store、SDKは oci.generative_ai_data）
└─ ③ オンデマンド推論基盤
※ OCI Generative AI Agents（エージェント+KB）はさらに別サービス（comparison/rag-backends.md ③参照）
```

RAGのVector Store（①）とSQL SearchのSemanticStore（②）は似た役割名だが別リソース体系である点に注意。
