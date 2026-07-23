# SPIKE-03: Vector Store / File Search 検証

実施日: 2026-06-10 / リージョン: ap-osaka-1 / 実行: `spikes/spike03_vector_store.py`, `spikes/spike03b_forced_search.py`

## 目的

日本語の社内規程風ダミー文書（PDF/docx/md）をVector Storeに取り込み、File Searchツール経由のRAG応答を実機検証。①対応形式 ②日本語検索品質（10問採点）③メタデータフィルタ ④引用情報の取得形式を確認する。

## アーキテクチャ上の発見（実機で確定）

1. **Vector Store本体のCRUDはコントロールプレーン**（`https://generativeai.{region}.oci.oraclecloud.com/20231130/openai/v1/vector_stores`）。推論側 `inference.generativeai...:/openai/v1` には `/vector_stores/{id}/files|file_batches|search` のサブリソース操作のみ登録されている。OpenAIと同名のAPIでもホストが分かれる。
2. **Files / Conversations 等の状態を持つデータプレーンAPIは `OpenAi-Project` ヘッダ必須**。値は `GenerativeAiProject` リソース（OCI管理APIで作成、`oci generative-ai generative-ai-project create`）のOCID。→ **OpenAIの"Projects"に相当する分離概念はOCIに存在する**（SPIKE-05のADRに反映）。
3. **非同期伝播に注意**: Vector Store作成後、CP側 `status=completed` になってもDP側から見えるまで追加で約10〜30秒かかる。アプリ実装ではDP側のリスト取得が200になるまで待つ必要がある。
4. Vector Store IDは `vs_kix_...` 形式（OCIDではない）。`file_search` ツールの `vector_store_ids` にそのまま渡す。

## 対応形式

| 形式 | 結果 |
|---|---|
| PDF（日本語・CIDフォント） | ✅ completed |
| md | ✅ completed |
| txt | ✅ completed |
| **docx** | ❌ `unsupported_file: Unsupported file type 'docx'` |

**docx非対応はAWS版との差分の機能差**。アプリ側でdocx→テキスト抽出（python-docx等）してから取り込む前処理が必要。file_batchesは1ファイルでも未対応形式が混ざると全体が400になるため、ファイル単位の取り込み+状態管理が無難。

## 日本語検索品質（10問評価）

### Vector Store直接検索（`/vector_stores/{id}/search`）: **top1一致 10/10**

| 質問 | top1 | スコア |
|---|---|---|
| 出張の定義 | travel-policy.pdf | 0.649 |
| グリーン車を使えるのは誰 | travel-policy.pdf | 0.535 |
| 管理職の日当 | travel-policy.pdf | 0.836 |
| 東京23区内宿泊の上限 | travel-policy.pdf | 0.878 |
| 精算期限 | travel-policy.pdf | 0.637 |
| 在宅勤務の週上限 | remote-work-policy.txt | 0.752 |
| 在宅勤務手当 | remote-work-policy.txt | 0.682 |
| 公衆Wi-Fi可否 | remote-work-policy.txt | 0.436 |
| 領収書必須の金額 | expense-guidelines.md | 0.668 |
| タクシー利用条件 | expense-guidelines.md | 0.278 |

正解文書とのスコア差が大きく（2位は0.01〜0.31）、日本語チャンク品質は実用水準。

### Responses API + file_searchツール（モデル: openai.gpt-oss-120b）

| 条件 | キーワード正答 | 正引用 |
|---|---|---|
| instructionsなし | 7/10 | 5/10 |
| **instructionsでツール使用を強制** | **10/10** | **9/10** |

instructionsなしでの取りこぼしは検索品質ではなく**モデルがツールを呼ばず一般論で回答した**ケース。「必ずfile_searchを使い検索結果のみに基づき回答」と指示すれば実用水準に達する。RAGチャットのシステムプロンプトに必須で組み込む。残る1件の引用欠落（タクシー）は回答自体は正答でannotationsのみ欠落。

## メタデータフィルタ

`attributes`（`vector_stores.files.create` 時に付与）+ `filters={"type":"eq","key":"category","value":"hr"}` が動作。カテゴリ・部署単位の絞り込みRAGが実装可能。

## 引用レスポンス構造

`include=["file_search_call.results"]` 指定で `response.output[]` に以下が入る:

```jsonc
{
  "type": "file_search_call",
  "results": [
    {
      "file_id": "file-kix-...",
      "filename": "travel-policy.pdf",
      "score": 0.839,
      "text": "出張旅費規程（株式会社サンプル商事）...(チャンク全文)",
      "attributes": {"category": "travel", "dept": "soumu"},
      "vector_store_id": "vs_kix_..."
    }
  ]
}
// message側: content[].annotations[] に filename / file_id（引用元表示UIに使用）
```

引用元表示UIは `file_search_call.results`（スコア・チャンク本文つき）と `annotations`（本文との対応付け）の両方を使える。JetUseの引用表示と同等のUIが構築可能。

## 設計への影響

- RAG主バックエンドとしてVector Store + File Searchは**採用可能**（日本語品質は10/10、引用構造も十分）
- 取り込みパイプライン: アップロード→（docxは前処理）→ファイル単位でVector Store登録→ステータスポーリング、を非同期ジョブ化（RAG-01の仕様に反映）
- `GenerativeAiProject` をテナント/ワークスペース分離の単位として使える
- Object Storageバケット連携の自動同期は `VectorStoreConnector`（OCI管理API）で可能 → RAG-01の「バケット自動更新」はこれを使う

## 残課題

- サイズ上限・ファイル数上限の境界テスト（大きいPDFでの追試）
- VectorStoreConnector経由の取り込み実機検証（RAG-01で実施）
- チャンクサイズ/オーバーラップのチューニング可否（`chunking_strategy` パラメータの対応状況）

## 残置リソース

- GenerativeAiProject: `jetuse-spike-project`
- Vector Store: `jetuse-spike-vs`（vs_kix_3xwx0nll...）+ Files 4件
