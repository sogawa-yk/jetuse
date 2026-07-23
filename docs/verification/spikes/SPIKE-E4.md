# SPIKE-E4: OCI Document Understanding (OCR) — 大阪可用性・日本語精度

ENH-07のゲート調査。**結論: GO**（大阪可用・日本語高精度・同期APIで実用）。

## 実機検証（2026-06-16, ap-osaka-1）

スクリプト: `spikes/spike_e4_docunderstand.py`。日本語の請求書風画像（PILでNoto CJK JPから生成、
タイトル+本文+空白整列の明細表+振込先）を `analyze_document`（同期 / inline base64）で
TEXT/TABLE/KEY_VALUE の3フィーチャ一括抽出。

| 項目 | 結果 |
|---|---|
| 可用性（ap-osaka-1） | ✅ `analyze_document` がエンドポイント `document.aiservice.ap-osaka-1...` で成功 |
| レイテンシ | 1ページ **6.93s**（同期・inline） |
| 日本語文字認識 | **char-set recall 100%**、単語 mean confidence **0.994** / min 0.952 |
| テキスト抽出 | 全行を正しく抽出（漢字・かな・英数・記号・カンマ区切り数値も正確） |
| テーブル抽出 | **日本語は非対応**（言語制限。2026-06-17追検証で確定。§下記） |
| キーバリュー | 1件（`Items`）検出。汎用文書では限定的、定型帳票（請求書/領収書等）向け |

### 判明事項
- **同期API `analyze_document` + `InlineDocumentDetails(data=base64)`** が最も簡単。画像/PDFを
  そのままbase64で渡せる。多ページ/大量は非同期 `create_processor_job`（Object Storage入出力）。
- **同期APIのページ上限は5ページ（サービス側固定）**。実測で 3/5ページ=OK（5ページ5.9s）、
  6ページ以上は **HTTP 413 `Input file has too many pages, maximum number of pages allowed is: 5`**。
  → **ENH-07bで「5ページ単位に分割→各チャンク同期OCR→マージ」のワークアラウンドを実装**し、
  5ページ超のPDFも透過対応（12ページPDF=3チャンクで全ページ抽出を実機確認）。非同期 processor job
  を採らなかった理由・実装詳細・制限一覧は **`docs/guides/ocr-limits-and-workarounds.md`** に集約。
- **テーブル抽出は日本語非対応（英語系のみ）**。2026-06-17の対照実験（同一罫線表で内容のみ変更）で、
  英語=6×4を正確に構造抽出／日本語(JPN・自動)=0件／日本語をENG強制=表は出るがセル文字化け、と確定。
  TEXT抽出は日本語高精度なため、日本語文書では「表が文字起こしになる」挙動になる。
  回避策（LLMで表へ再構成 等）は `docs/guides/ocr-limits-and-workarounds.md` §1b を参照。
- 言語コードは ISO 639-2/B 系（`JPN`/`ENG`/`CHI_SIM`/`KOR`/`FRE`/`GER`/`SPA` 等）。
- フィーチャ: TEXT / TABLE / KEY_VALUE / 要素抽出 / 分類 / 言語分類。プリトレ済みモデルで
  追加学習なしに利用可。

### IAM（要・人間承認）
ローカル（`~/.oci` ユーザー認証）では成功。**本番CI（リソースプリンシパル）には未付与だと
404 NotAuthorizedOrNotFound** になる見込み（翻訳ENH-10と同型）。必要ポリシー:

```
allow dynamic-group jetuse-dg to use ai-service-document-family in compartment jetuse-proto
```

`docunderstand.ocr()` は 401/404 を捕捉して「IAM未整備の可能性」の日本語メッセージ（422）へ
変換するため、未付与でもアプリは安全に失敗（500は出さない）。

## 実装（ENH-07）
- `packages/api/jetuse_core/docunderstand.py`: `ocr(content, *, language, tables, key_values)`。
  RP/ユーザー認証両対応。`OcrError` で失敗を表現。inline同期、上限8MB。
- API: `GET /api/ocr/options`（言語一覧）、`POST /api/ocr`（multipartアップロード）。
- UI: `pages/ocr.tsx`（サイドバー「OCR / 文書認識」）。ファイル選択・言語・表/KVトグル・
  抽出テキスト/表/KV表示・コピー・「このテキストでチャット」（sessionStorage経由でchatへプリフィル）。
- テスト: `tests/test_docunderstand.py`（整形・空/過大・404→友好エラー）4件。

## 代替（go不要だったため記録のみ）
日本語OCRは十分実用精度のため代替検討は不要。大量バッチが必要になった場合のみ非同期
processor job（または vision LLM OCR との比較）を別途検討する。
