# シナリオ1: 同一の不正な `kind` は、どのバックエンドを選んでも 422

実施: 2026-07-31 JST / リージョン ap-osaka-1 /
コンパートメント **`jetuse`**（`.env` の `COMPARTMENT_OCID` が指す実体。`CLAUDE.md` が
「jetuse-proto」、`loop-config.yml` が「jetuse-dev」と呼んでいるのはこの 1 つのコンパートメント。
表示名は `env.log` の `IdentityClient.get_compartment(...).name` で確認した）。

## 実行環境

- API: `uvicorn service.main:app --port 8011`（実 `.env`・`AUTH_MODE=config_file` → 実 OCI に到達）
- DB: 共有 loop ADB（`jetuse-loop-adb` / DSN `jetuseloop2_low`）の**このタスク専用スキーマ**。
  `spikes/ragm02/setup_schema.py` を `SPIKE_SCHEMA_PREFIX=JETUSE_RAGM04` で再利用（ADB は増やしていない）。
  - `JETUSE_RAGM04_C5F34C` … シナリオ2 で使用（DROP 済み）
  - `JETUSE_RAGM04_F87BE4` … 本シナリオの生ログ取得で使用（DROP 済み。`teardown.md`）
- deploy 相当: `python -m jetuse_core.migrate` を実スキーマへ適用（`deploy.log`。019 まで）
- 実行と証跡: `scenario-1.raw.log`（リクエスト / HTTP status / **レスポンス本文の全文**）、
  `scenario-1.adb.log`（ADB 側の正常系）、`env.log`（コンパートメント名・project 解決状況）

## 期待

`kind` に数値・真偽を渡したら、**取り込み・検索とも 422**。片方だけ通ったり、
黙って文字列化されたりしないこと。長さ上限（32 バイト）は据え置きで、超過は 422。

## 実結果（全文は `scenario-1.raw.log`）

### 1a. 取り込み（`POST /api/rag/files`。1 リクエストで両バックエンドへ入る口）

| `attributes` | HTTP | `detail` の要点 |
|---|---|---|
| `{"kind": 1}` | **422** | `attribute 'kind': value must be a string (numbers and booleans are not accepted…)` |
| `{"kind": 0}` | **422** | 同上 |
| `{"kind": 1.5}` | **422** | 同上 |
| `{"kind": true}` | **422** | 同上 |
| `{"kind": "分類"×6}`（36 バイト） | **422** | `value must be at most 32 bytes in UTF-8` |
| `{"kind": "spec"}` | 503 | `GenerativeAI project を解決できません…` |

最後の 503 は `kind` と無関係の環境要因で、**文字列は検証を通過している**ことの裏返し
（型で弾かれていれば 422 で止まり、project 解決まで進まない）。このコンパートメントには
ACTIVE な GenerativeAI project が 0 件で、マネージド側のアップロード経路自体が使えない
（`env.log` / `SKIPPED.md` 1）。

### 1b. 検索フィルタ（`rag_backend` を変えて同じ不正値を投げる）

| `rag_backend` | `rag_filters.value` | HTTP |
|---|---|---|
| vector_store / adb / select_ai / opensearch | `1`（数値） | **422**（4 つとも同一メッセージ） |
| vector_store | `"spec"`（文字列） | 400 `no documents uploaded` |
| adb | `"spec"`（文字列） | 400 `rag_filters is not supported on the adb backend` |

不正な型はバックエンド別の 400 より**先に**弾かれる＝**同じ入力に同じ応答**。
文字列にした途端、そこから先はバックエンドごとの既存の扱い（RAGM-01/03 で決めたもの）に戻る。

### 1c. ADB 側の正常系（同じ値が文字列で入る。`scenario-1.adb.log`）

```
$ rag_adb.ingest(..., kind="spec")   # 実 ADB・実埋め込み
chunks: 1
$ SELECT doc_file, kind, COUNT(*) FROM rag_adb_chunks ...
('jetuse-spike-ragm04-ADB取り込み済み.md', 'spec', 1)
```

**判定: PASS**
- 不正な `kind`（`1` / `0` / `1.5` / `true`）は取り込みで 422（1a）。
- 同じ不正値を検索フィルタに載せると、4 バックエンドのどれでも 422（1b）。
- 32 バイト超は従来どおり 422 で、上限は広げていない。
- 文字列 `kind` は ADB の `VARCHAR2(32)` 列へそのまま入る（1c）。
