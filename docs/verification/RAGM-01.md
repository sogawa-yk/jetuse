# RAGM-01 検証レポート: マネージド Vector Store の属性付与・構造化出典・版フィルタ

実施日: 2026-07-30 / リージョン: ap-osaka-1 / 対象: `vector_store` バックエンド（既定）
仕様の正本: `docs/decisions/ADR-0020-rag-metadata-backend.md` §1 / 実測の根拠: `docs/verification/SPIKE-M1.md` ①-a〜①-e
証跡: `runs/2026-07-30T0025_RAGM-01/e2e/`

## 結論（先に）

SPIKE-M1 で「OCI 側は属性もフィルタも対応済み・不足はアプリ側」と確定していた穴を塞いだ。
実環境（実 ADB + 実 OCI Generative AI）で次の 2 点を確認した。

1. **出典が構造化されて返る**。回答の `citations[].source` に `file` / `version` / `sheet` / `cells` /
   `sha256` / `kind` / `current_version` が構造化値として載る（本文への埋め込みではない）。
   併せて該当箇所の本文 `text` と `chunk_id` も返す。
2. **旧版を検索から外せる**。同一クエリで `current_version='Y'` の有無だけを変えると、
   フィルタ無しでは v1.0 と v2.0 の両方が引かれ、フィルタ有りでは v2.0 だけになる。

## 実装（アプリ側の穴の塞ぎ方）

| 変更 | 内容 |
|---|---|
| `jetuse_core/rag_metadata.py`（新規） | 属性とフィルタの唯一の門番。許可キー 8 種を定数化し、未知キー・上限超過・入れ子・非スカラーを `MetadataError` で拒否する |
| `jetuse_core/rag.py` | `add_file(..., attributes=...)` → `vector_stores.files.create(attributes=...)`。`file` と `sha256` は未指定なら補完。**値の無いメタはキーごと省く** |
| `jetuse_core/chat.py` | `_extract_citations()` を拡張（`source` / `text` / `chunk_id` を追加、既存 3 フィールドは温存）。`file_search_tool()` で `tools[].filters` を載せる |
| `service/schemas.py` / `service/routes/{chat,rag}.py` | `rag_filters`（チャット）と `attributes`（アップロード）を受け、境界で 422 |

### 決めたこと 1: 上限超過は切り詰めずに拒否する

属性は値 512 文字まで（SPIKE-M1 ①-d）。**切り詰めない。** `sha256` や `version` を黙って
切り詰めると値が別物になり、後段のフィルタが一致しなくなる。これは ①-b の
「存在しないキーで絞るとエラーにならず 0 件」と同じ失敗の形（利用者から見て「該当なし」と
区別が付かない）なので、入口で 422 にして気付けるようにした。

### 決めたこと 2: 空値はキーごと省く

`""` を入れると `eq` 一致が成立しなくなる。「値が無い」は**キーの不在**で表す。
ただし `0` と `False` は値なので残す（truthy 判定で落とさない — 単体テストで固定）。

### 決めたこと 3: 効かない組み合わせは黙って無視しない

`rag_filters` は `vector_store` バックエンドの `file_search` でしか効かない。
`select_ai` / `opensearch`、および**エージェントモード**（`agent` / `agent_id`。別ディスパッチで
絞り込みを渡す口が無い）と併用されたら 400 で断る。無視すると「版フィルタを掛けたのに
旧版が混ざる」という、最も避けたい壊れ方になる。エージェント経路の絞り込み対応は本タスクの
範囲外（`rag_search` ツールへ渡す口を作る設計判断が要る）。

## 実環境 E2E

共有 loop ADB（`jetuse-loop-adb`）の**タスク専用スキーマ `JETUSE_RAGM01`** で隔離し、ADB は増やしていない。
OCI 側の検証用 Vector Store は run 固有名 `jetuse-spike-ragm01-<tag>`。アプリの全経路
（FastAPI ルート → `jetuse_core` → OCI）を実 API に対して通した（モックなし）。

| # | シナリオ | 結果 | 証跡 |
|---|---|---|---|
| 1 | 版違いの架空文書 2 件を属性付きで取り込み → 版フィルタ有無の対照 | PASS | `e2e/scenario-1.md` |
| 2 | 回答の citations にセル範囲まで載る（実レスポンス） | PASS | `e2e/scenario-2.md` |
| 否定 | 未知フィルタキー / `in` / 未知属性キー / 512 文字超 / バックエンド不一致 / エージェントモード / 子が null | PASS | `e2e/guard.md` |

### シナリオ 1 の対照（同一クエリ・フィルタの有無だけが違う）

| 条件 | 引用ファイル | 引用の version |
|---|---|---|
| フィルタ無し | v1.md, v2.md | 1.0, 2.0 |
| `current_version='Y'` | v2.md | 2.0 |

フィルタ無しの回答は「v1.0 は 100 件 / v2.0 は 200 件」と両版を混ぜて答える。
フィルタ有りでは v2.0（200 件）だけを根拠に答え、旧版の引用は 1 件も返らない。

### シナリオ 2 の引用（実レスポンス）

```json
{"file_id": "file-<REDACTED>", "filename": "inventory-api-spec-v2.md", "score": 0.943,
 "source": {"file": "架空サンプル_在庫連携API仕様書.xlsx", "version": "2.0",
            "sheet": "API一覧", "cells": "B18:F18", "kind": "spec", "current_version": "Y",
            "sha256": "2f56fb…"},
 "text": "…在庫照会API GET /v1/inventory は一度に最大200件まで返却する。…",
 "chunk_id": "0_9789e265-…"}
```

既存フロントが読む `file_id` / `filename` / `score` はそのまま。拡張は追加フィールドのみ
（後方互換テスト `test_extract_citations_backward_compatible_shape`）。

### 片付け

検証用の Vector Store・ファイル・登録簿行・検証スキーマ（`JETUSE_RAGM01` / `_Q`）はすべて削除し、
**不在は `NotFoundError` で確認**した（通信断や 5xx を「消えた」と読まない。確認できなければ
台帳を残して非ゼロ終了する）。Files API 側も 1 ファイルずつ再照会している（`e2e/teardown.md`）。スキーマの DROP は `--receipt` に記録した
`USER_ID` と一致したときだけ実行する（RP-01 と同じ所有権照合）。

実施しなかった範囲と理由は `e2e/SKIPPED.md`（配備済みスタックへの再配備・属性 16 キー超過・
`in` の上流 400 再実測・属性更新 API・フロント・`adb` バックエンド）。

## 単体テスト

- `.venv/bin/pytest packages/api/tests` 442 passed（新規 `test_rag_metadata.py` 19 件 + 既存テストへの追加 16 件）
- `.venv/bin/ruff check packages/api` クリーン

守っている契約:
- 未知のフィルタキーは 422（`test_chat_stream_rejects_unknown_filter_key`）
- 空値のキーは送らない・`0`/`False` は残す（`test_normalize_attributes_*`）
- 既存 citations 形式が壊れない（`test_extract_citations_backward_compatible_shape`）
- 絞り込みが効かない経路（select_ai / opensearch / エージェント）は 400 で断る
  （`test_chat_stream_rejects_filters_without_vector_store_backend` / `test_chat_stream_rejects_filters_in_agent_mode`）
- 引用の代表チャンクは丸め前スコアで選ぶ（`test_extract_citations_compares_unrounded_scores`）
- 補完した属性が長すぎても 500 にせず 422（`test_upload_rejects_overlong_filename_with_422`）

## 環境について気付いたこと（この worktree 固有ではない）

`.env` の `COMPARTMENT_OCID` が親コンパートメント（`jetuse`）を指す一方、実リソース
（loop ADB / GenerativeAiProject）は子の `dev` にある。`PROJECT_OCID` 未設定だと
`resolve_project_ocid()` が親を探して `ProjectResolutionError` になる。E2E では
`COMPARTMENT_OCID`（=dev）と `PROJECT_OCID` を明示して実行した。`.env.example` は
`ADB_COMPARTMENT_OCID` の上書きを既に案内しているが、GenAI 側にも同じ「子コンパートメント」問題がある。
恒久的な扱い（`GENAI_COMPARTMENT_OCID` を足すか、`.env` の既定値を dev にするか）は仕様判断なので
ここでは変更せず記録に留める。

## 残る限界

- **属性はファイル単位**（SPIKE-M1 ①-a）。1 ファイルが複数チャンクに割れると全チャンクが同じ
  `cells` を返す。チャンク単位の出典が要る文書は 1 チャンク = 1 ファイルで取り込む（ADR-0020 §1 の代償）。
  チャンク単位の出典は `adb` バックエンド（RAGM-02）の担当。
- 同一ファイルの複数ヒットは**最上位スコアのチャンク**を代表として返す（引用はファイル単位に畳む既存仕様）。
- 版の付け替え運用（`current_version` を Y→N にする操作の主体とタイミング）は未仕様。
