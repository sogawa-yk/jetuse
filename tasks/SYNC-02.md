# タスク: SYNC-02 main → dev 同期（抽出・OCR × 不透明ファイルキーの統合）

## 目的
**`main` の PREP-01/03/04（xlsx 抽出・スキャン OCR）と `dev` の SP2-02（不透明ファイルキー）が
同じ場所で衝突し、`ops/sync-main-to-dev.sh` が機械的には片付かない。**

2026-08-02 に同期を試みてコンフリクト 16 箇所。**11 箇所は両側の追加を残すだけ**で片付いたが、
**5 箇所は設計判断**を含むため中断した（推測で解決しない）。

## 衝突の中身（実際に確認した事実）

`main` は取り込み前に **xlsx / スキャン文書をテキストへ変換**し、マネージド側へは
`<元名>.txt` として送る。理由は実測（`docs/verification/PREP-01.md`）:

```
Unsupported file type 'xlsx' found for file-kix-…
```

→ **マネージド側は拡張子で受け付けを判断する。** 変換後は `.txt` で送る必要がある。

`dev` は SP2-02 で、OCI Files 側のファイル名を **不透明キー** `file_key(owner, rid, ext)` にした。
この `ext` は台帳（`rag_ledger`）に記録され、**2 つの用途**に使われる:

| 用途 | 参照箇所 |
|---|---|
| OCI Files の迷子ファイル照合（reconcile） | `file_key(row["owner_key"], row["id"], row["ext"])` |
| Object Storage の**原本**の削除キー | `delete_original_exact(owner, rid, ext)` |

**原本は元のバイト列**（xlsx のまま）だが、**送信するのは変換後のテキスト**。
`ext` が 1 つしかないため、どちらかが必ず壊れる:

- `ext` に `txt` を入れる → **原本（xlsx）が `.txt` という名前で保管される**
- `ext` に `xlsx` を入れる → **送信が拒否される**、または迷子ファイルを照合できない

## 決まっていること（2026-08-02 人間ゲートで承認済み。作り直さない）

**案 A: 台帳に「保管用の拡張子」と「送信用の拡張子」を別々に持つ。**

- 原本は**元の拡張子**のまま保管する（`ext`）。
- OCI Files への送信名は**送信用の拡張子**（`upload_ext`。変換したなら `txt`、していなければ `ext` と同値）。
- reconcile は `upload_ext` で照合し、原本の削除は `ext` を使う。

**案 B（変換後のテキストだけを原本として保管する）は採らない。**
変換の精度は今後上がるため、**元ファイルを捨てると後から取り込み直せない**。
台帳の項目 1 つで避けられる損失に対して代償が大きい。

## 仕様参照
- `packages/api/jetuse_core/rag.py`（`add_file` / `prepare_upload` / `derived_keys` / `_put_original`）
- `packages/api/jetuse_core/rag_ledger.py`（`reserve` / `set_external` / reconcile）
- `packages/api/jetuse_core/owner_keys.py`（`file_key` / `normalize_ext`）
- `docs/verification/PREP-01.md`（拡張子で拒否される実測）
- `ops/sync-main-to-dev.sh`（同期手順。`refactor/*` ブランチで切る）

## 対象 area
api

## 前提（依存タスク / 人間の事前作業）
- **TOOL-03 のマージ後に着手する。** 同じ `packages/api` を触るため並走させない。

## 作業内容
- `ops/sync-main-to-dev.sh` で同期ブランチを作り、コンフリクトを解消する。
- **機械的に片付く 11 箇所**（両側の追加を残すだけ。2026-08-02 に確認済み）:
  - `STATE.md` は**削除**（gitignore へ移行済み）
  - `docs/verification/jetuse-app/e2e-screenshots/*.png` は **main 側を採用**
  - `capabilities.py`: main の RAG 軸カタログと dev のデモ語彙は**別物の追加**。両方残す
  - `rag_opensearch.ingest` / テスト 4 箇所のシグネチャ: `lease` と `ocr_engine` の**両方**を受ける
- **設計判断を含む 5 箇所**（`rag.py` 4 / `service/routes/rag.py` 5）:
  - 台帳に `upload_ext` を足す（マイグレーション）。既存行は `ext` と同値で埋める。
  - `add_file` は **dev の台帳・貸出フロー**（`owner_key_gate` → `upload_gate` →
    `require_lease_for` → 予約 → `ensure_store(lease=)` → `_put_original` → `set_external`）を保ち、
    そこへ main の**抽出・OCR**（`derived_keys` の事前判定 → `prepare_upload` → 導出属性の合成）を挿す。
  - **課金される抽出（OCR）の前に、副作用なしで弾ける検証を済ませる**（`derived_keys` は OCR を呼ばない）。
    これは main 側の明示的な設計意図なので壊さない。
  - 抽出の失敗時は**予約を戻す**（外部資産はまだ無いので解放して良い）。
- 同期後、**両系統でテストを通す**。

## 完了条件（検証可能な述語で）
- [ ] コンフリクトが解消され、`dev` 側で `pytest packages/api/tests` 全件パス・`ruff` クリーン。
- [ ] 台帳に `upload_ext` があり、**既存行は `ext` と同値**で埋まっている（マイグレーションのテスト）。
- [ ] xlsx を取り込むと、**原本は `.xlsx` で保管され、送信名は `.txt`** になる（テストで固定）。
- [ ] 迷子ファイルの照合（reconcile）が**変換されたファイルでも効く**（テストで固定）。
- [ ] dev 固有機能（貸出・箱上限・デモ名前空間）の既存テストが**回帰しない**。
- [ ] main 固有機能（xlsx 抽出・OCR・セル内分割）の既存テストが**回帰しない**。
- [ ] STATE.md の `review_verdict` が PASS。

## E2E シナリオ（実環境 / jetuse-dev）
- [ ] シナリオ1: dev 構成で xlsx を取り込み、**検索でヒットし、出典にシート名・セル範囲が載る**。
- [ ] シナリオ2: 同じファイルの**原本が元の形式で取り出せる**ことを示す。
- [ ] シナリオ3（回帰）: dev 固有の貸出つき取り込みが従来どおり動くことを示す。
- [ ] 実施不能な範囲は `runs/<run-id>/e2e/SKIPPED.md` に理由を明記（無言スキップ禁止）。

## 成果物
- 同期ブランチ（`refactor/*`。`deploy-dev.yml` の自動配備を避けるため名前を変えない）
- 台帳のマイグレーション、テスト
- `docs/verification/SYNC-02.md`（**衝突の中身と、案 A を採った理由**を含む）

## 非ゴール / 禁止事項
- **案 B（原本を捨てる）を採らない**（承認済みの判断）。
- dev 固有機能を main へ持ち込まない（`dev` 全体を `main` へ merge しない）。
- ブランチ名を `refactor/*` 以外にしない（自動配備が走る）。
- **顧客名・案件名を書かない**（公開リポジトリ）。認証情報・OCID をコミットしない。
- IAM を変更しない。未承認のコミット / PR / push を行わない（人間ゲート）。
