# シナリオ2: ADB 未取り込みのファイルを選んでも、ADB では送信可にならない

実施: 2026-07-31 09:56–09:58 JST / 実ブラウザ（Chromium・Playwright MCP）
環境はシナリオ1 と同じ（コンパートメント `jetuse` / 実 API / 実 ADB の専用スキーマ `JETUSE_RAGM04_C5F34C`）。
画面: `http://localhost:5173/#/rag`（Vite dev server。`/api` は シナリオ1 と同じ実 API へプロキシ）

## 作った状態（実 DB・実 ADB）

| ファイル | マネージド（VS） | ADB | 作り方 |
|---|---|---|---|
| `jetuse-spike-ragm04-未取り込み.md` | `indexed`（台帳 `status='completed'`） | **`pending`** | 台帳行のみ |
| `jetuse-spike-ragm04-ADB取り込み済み.md` | `indexed` | **`indexed`** | `rag_adb.ingest(kind="spec")` を実 ADB へ（1 チャンク・実埋め込み） |

`/api/rag/files` の実応答（`rag.attach_backend_status` が実 DB / 実 ADB を見て組んだもの）:

```
ragm04-09dbac8e ADB取り込み済み.md completed {'vector_store': 'indexed', 'select_ai': 'pending', 'opensearch': 'disabled', 'adb': 'indexed'}
ragm04-6413029f 未取り込み.md     completed {'vector_store': 'indexed', 'select_ai': 'pending', 'opensearch': 'disabled', 'adb': 'pending'}
```

マネージド側の状態は台帳行を直接入れて作った（アップロード経路は project 不在で使えない — `SKIPPED.md` 1）。
**判定に効く側（ADB の取り込み状態）は実物**で、`indexed` は実際にチャンクが入っている行から来ている。

## 実結果（同じ画面・選択だけが違う）

| # | 一覧の状態 | 選択 | 入力欄 | 証跡 |
|---|---|---|---|---|
| 2a | ✓VS / ⏳ADB（1 件） | VS | **有効**（`文書について質問（Shift+Enterで改行）`） | `RAGM-04-2a-vs-sendable.png` |
| 2b | ✓VS / ⏳ADB（1 件） | **ADB** | **無効**（`文書をアップロードすると質問できます（取り込み完了後）`）・送信ボタン disabled | `RAGM-04-2b-adb-not-sendable.png` / `snapshot-2b-adb.yml` |
| 2c | ADB 取り込み済みを 1 件足した後 | ADB | **有効** | `RAGM-04-2c-adb-sendable.png` / `snapshot-2c-adb.yml` |
| 2d | 同上（SAI はどちらも `pending`） | SAI | **無効** | `snapshot-2d-select_ai.yml` |

スクリーンショットは `docs/verification/e2e-screenshots/`（このリポジトリが画面証跡を置く場所）。
`runs/<run-id>/e2e/` はレビュー時に全文がテキストで Codex へ渡るためバイナリを置かない
（RAGM-03 review-2 で実際にレビューが失敗した）。代わりに各状態の
アクセシビリティスナップショット（DOM のテキスト）を `snapshot-*.yml` として置いてある。

**判定: PASS**
- 2b が修正前の不具合そのもの（RAGM03-005）。マネージド側が `completed` でも、
  **ADB を選んでいる間は送信可にならない**。
- 2c で「ADB に取り込めたファイルが 1 件でもあれば送信可になる」ことを確認（常時無効ではない）。
- 2d は同じ判定が SAI にも効いていること（判定根拠が選択中のバックエンドであること）の対照。
- 取り込み状況バッジ（✓VS / ⏳ADB / ⏳SAI / –OS）と能力表示パネルは 2a〜2d を通して
  従来どおりの意味・表示のまま（新しい概念は増やしていない）。
