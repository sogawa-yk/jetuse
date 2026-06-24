# 計画: 機能拡張(enhance.md)の実装

対象は `docs/feedbacks/enhance.md` の要望群。従来方針どおり
**各テーマは「実現可能性調査(SPIKE)→ ゲート判定 → 実装」の順**で進める。
OCIサービス選択を伴うものは `docs/comparison/` に比較を残す（プリセールス転用可能な粒度）。
実機検証主義（レポートは `docs/verification/`）。タスクIDは `ENH-NN`。1タスク=1ブランチ→main。

## 現状(2026-06-15)の関連実装

- **DBチャット**: `jetuse_core/nl2sql.py`(SQL Search=SemanticStore `generateSqlFromNl` / Select AI=`DBMS_CLOUD_AI` profile `JETUSE_SQL_AI` / 読取専用 `JETUSE_QUERY` 実行) + `pages/dbchat.tsx`(バックエンド切替・サンプル質問・グラフ提案)。対象は SH サンプルスキーマ固定。
- **RAG**: `jetuse_core/rag.py`(Vector Store + file_search) + `rag_select_ai.py`(Select AI with RAG)。
- **エージェント**: ADR-0009で **SDK選択式の完全hosted**(OpenAI Agents SDK / ADK / LangGraph の3コンテナ)。
- **音声**: `stt_realtime.py`(WHISPERリアルタイム=partial無し) + `pages/realtime.tsx` / `tts.py` / `pages/voicechat.tsx`。
- **映像**: `pages/video.tsx`(ブラウザでフレーム抽出→vision модель、MM-01)。
- **ADB**: Oracle Database **26ai**(Select AI / Select AI Agent 系機能が利用可能な世代)。

---

## 全体の進め方とゲート

```
Phase E-investigate（調査優先・並行可）
  ├ SPIKE-E1 Select AI Agent / プリビルドエージェント(ADB 26ai)         … go/no-go=エージェント新種別の土台
  ├ SPIKE-E2 OpenSearch(OCI Search with OpenSearch)によるRAG            … マネージド可用性・統合方式
  ├ SPIKE-E3 Oracle "Trusted Answer Search" の正体と適用可否             … 用語確定が先（後述）
  ├ SPIKE-E4 OCI Document Understanding(OCR)                            … 大阪可用性・日本語精度
  ├ SPIKE-E5 OCI Language 翻訳 vs Enterprise AI LLM翻訳                  … 大阪可用性・速度比較
  └ (CSVアップロード/スキーマ選択/UI改善/音声UIは調査軽=既存機構の延長)
      ↓ 各ゲートで go/no-go（ユーザー承認）
Phase E-implement（goになったものだけ）
```

優先順の考え方: **DBチャット強化(要望1-3)は既存資産の延長で確度高 → 先行**。
エージェント新種別(要望4)・RAG拡張(要望7)・OCR(要望8)・翻訳化(要望9)は調査結果次第。
音声UI(要望5)・映像エラー(要望6)は軽微改修として随時。

---

## テーマA: DBチャット強化（要望1〜3）

### ENH-01: 構造化データ(CSV等)のアップロード→DBチャット対象化

**目的**: ユーザーがCSV等をアップロードし、その表に対して自然言語で質問できるようにする。

**現状**: 対象は SH サンプル固定。アップロード機構なし。

**調査(SPIKE-E1a・軽)**:
- 取り込み先の方式比較(`docs/comparison/csv-ingest.md`):
  1. **ADBへロード**: `DBMS_CLOUD.COPY_DATA`/`CREATE TABLE`(Object Storage経由) or ADB組込のデータロード。
     SQL Search/Select AIの対象に自然に乗る(既存NL2SQL資産をそのまま活用)。
  2. クライアント/メモリ内(DuckDB等): ADBに載せず分析。マネージド軸から外れるため非推奨。
- ユーザーごとのスキーマ分離(`JETUSE_APP` 配下にユーザー名前空間 or 一時表)とクリーンアップ方針。
- 型推論・列名サニタイズ・サイズ上限・PII配慮。
- **ゲート**: ADBロード方式で「アップロード→質問」がE2Eで成立するか。マネージド(DBMS_CLOUD)で完結するか。

**実装(goの場合)**:
- `POST /api/db/datasets`(CSVアップロード→Object Storage一時格納→`DBMS_CLOUD.COPY_DATA`でユーザー専用表作成)。
- SemanticStore/Select AIプロファイルの `object_list` に当該表を追加(動的)。dbchatの対象に選択可能化。
- 完了条件: CSVアップロード→生成された表に対しNL2SQLが回り結果表示。レポート `docs/verification/ENH-01.md`。

**リスク/見込み**: 中。ADBロードはDBMS_CLOUDでマネージド。スキーマ分離と後始末の設計が肝。

### ENH-02: 検索対象スキーマ/テーブルの選択＋データ中身の参照

**目的**: dbチャットで対象スキーマ/テーブルを選べ、テーブルの中身(サンプル行)も参照できる。

**現状**: `nl2sql.get_schema_info()` が SH のテーブル/カラム情報をUIに出す(参照のみ・固定)。

**調査(軽)**: 既存 `get_schema_info` をスキーマ引数対応に拡張できるか。読取専用ユーザーの権限範囲で
他スキーマ(ユーザーCSV表含む)のメタデータ/サンプル行取得が可能か。Select AI/SemanticStoreの
対象スコープを選択スキーマに動的に絞れるか。

**実装**:
- `GET /api/db/schemas`・`GET /api/db/tables?schema=`・`GET /api/db/preview?table=`(先頭N行・読取専用ガード)。
- dbchatに「対象スキーマ/テーブル選択」+「テーブルプレビュー」パネル。NL2SQLの対象スコープへ反映。
- 完了条件: スキーマ/テーブル選択→中身プレビュー→その範囲で質問。

**リスク/見込み**: 低〜中。既存メタデータ取得の延長。

### ENH-03: DBチャットUI/UX充実（プロファイル選択・モデル変更・対象DB制御）※複雑化しない範囲

**目的**: バックエンド方式(SQL Search/Select AI)・使用モデル・対象データ等を分かりやすく制御。

**現状**: バックエンド切替トグル + サンプル質問 + グラフ提案あり。モデルは固定気味。

**調査/設計(軽)**: 露出する操作を「プロファイル(SQL Search / Select AI / 接続/権限プロファイル) /
モデル / 対象スキーマ・テーブル」に整理。過剰露出を避けるための既定とアコーディオン化。

**実装**: dbchatの設定パネル整理(ENH-01/02と統合)。Select AIプロファイルのモデル切替(対応範囲で)。

**リスク/見込み**: 低。UI整理中心。ENH-01/02と同時実装が効率的。

---

## テーマB: Select AI エージェントのプラットフォーム統合（要望4）

### ENH-04: エージェント種別に「Select AI Agent」を追加（プリビルド/DBインスペクション等）

**目的**: 現在のエージェント実行(ADR-0009: SDK選択式hostedコンテナ)に**並列**で、ADBの
**Select AI Agent**(DBに常駐するエージェント。SQL/RAG/ツール実行をDB内で行う)を選べるようにする。
プリビルドエージェント(例: データベースインスペクションエージェント)もここで設定可能に。

**現状**: エージェントは hosted SDK コンテナのみ。Select AI(NL2SQL/ RAG)は単発呼び出しで使用中。
ADBは26ai。

**調査(SPIKE-E1)★種別追加の土台**:
- ADB 26aiの **Select AI Agent フレームワーク**(`DBMS_CLOUD_AI` のエージェント/ツール/タスク系API)の
  実機確認: エージェント定義・ツール登録・マルチステップ実行・会話保持がどこまで可能か。
- **プリビルド/インスペクション系エージェント**の有無と作成・実行方法(DBスキーマ理解・データ品質点検等)。
- アプリからの呼び出し経路(SQL経由 `DBMS_CLOUD_AI.GENERATE`/agent実行プロシージャ)とストリーミング可否。
- ADR-0009のエージェント抽象に「framework=select_ai」を**第4の実行種別**として無理なく足せるか。
- 成果物: `docs/verification/SPIKE-E1.md` + `docs/comparison/agent-runtimes.md`(hosted SDK 3種 vs Select AI Agentの使い分け)。
- **ゲート**: Select AI Agentが実機で有用に動くか。動けば「DBネイティブ・エージェント」として差別化価値大。

**実装(goの場合)**:
- エージェント作成画面の種別に「Select AI Agent」を追加(SDK選択と並列)。プリビルド選択・対象プロファイル設定。
- `jetuse_core/select_ai_agent.py`(DBエージェント実行のラッパ) + main.pyのエージェントrouting拡張。
- 完了条件: アプリからSelect AI Agent(+プリビルド)を作成・実行しE2E。比較ドキュメント更新。

**リスク/見込み**: 中。ADB 26aiの機能成熟度に依存。go濃厚だが実機確認必須。

---

## テーマC: RAG/検索方式の追加（要望7）

### ENH-05: RAG検索に OpenSearch(OCI Search with OpenSearch) を追加

**目的**: 現状のVector Store/Select AI with RAGに加え、**OCIマネージドのOpenSearch**による
ハイブリッド/全文+ベクトル検索を選べるようにする。

**調査(SPIKE-E2)**:
- **OCI Search with OpenSearch** の大阪可用性・クラスタ構成・コスト・IAM。
- 文書取り込み(チャンク化・埋め込み生成=Enterprise AI embeddings)→インデックス→検索の経路。
- 既存RAG抽象(`rag.py`)に「検索バックエンド=OpenSearch」を足す方式。kNN/ハイブリッド検索の品質。
- 成果物: `docs/verification/SPIKE-E2.md` + `docs/comparison/rag-backends.md`(VectorStore / Select AI RAG / OpenSearch)。
- **ゲート**: マネージドで構築でき、既存RAGと統合する価値があるか(検索品質/運用)。

**実装(goの場合)**: OpenSearchクラスタ(検証用は小構成)、取り込み・検索アダプタ、RAG UIの検索方式選択。

**リスク/見込み**: 中。マネージドだがクラスタ常設コスト。検証は最小構成で。

### ENH-06: Oracle "Trusted Answer Search" の調査と適用

**目的**: 要望の「Oracle Trusted Answer Search」を検索方式の選択肢に加える。

**調査(SPIKE-E3)★用語確定が先**:
- 「Trusted Answer Search」が指す機能の特定(候補: ADB Select AIの根拠付き回答/`narrate`、
  OCI内の信頼回答検索機能、または特定プロダクト名)。**まず正体を確定**し、OCIマネージドで使えるかを判定。
- 使える場合の統合方式・既存RAGとの差別化。
- 成果物: `docs/verification/SPIKE-E3.md`(機能特定＋go/no-go)。
- **ゲート**: マネージドで実体があり適用可能か。実体不明/不可ならA項目(未提供)として記録。

**リスク/見込み**: 不確実(用語特定次第)。調査優先。

---

## テーマD: ドキュメント理解 / OCR（要望8）

### ENH-07: OCR機能(OCI Document Understanding)の追加

**目的**: 画像/PDFからテキスト抽出(OCR)・表/キーバリュー抽出を行い、チャットやRAG取り込みに活用。

**調査(SPIKE-E4)**:
- **OCI Document Understanding** の大阪可用性、対応機能(OCR/テーブル/キーバリュー/分類)、**日本語精度**、
  同期/非同期(バッチ)API、IAM、コスト。
- ユースケース接続先: ①単発OCR画面 ②RAG取り込み前処理(スキャンPDF→テキスト) ③DBチャットの帳票取り込み。
- 成果物: `docs/verification/SPIKE-E4.md`(可用性＋日本語精度実測)。
- **ゲート**: 大阪で日本語OCRが実用精度か。不可なら代替(他リージョン/vision LLM OCR)を比較記録。

**実装(goの場合)**: `jetuse_core/docunderstand.py` + OCR画面 or RAG取り込み前処理に組込。

**リスク/見込み**: 中。マネージド。大阪可用性と日本語精度が鍵。

---

## テーマE: 音声・映像（要望5・6・9）

### ENH-08: 音声チャットUIの改善（トグルon/off分離 など）

**目的**: 音声チャット(`voicechat.tsx`)の操作性改善。読み上げon/off・自動送信・ボイス選択等の分離整理。

**調査(軽)**: 現UIの操作フロー棚卸し。「録音」「読み上げ」「自動送信(将来)」のトグル分離設計。

**実装**: voicechat UIのコントロール再配置(複雑化しない範囲)。SPIKE-06/VOICE-03の制約は踏襲。

**リスク/見込み**: 低。フロント中心。

### ENH-09: 映像分析機能のエラー修正

**目的**: `pages/video.tsx`(MM-01)の既知エラーを修正する。

**調査(軽)**: エラーの再現と原因特定(フレーム抽出 or vision呼び出し or モデル可用性)。
- まず再現手順とエラーメッセージを収集(`docs/verification/ENH-09.md`)。

**実装**: 原因に応じた修正。完了条件: 映像→フレーム抽出→分析が再度通る。

**リスク/見込み**: 低〜中。原因特定後に確定。

### ENH-10: リアルタイム文字起こし → リアルタイム翻訳化

**目的**: `realtime.tsx`/`stt_realtime.py` の文字起こし能力を流用し、**リアルタイム翻訳**機能に発展させる。

**現状**: WHISPERリアルタイムSTT(partial無し、finalのみ数秒遅れ)。文字起こし能力は流用可。

**調査(SPIKE-E5)**:
- 翻訳方式の比較(`docs/comparison/translation.md`):
  1. **OCI Enterprise AI LLM**(既存基盤・多言語・文脈考慮)
  2. **OCI Language**(翻訳専用・高速の可能性)— 大阪可用性・対応言語・レイテンシを実測
- STT(final)→翻訳→表示のパイプライン設計(言語ペア選択、原文/訳文の併記)。
- 成果物: `docs/verification/SPIKE-E5.md`(速度/品質/可用性比較)。
- **ゲート**: 体感許容のレイテンシで翻訳が成立するか。方式は実測で選定(両対応も可)。

**実装(goの場合)**: realtime画面を「文字起こし＋翻訳」へ拡張(言語選択・原文/訳文表示)、翻訳バックエンド選択。

**リスク/見込み**: 中。OCI Languageの大阪可用性とレイテンシが鍵。LLM方式は確実だが速度劣後の可能性。

---

## まとめ（着手順とゲート）

| 順 | タスク | テーマ | 調査の重さ | 見込み |
|---|---|---|---|---|
| 1 | ENH-01/02/03 DBチャット強化(CSV/スキーマ選択/UI) | A | 軽〜中 | 既存資産延長で確度高 |
| 2 | ENH-04 Select AI Agent統合 | B | 重(SPIKE-E1) | 26aiの機能次第。差別化価値大 |
| 3 | ENH-10 リアルタイム翻訳 | E | 中(SPIKE-E5) | OCI Language可用性次第 |
| 4 | ENH-07 OCR(Document Understanding) | D | 中(SPIKE-E4) | 大阪可用性・日本語精度次第 |
| 5 | ENH-05 OpenSearch RAG | C | 中(SPIKE-E2) | マネージド・常設コスト |
| 6 | ENH-06 Trusted Answer Search | C | 不確実(SPIKE-E3) | 用語特定が先 |
| - | ENH-08 音声UI / ENH-09 映像修正 | E | 軽 | 随時の軽微改修 |

**各タスクは調査(SPIKE)完了時にゲート判定をユーザーへ提示し、go/no-goの承認を得てから実装に入る。**
no-go/不可は `docs/comparison/aws-reference.md` の区分更新(プラットフォーム制約 or 使い分け確定)として記録する。
OCIサービス選択は `docs/comparison/` に比較を残す（プリセールス転用）。
