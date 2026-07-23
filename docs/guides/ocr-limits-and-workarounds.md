# OCI Document Understanding（OCR）の制限とワークアラウンド

ENH-07で実装したOCR機能（`packages/api/jetuse_core/docunderstand.py`）が依存する
OCI Document Understanding **同期API（`analyze_document` / inline）** の制限と、本プロジェクトでの
回避方法をまとめる。すべて ap-osaka-1 での実機検証（SPIKE-E4, 2026-06-16）に基づく。

## 1. 制限の一覧（実測）

| 制限 | 値 | 確認方法 / 症状 | 出典 |
|---|---|---|---|
| **1回あたりの最大ページ数** | **5ページ** | 6ページ以上で `HTTP 413: Input file has too many pages, maximum number of pages allowed is: 5`。3/5ページ=OK | SPIKE-E4実測 |
| inlineペイロードサイズ | 〜8MB目安 | base64でリクエストボディに載るため過大だと失敗 | 経験則 |
| IAM | `use ai-service-document-family` 必須 | 未付与のCI（RP）は `404 NotAuthorizedOrNotFound`（ローカルのユーザー認証では成功するため気づきにくい） | SPIKE-E4実測 |
| 同期 vs 非同期 | 同期=少ページ即時 / 非同期=大量ページ | 大量・多ページは `create_processor_job`（Object Storage入出力）が本来の手段 | OCI公式 |
| テーブル抽出 | **英語系のみ・罫線ベース** | **日本語文書ではテーブル構造を返さない**（テキストのみ）。英語の同一表は6×4で正確に抽出。罫線なしの表も非対応 | SPIKE-E4実測（2026-06-17） |
| 言語コード | ISO 639-2/B系 | `JPN`/`ENG`/`CHI_SIM`/`KOR`/`FRE`/`GER`/`SPA` 等。`ja`等のISO639-1ではない | SDK |

レイテンシ実測（同期・1呼び出し）: 1ページ 6.9s / 5ページ 5.9s / 3ページ 3.4s。

## 2. 採用したワークアラウンド：5ページ超は「分割→同期OCR→マージ」（ENH-07b）

### 方針
5ページ超のPDFに対し、**非同期 Processor Job ではなく、サーバー側でPDFを5ページ以下に
分割し、各チャンクを同期APIでOCRして結果をマージする**方式を採用した。

### 非同期 Processor Job を採らなかった理由
| 観点 | 分割＋同期（採用） | 非同期 Processor Job |
|---|---|---|
| 追加インフラ | 不要 | **入力/出力 Object Storage バケットが必要** |
| 追加IAM | 不要（document-familyのみ） | バケットへの `manage object-family` 等が追加で必要 |
| 実装 | 既存同期コードを再利用＋pypdfで分割 | ジョブ作成→**ポーリング**→出力JSONダウンロード→パース |
| レイテンシ | チャンク数×同期レイテンシ（直列）。中規模まで実用 | ジョブのスケジューリング待ちが入る |
| 適性 | 〜100ページ程度の単発OCR（プロトタイプ用途に十分） | 数百〜数千ページの大量バッチ |

プロトタイプの「単発OCR画面」用途では分割方式が十分かつ最小コスト。大量バッチが要件化した
場合に限り非同期方式を追加検討する（その際は本ドキュメントに追記）。

### 実装（`docunderstand.py`）
```
ocr(content, language, tables, key_values)
  ├ 全体ガード: MAX_TOTAL_BYTES(60MB) / MAX_TOTAL_PAGES(100ページ)
  ├ PDF かつ ページ数 > MAX_SYNC_PAGES(5) なら pypdf で5ページ単位に分割
  │    _split_pdf(): PdfReader/PdfWriter で元ページを保持したままチャンクPDFを生成
  └ 各チャンクを _analyze_chunk() で同期OCR → lines/confidences/tables/key_values をマージ
       mean_confidence は全チャンクの単語信頼度を合算して再計算
返り値に chunk_count を含め、UIは「Nチャンクに分割して処理」と表示
```
- 画像（PNG/JPEG/TIFF）や5ページ以下のPDFは**分割せず単発**で処理（オーバーヘッドなし）。
- 分割は元PDFのページをそのままコピーするため**ラスタライズによる劣化なし**。
- 壊れたPDF（ページ数取得失敗）は単発でOCIに委ね、最終的な413等は友好的メッセージへ変換。

### 実機確認（2026-06-16）
12ページPDF（202KB）→ **3チャンク（5/5/2）に自動分割 → 全12ページ抽出（カバレッジ12/12）**、
mean_confidence 0.9906、所要 12.2s。ページ順も保持。

## 1b. テーブル抽出は日本語非対応（サービス側の言語制限）

「PDFを投げると表ではなく文字起こしになる」現象の原因。**精度のばらつきではなく言語サポートの
制限**。同一の罫線表で内容（言語）だけ変えた対照実験（2026-06-17、A4・150dpi相当）:

| 表の内容 | `language` | テーブル検出 | 備考 |
|---|---|---|---|
| 英語 | `ENG` / 自動 | ✅ 1件（6行×4列・全セル正確） | 構造抽出が機能 |
| **日本語** | **`JPN`** | ❌ **0件** | テキスト抽出のみ。表として返らない |
| 日本語 | 自動(`None`) | ❌ 0件 | 同上 |
| 日本語 | `ENG`（無理やり） | ⚠ 1件 | 構造は出るが**セル文字が文字化け**（日本語TEXTが効かない） |

結論: **TEXT抽出は日本語で高精度に動くが、TABLE構造抽出は英語系のみ**。日本語文書では
OCIが表を返さないため、UIには本文テキストだけが出る（＝「文字起こし」に見える）。罫線の
有無は二次的要因で、根本は言語サポート。

### 採用方針（2026-06-17、ユーザー判断）
**ネイティブ機能でできる範囲にUIを合わせる**。表抽出は英語のみ対応のため:
- **「表を抽出」チェックボックスは language=ENG のときだけ表示**（非英語では非表示にし、
  `tables=false` で送る）。非英語時は「表抽出は英語選択時のみ対応」と明記。
- ネイティブ抽出の品質バグ（OCIはヘッダーを `header_rows` で返すのにアプリが `body_rows`
  しか読まず**ヘッダー行が欠落**していた）を修正。`header_rows + body_rows + footer_rows` を
  順に展開するようにした（GAN論文 Table 1 でヘッダー `Model/MNIST/TFD` + 全行の復元を実機確認）。
- 残る癖: `±` が `+` に化ける（OCIの文字認識側。アプリでは直さない）。

#### VLMエンジンを選択式で追加（ENH-07g、2026-06-17）
日本語の表抽出ニーズに対し、**OCRエンジンを選択式**にした（UIのエンジンプルダウン）:
- `document_understanding`（既定）: OCI Document Understanding。日本語テキスト高精度・高速。
  表は英語のみ。キー/値抽出あり。
- `vlm`: OCI Generative AI のビジョンLLM（gemini-2.5-pro / flash 選択可）。ページ画像を
  LLMで読み、**日本語の表も抽出**。`text`＋`tables`（構造化）を返す。
  - 実装: `docunderstand.ocr_vlm()`。PDFは pymupdf でページ画像化→ページ毎にLLM（並列）。
    JSON（`{text, tables}`）で受けて構造化、UIは既存の表描画を流用。
  - 実機確認（2026-06-17, ゲートウェイ経由）: 日本語の罫線表PDF → HTTP 200/13秒、
    ヘッダー＋全行を正確に抽出（`±`等の記号も保持）。
  - トレードオフ: ページ毎にLLM呼び出し（コスト・時間増）、生成のため厳密OCRではない。
    UIに注記表示。`MAX_TOTAL_PAGES=100` のガードあり。

### リージョン差の検証（2026-06-17）
同一の表を **Osaka / Phoenix / Chicago / Ashburn** に投げた結果、**英語表は全リージョンで検出・
日本語表は全リージョンで0件**。テーブル抽出モデルはリージョン共通で、リージョン変更では
日本語非対応は解決しない（実機確定）。

## 2b. 多ページ時の HTTP 504（API Gateway タイムアウト）と対策（ENH-07d）

分割対応後、5ページ超のPDFで **HTTP 504** が発生した。原因は2つの合わせ技：
1. OCRは結果が出るまで**何も返さない同期ブロッキングPOST**（チャットのようなSSE keepaliveが無い）。
2. 汎用ルート `/api/{p*}` の **API Gateway `read_timeout` が60秒**（SSEのチャット専用ルートだけ300秒）。
   チャンクを**直列**処理すると総時間が60秒を超え、ゲートウェイが504を返していた。

対策（両方実施）:
- **チャンクを並列OCR**（`OCR_CONCURRENCY=5` の `ThreadPoolExecutor`。ページ順は維持）。
  → 壁時計時間を短縮（実機: 14ページ=3チャンクで **10.4秒**／20ページ=4チャンクで21秒）。
- **`/api/ocr` 専用ルートを追加し `read_timeout=300`**（チャットSSEと同じ上限）。
  完全一致ルートが必須（`{p*}` は末尾セグメント無しの `/api/ocr` に一致しない）。
  - 注: API Gatewayの **HTTP_BACKEND は connect/send timeout が >=1 必須**。ルート挿入で
    状態インデックスがFunctionsルート（0固定）とずれて400になったため、HTTP_BACKENDでは
    connect=60 / send=300 を明示している（`modules/api-gateway/main.tf`）。

実機確認（2026-06-17, ゲートウェイ経由・実トークン）: 14ページPDF → **HTTP 200 / 10.4秒**、
3チャンク、全14ページ抽出、mean_confidence 0.987。504は解消。

## 3. エラーの友好変換（ユーザーに生のORA/HTTPを見せない）
`docunderstand.ocr()` / `_analyze_chunk()` は以下を日本語メッセージ（API側は422）へ変換する。
- `401/404 NotAuthorizedOrNotFound` → 「OCRサービスにアクセスできません（IAM未整備の可能性）。
  管理者に `use ai-service-document-family` 権限の付与を依頼してください。」
- `413 / too many pages`（通常は事前分割で到達しない保険）→ 「最大5ページ。分割してください。」
- 分割後チャンクが8MB超 → 「解像度を下げるかページ数を減らしてください。」
- 総ページ数 > 100 → 「分割してアップロードしてください。」

## 4. 必要IAM（人間承認が必要）
```
allow dynamic-group jetuse-dg to use ai-service-document-family in compartment jetuse-proto
```
未付与でもアプリは500を出さず、上記の友好的422で安全に失敗する。詳細は `docs/setup/iam.md`。

## 5. 関連
- 調査レポート: `docs/verification/spikes/SPIKE-E4.md`
- Tips: `docs/tips.md`（2026-06-16 の Document Understanding 関連エントリ）
- 実装: `packages/api/jetuse_core/docunderstand.py` / `service/main.py`（`/api/ocr`） / `packages/web/src/pages/ocr.tsx`
- 公式: https://docs.oracle.com/en-us/iaas/Content/document-understanding/using/about_doc_understanding.htm
