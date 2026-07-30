# シナリオ 1: `GET /api/capabilities` にバックエンド別の能力が載る

実施日時: 2026-07-30 18:21–18:29 JST（RAGM03-001/002 の修正後に 2026-07-31 01:20 JST 再取得） / リージョン: ap-osaka-1

## 実行環境（配備形態は SKIPPED.md 1 を参照）

- API: `uvicorn service.main:app`（実 `.env` / `AUTH_MODE=config_file` → 実 OCI に到達。
  起動ログに `GET https://generativeai.ap-osaka-1.oci.oraclecloud.com/.../vector_stores 200 OK`）
- SPA: `npm --prefix packages/web run dev`（`/api` は上記 API へプロキシ）
- 取得コマンド: `curl -s http://localhost:5173/api/capabilities`（＝画面と同じ経路）
- 証跡: `scenario-1.capabilities.json`（応答全文）

## 期待

`rag.search` ディスクリプタに `backend_capabilities` があり、4 バックエンド × 5 軸の
能力が載っていて、**未実証の項目が実証済みと区別できる**こと。既存構造（8 能力・
`routes` / `openapi` フラグメント）が壊れていないこと。

## 実結果

既存構造: `capabilities` は 8 件のまま
（chat / rag.search / dbchat / agents / voice / minutes / translate / docunderstand）。

`rag.search.backend_capabilities.axes` =
`citation_granularity / filter_expressiveness / business_data_join / row_level_security /
metadata_update_consistency`（比較ドキュメントの 5 軸）。

| backend | 出典粒度 | 絞り込み | 業務データ結合 | 行レベル制御 | メタ更新整合 |
|---|---|---|---|---|---|
| vector_store | limited | limited | no | no | limited |
| adb | **yes** | **limited** | **unverified** | **unverified** | **yes** |
| select_ai | limited | no | no | unverified | no |
| opensearch | unverified | unverified | unverified | unverified | unverified |

`verified` は `support != "unverified"` で、`unverified` の 8 項目はすべて `verified=false`。
ADB の絞り込みは `limited`: DB 側は SQL の WHERE で絞れるが、**チャット API から条件を渡す口は無く**
常に現行版のみを検索する（RAGM03-001 の是正。表の 1 行だけを見た人が誤解しない支持レベルにした）。
各項目に `detail`（何ができる／できないか）と `evidence`（根拠の所在）が入っている。例:

```json
"row_level_security": {
  "support": "unverified", "verified": false,
  "detail": "VPD / Data Redaction は同じ表に対する DB 機能なので原理的には併用できるが、
             ベクタ検索に効くことは未実証(SPIKE-M1 の非ゴール)。…",
  "evidence": "runs/2026-07-28T1848_SPIKE-M1/e2e/SKIPPED.md 3 / ADR-0020 未解決"
}
```

**判定: PASS**（バックエンド別能力が返り、未実証が実証済みと区別できる。既存構造は不変）
