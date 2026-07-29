# タスク: RAGM-01 マネージド Vector Store に属性付与・構造化出典・版フィルタ

## 目的
既定バックエンド（Enterprise AI マネージド Vector Store）の RAG で、
**出典を構造化して返す**・**旧版を検索から外す**を成立させる。
SPIKE-M1 で「OCI 側は属性もフィルタも対応済み・不足はアプリ側の実装」と確定したので、その穴を塞ぐ。

## 仕様参照
- `docs/decisions/ADR-0019-rag-metadata-backend.md` §1（Accepted）
- 実測の根拠: `docs/verification/SPIKE-M1.md` ①-a〜①-e

## 対象 area
api

## 前提（依存タスク / 人間の事前作業）
- 依存タスクなし（ADR-0019 承認済み）。
- SPIKE-M1 の実測どおり、属性はキー最大 16・値 512 文字・文字列/数値/真偽のみ・入れ子不可。

## 作業内容
- `jetuse_core/rag.py` の取り込みで `vector_stores.files.create(..., attributes={...})` を付ける。
  キーは `file` / `version` / `sheet` / `cells` / `sha256` / `kind` / `current_version` / `chunk_id`。
  値が無いメタは**キーごと省く**（空文字を入れない。フィルタが静かに効かなくなるため）。
- `jetuse_core/chat.py:_extract_citations()` を拡張し、`file_search_call.results[].attributes` と
  `.text` を構造化引用として返す。**既存の `{file_id, filename, score}` は残す**（後方互換）。
  拡張は `source: {file, version, sheet, cells, ...}` のような追加フィールドで行う。
- 検索時に `tools[].filters` を渡せるようにし、`current_version='Y'` を掛けられるようにする。
- **フィルタキーのタイポ検知**: SPIKE-M1 で「存在しないキーで絞ると**エラーにならず 0 件**」を
  実測した。アプリ側で許可キーを定数化し、未知キーは 422 で弾く（テストで守る）。
- 属性の上限（16 キー / 値 512 文字）を超える入力の切り詰めか拒否をどちらかに決めて実装する。

## 完了条件（検証可能な述語で）
- [ ] 属性付きで取り込んだファイルに対し、`/api/chat/stream` の citations に
      `file` / `version` / `sheet` / `cells` が構造化された値として載る（本文埋め込みでない）。
- [ ] `current_version='Y'` を掛けた検索で、旧版として登録したファイルが 1 件も返らない。
      フィルタ無しでは返ることを対照として同一クエリで示す。
- [ ] 既存の citations 形式（`{file_id, filename, score}`）を読む既存フロントが壊れない
      （後方互換のテストがある）。
- [ ] 未知のフィルタキーが 422 になる単体テストがある。
- [ ] `.venv/bin/pytest packages/api/tests` 全緑・`.venv/bin/ruff check packages/api` クリーン。

## E2E シナリオ（実環境 / jetuse-dev）
- [ ] シナリオ1: 版違いの架空文書 2 件を取り込み → 版フィルタ有り/無しで結果が変わることを実 API で示す。
- [ ] シナリオ2: 回答の citations に セル範囲まで載ることを実レスポンスで示す。
- [ ] 実施不能な範囲は `runs/<run-id>/e2e/SKIPPED.md` に理由を明記。

## 成果物
- `packages/api/jetuse_core/rag.py` / `chat.py` の変更、テスト
- `docs/verification/RAGM-01.md`

## 非ゴール / 禁止事項
- `adb` バックエンドの実装（RAGM-02）。
- 能力差の UI 表示（RAGM-03）。
- 顧客データの持ち込み（架空データのみ）。認証情報・OCID のコミット。
