# タスク: SYNC-01 main → dev 同期（RAGM-01 / RAGM-02 と SP2-02 の統合）

## 目的
`main`（RAGM-01・RAGM-02 が入った状態）を `dev` へ取り込む。
通常の同期は機械的だが、**今回は RAG の中核コードで dev 側 SP2-02 と main 側 RAGM が
同じ関数を作り替えており、19 箇所・約 850 行が衝突する**。片側を機械的に採ると
どちらかの機能が壊れるため、**テストとレビューを回しながら統合する**。

## 仕様参照
- `CLAUDE.md`「開発方式」: Public 変更は main へ入れ、直後に main → dev で同期する
- `docs/guides/branching-and-releases.md`（同期の正本）
- dev 側の意図: `specs/18-sp2-demo-tenancy.md`（デモ単位のデータ分離。**信頼境界**）
- main 側の意図: `docs/decisions/ADR-0020-rag-metadata-backend.md`（RAGM-01 §1 / RAGM-02 §2）

## 対象 area
api

## 前提（依存タスク / 人間の事前作業）
- ブランチ `feat/SYNC-01` は **`origin/dev` から**切ってある（このタスクだけ base が dev。
  同期の性質上、dev を土台に main を取り込むため）。
- 取り込む相手は `origin/main`（`96e7311` 時点。+5 commit）。

## 衝突箇所（事前調査済み）
| ファイル | 箇所 | 行数 | 性質 |
|---|---:|---:|---|
| `packages/api/jetuse_core/rag.py` | 8 | 250 | **最重要**。dev が `_insert_file` を台帳付き `_insert_file_confirmed` に作り替え、main が `_fit()`（バイト境界の切り詰め）と `_delete_row()`（ADB チャンクを同一トランザクションで削除）を追加。同じ関数の中で絡む |
| `packages/api/service/routes/rag.py` | 5 | 66 | |
| `packages/api/service/routes/chat.py` | 1 | 33 | ガード群とディスパッチ |
| `packages/api/service/schemas.py` | 1 | 5 | `rag_backend` の Literal と `rag_filters` |
| `packages/api/tests/test_rag.py` | 1 | 412 | |
| `packages/api/tests/test_rag_add_file_retry.py` | 1 | 73 | |
| `packages/api/tests/test_demo_routes.py` | 2 | 10 | |

## 作業内容
- `git merge origin/main` を実行し、上記の衝突を**1つずつ中身を読んで統合**する。
- **`--ours` / `--theirs` で機械的に片側を採らない。** 双方が別の機能を足している箇所は
  両方を残す。同じ関数を作り替えている箇所は、**両方の意図が成立する形**に書く。
  - 例（既知）: dev の `_insert_file_confirmed`（台帳 confirmed を同一トランザクションで確定）は
    main の `_fit()`（日本語ファイル名で ORA-12899 を避けるバイト境界切り）を**使うべき**。
    文字数切り（`filename[:MAX_FILENAME_CHARS]`）に戻さないこと。
  - 例（既知）: main の `_delete_row()` は ADB チャンクを台帳と同一トランザクションで消す。
    dev の削除経路（デモ箱の後始末）と**両立させる**。可用性チェックを削除のスキップ条件にしない。
- 統合後、**dev 側の機能（SP2-02 のデモ単位分離）と main 側の機能（RAGM-01 の属性・版フィルタ、
  RAGM-02 の adb バックエンド）が両方生きていること**を、既存テストで確認する。
- 既知の構造的乖離（`CLAUDE.md` の sp3_03_scaffold 行 / `.gitignore` の runs/ / `docs/archive/README`）は
  dev 版が正。ただし main が同じ箇所を本当に変えていないか diff で確認してから採る。

## 完了条件（検証可能な述語で）
- [ ] コンフリクトマーカーが 1 つも残っていない。
- [ ] `.venv/bin/pytest packages/api/tests` **全件パス**（dev 側・main 側どちらのテストも落ちない）。
- [ ] `.venv/bin/ruff check packages/api` クリーン。
- [ ] `dev` 固有ファイルの残存を個別に確認（`specs/18-sp2-demo-tenancy.md` /
      `specs/19-sp3-builder.md` / `packages/api/service/routes/builder.py` /
      `packages/web/src/pages/demobuilder/index.tsx`）。
- [ ] `main` 側の新規ファイルが取り込まれていること（`jetuse_core/rag_adb.py` /
      `jetuse_core/rag_metadata.py` / `migrations/017〜019`）。
- [ ] **統合の判断を `docs/verification/SYNC-01.md` に記録**する。衝突ごとに
      「dev はこうしたかった / main はこうしたかった / どう統合したか」を1〜2行で。
- [ ] STATE.md の `review_verdict` が PASS。

## E2E シナリオ（実環境 / jetuse-dev）
本タスクはマージであり新機能を足さない。**実環境 E2E は最小限**でよいが、
統合で壊れやすい箇所を実機で1本は通すこと。

- [ ] シナリオ1: 実 ADB に対しマイグレーションを適用（`jetuse_core.migrate`）し、
      main 側の 017〜019 が dev のマイグレーション列に矛盾なく載ることを示す。
- [ ] 実施不能な範囲は `runs/<run-id>/e2e/SKIPPED.md` に理由を明記（無言スキップ禁止）。

## 成果物
- 統合済みの `feat/SYNC-01`
- `docs/verification/SYNC-01.md`（衝突ごとの統合判断）

## 非ゴール / 禁止事項
- **機能追加・リファクタをしない**。目的は統合であり、ついでの改善を混ぜない
  （混ぜると「同期で何が変わったか」が読めなくなる）。
- dev 固有機能を落とさない。main 側の新機能も落とさない。**どちらかを削るのは解決ではない**。
- 既知の重複ガード（`agent and rag cannot be combined` が 2 箇所）は**この タスクでは触らない**
  （別途整理する。マージに無関係な変更を混ぜない）。
- 顧客名・案件名を書かない。認証情報・OCID・エンドポイント実値をコミットしない。
- 未承認のコミット / PR / push を行わない（人間ゲート）。
  **特に push 先のブランチ名に注意**: `feat/*` へ push すると dev 環境へ自動配備が走る。
  push はオーケストレータが `refactor/sync-main-dev` 名で行うので、**このループは push しない**。
