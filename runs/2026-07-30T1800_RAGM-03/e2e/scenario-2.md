# シナリオ 2: 画面でバックエンドを切り替えると能力表示が変わる

実施日時: 2026-07-30 18:28–18:29 JST（RAGM03-001/002 の修正後に 2026-07-31 01:20 JST 再取得） / 実ブラウザ（Chromium・Playwright MCP）

操作: `http://localhost:5173/#/rag` を開き、チャット欄下のバックエンド選択を
VS → ADB → SAI と切り替え、そのつど全画面スクリーンショットと
アクセシビリティスナップショット（DOM のテキスト）を取得した。

| # | 選択 | 証跡 |
|---|---|---|
| 2a | VS（Vector Store・既定） | `docs/verification/e2e-screenshots/RAGM-03-vector_store.png` / `scenario-2a-vector_store.snapshot.yml` |
| 2b | **ADB（Oracle AI Database）** | `docs/verification/e2e-screenshots/RAGM-03-adb.png` / `scenario-2b-adb.snapshot.yml` |
| 2c | SAI（Select AI） | `docs/verification/e2e-screenshots/RAGM-03-select_ai.png` / `scenario-2c-select_ai.snapshot.yml` |

> スクリーンショットは `docs/verification/e2e-screenshots/`（このリポジトリが画面証跡を置く場所）に
> 置いた。`runs/<run-id>/e2e/` はレビュー時に全文がテキストとして Codex へ渡されるため、
> バイナリを置くと入力が UTF-8 でなくなりレビューが失敗する（review-2 で実際に起きた）。
> 代わりに各状態の**アクセシビリティスナップショット（DOM のテキスト）**を e2e/ に置いてある。

## 実結果（同じ画面・選択だけが違う）

| 軸 | VS の表示 | **ADB の表示** |
|---|---|---|
| 出典の粒度 | △ 条件付き「ファイル単位。1 ファイルが複数チャンクに割れても属性は 1 種類…」 | **✓ 使える「チャンク単位。xlsx はシート名とセル範囲まで返る(実測例: 『制約』C5:E6 / 『改訂履歴』A1:C2)」** |
| 絞り込みの表現力 | △ 条件付き「属性フィルタ eq/and/or/gte…版で絞るには rag_filters を明示指定(既定は絞らない)」 | **△ 条件付き「検索は常に現行版のみ(current_version='Y')。ただしチャット API から条件を渡す口は無い」** |
| 業務データとの結合 | ✕ 使えない | **? 未実証**「1 SQL で書けるが実行結果はまだ無い」 |
| 行レベル制御 | ✕ 使えない | **? 未実証**「VPD はベクタ検索に効くことが未実証」 |
| メタデータ更新の整合性 | △ 条件付き（結果整合） | ✓ 使える（同一トランザクション） |

- 見出しも切り替わる: 「Enterprise AI マネージド Vector Store (file_search) — 手軽さ側(既定)」
  ↔「Oracle AI Database 自前索引 (DBMS_VECTOR_CHAIN) — 高機能側」。
- SAI（2c）では「絞り込み ✕ 使えない（任意メタデータは ORA-20048）」「メタ更新 ✕」に加え、
  注記に「xlsx をこの経路でどう扱うかは実機確認中(PREP-02)で未確認」が出る。
- 各バッジの `title` に根拠の所在（例 `runs/2026-07-28T1848_SPIKE-M1/e2e/SKIPPED.md 3`）が入る。
- 取り込み状況バッジ（VS/ADB/SAI/OS）とは別枠で、凡例に「あちらは取り込めたか、ここは何ができるか」
  と明記されている（スクリーンショット下部）。

- 文書ファイル欄の対応形式が「PDF / テキスト / Markdown / **Excel（.xlsx）**」になり、
  アップロードのファイル選択で xlsx を選べる（RAGM03-002 の是正。セル範囲出典の入力形式）。

**判定: PASS**（`adb` を選ぶとセル範囲つき出典が「使える」、版の扱い（常に現行版のみ）が
「条件付き」として画面に出る。未実証の 2 軸は「? 未実証」で、「使える」とは表示されない）
