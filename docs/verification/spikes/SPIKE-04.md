# SPIKE-04: SQL Search (NL2SQL) 検証

実施日: 2026-06-10（IAM整備後の完結評価まで同日） / リージョン: ap-osaka-1 / 実行: `spikes/spike04_select_ai.py` / `spikes/spike04_sql_search.py`

> **完結**: IAM整備後の再検証で SQL Search は **10/10 正答**（§結果3）。Select AI比較も完成。

## 目的

ADB（SHサンプルスキーマ）に対し、①OCI GenAI **SQL Search**（SemanticStore + GenerateSqlFromNl）のセットアップと生成品質、②ADB内 **Select AI**（DBMS_CLOUD_AI）との比較を実機評価する。

## 構築したリソース（すべて jetuse-spike- プレフィックス）

| リソース | 名前 | 備考 |
|---|---|---|
| ADB (ECPU 2, ADW) | jetuse-spike-adb | mTLS不要・ACL公開（スパイク用）。SH/SSBサンプル同梱を確認（SH.SALES 918,843行） |
| DBユーザー | JETUSE_QUERY | CREATE SESSIONのみ。SHはPUBLIC公開のため追加GRANT不要（個別GRANTはORA-01031） |
| Vault / Key | jetuse-spike-vault / jetuse-spike-key | シークレット2件（ADMIN/JETUSE_QUERYパスワード） |
| DBTools接続 | jetuse-spike-dbconn-enrich (ADMIN) / jetuse-spike-dbconn-query (JETUSE_QUERY) | validate両方OK |
| SemanticStore | jetuse-spike-semstore | **ACTIVE（大阪で作成可能と確認）** |

## 結果1: SQL Search — セットアップは成功、enrichmentはIAM未整備でブロック

- **SemanticStoreは大阪で作成可能**（`oci generative-ai semantic-store create` → ACTIVE）。NL2SQL APIは独立バージョン **`/20260325`**（ホスト: inference.generativeai）にあり、`POST /semanticStores/{id}/actions/enrich` と `.../actions/generateSqlFromNl` を確認。
- enrichmentジョブ（FULL_BUILD, schema=SH）は **FAILED**（lifecycleDetails空）。`generateSqlFromNl` は「Requested resource not found for provided schema/metadata」→ 濃縮メタデータ不在。
- 原因はサービス側リソースプリンシパルのIAM未整備が濃厚。公式要件は**テナンシレベルの動的グループ**（`resource.type='generativeaisemanticstore'`）+ コンパートメントポリシー5本（database-tools-family use / secret-family read / database-family read / autonomous-database-family read / generative-ai-family use）。現ユーザーは動的グループの参照・作成権限なし → **人間の事前作業として `docs/setup/iam.md` に手順化**。IAM整備後に同スクリプトで再検証する。

## 結果2: Select AI（DBMS_CLOUD_AI）— 日本語10問評価

セットアップ: APIキー認証のDBMS_CLOUD credential（IAM変更不要）+ プロファイル（`object_list: SH`, `comments: true`）。ハマり点: `~/.oci/oci_api_key.pem` 末尾の `OCI_API_KEY` マーカー行を除去しないとORA-20401になる。

生成SQLのみ取得（`action=>'showsql'`）→ 読取専用ユーザーで実行し正解値と突合（SELECT以外拒否ガードも実装済み）:

| # | 質問 | llama-3.3-70b | command-a-03-2025 |
|---|---|---|---|
| 1 | 2001年の売上合計 | ○ 28,136,461.98 | ○ 同値 |
| 2 | チャネル別売上合計 | ○ | ○ |
| 3 | 売上トップ3カテゴリ | ○ | ○ |
| 4 | 顧客数の多い国トップ5 | ○ | ○ |
| 5 | 1999年売上トップ5商品 | ○ | ○ |
| 6 | 2001年月別売上推移 | ○（月番号で正順ソート） | ○ |
| 7 | プロモーション別トップ5 | ○ | ○ |
| 8 | 平均販売単価最高の商品 | ○ | ○ |
| 9 | 2001年売上最大の四半期 | × FISCAL四半期を使用し誤答（正解: 2001-04） | × 集計ロジック誤りで2001-02 |
| 10 | Internetチャネル2000年売上 | × `channel_desc='internet'`（小文字）でNULL | ○ `UPPER()`比較で正答 1,881,976.76 |
| | **正答率** | **8/10** | **9/10** |

### 品質所見

- 日本語質問の理解・JOIN・集計・`FETCH FIRST` の使い方は両モデルとも安定。日本語の列エイリアス（「商品カテゴリ」等）も生成できる。
- 失敗パターンは2つ: **①データ値の表記揺れ**（'Internet' vs 'internet' — セマンティック濃縮で値サンプルを持つSQL Searchが解決するはずの領域）、**②「最大の四半期」のような集計+順位の複合**（fiscal/calendar の曖昧さ）。
- ビジネス用語辞書（コメント・アノテーション）を整備すればSelect AIでも実用水準に到達可能。

## 結果3: SQL Search 完結評価（IAM整備後・2026-06-10）

### 重要な発見: IAM整備「後」にSemanticStoreの作り直しが必要（未文書仕様）

IAM（動的グループ+ポリシー、`docs/setup/iam.md` の統合版）整備後も、**整備前に作成したストアではenrichmentがFAILEDのまま**（3回再試行・25分以上待っても同じ。lifecycleDetailsは常に空）。**新しいSemanticStoreを作成したら一発でSUCCEEDED**。
→ ストア作成時点の権限状態が内部に固定される模様。**デプロイ手順は「IAM → SemanticStore作成 → enrich」の順序厳守**とし、順序を誤ったらストア再作成。

- enrichment FULL_BUILD (SH): 約4分でSUCCEEDED
- 旧ストア jetuse-spike-semstore は削除、`.env` の `SEMSTORE_OCID` は新ストア（jetuse-spike-semstore2）に更新済み

### 日本語10問評価（Select AIと同一質問・同一突合方式）

| # | 質問（要約） | SQL Search | 備考 |
|---|---|---|---|
| 1〜8 | 集計・JOIN・トップN系 | **○ 全問正答** | Q1=28,136,461.98 等、Select AIの正解値と一致。Q6のみORDER BY無し（値は正、並びは未指定） |
| 9 | 2001年売上最大の四半期 | **○ 2001-04** | **Select AIは両モデルとも誤答**（fiscal/calendar混同）→ CALENDAR_QUARTER_DESCを正しく選択 |
| 10 | Internetチャネル2000年売上 | **○ 1,881,976.76** | `UPPER()`比較を自動採用。**llamaが落とした表記揺れを解決** |
| | **正答率** | **10/10** | Select AI: command-a 9/10 / llama 8/10 |

### 性能・API挙動

- `generateSqlFromNl` は**同期API**（ジョブポーリング不要）。ただし**1問あたり29〜39秒**（平均約34秒）→ チャットUIでは思考中表示・ストリーミング無しの長待ち設計が必須（CHAT系タスクへの要件）
- セマンティック濃縮の本領（値の表記揺れ・暦の曖昧さの解決）を実測で確認。Phase 5の主バックエンド採用を確定材料とする

## 設計への影響

1. Phase 5（SQL-01/02）の主役は引き続きSQL Search（SemanticStore）とするが、**IAM事前作業（動的グループ）が顧客環境でも必須**になるため、デプロイガイドに明記。Select AIバックエンドは「IAM変更を避けたい顧客向けフォールバック」として正式オプション化する価値がある（今回の実測で9/10）。
2. NL2SQLチャットフローの「生成のみ→ユーザー確認→読取専用実行」は今回のスクリプト構造（showsql→ガード→JETUSE_QUERY実行）をそのまま昇格できる。
3. SQL Search APIはバージョン `/20260325` で、現行oci CLIにデータプレーンコマンドがない → FastAPIからはraw署名リクエスト（httpx + IAM署名）で叩く。

## 残課題

- ~~【人間】動的グループ+ポリシー作成 → enrichment再実行 → 10問評価~~ → **完了（結果3）**
- ~~Q9/Q10系の失敗をセマンティック濃縮が解決するか~~ → **解決を実測確認**
- ~~GenerateSqlFromNlJobの同期/非同期挙動~~ → **同期（約34秒/問）**

## 実行ログ

`spikes/data/spike04_results_*.json` に全問の生成SQL・実行結果を保存（SQL Search分は `spike04_results_sql_search.json`）。
