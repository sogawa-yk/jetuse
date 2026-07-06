# ステージ2 進捗キュー（stage-runner の単一の真実源）— SP2: テナンシ + Demo エンティティ

デモ生成プラットフォーム再設計（`specs/17-demo-platform-redesign.md` §1・§6 / ADR-0015）の第二ステージ＝
**SP2: テナンシ + Demo エンティティ**（Demo の一級化 + 箱のプロビジョニング + Identity Domains ユーザー分離）。
**base=`dev`**（SP2 は Internal 固有 — specs/17 §7）、ステージ統合ブランチ `feat/sp2-demo-tenancy`。
PASS したタスクを stage-runner がステージブランチへ自動 commit+merge する。push / dev への PR /
apply / IAM / Identity Domain は自走中も停止（人間ゲート）。

> **spec-driven**: SP2 の詳細仕様は specs/17 §6 に概略しかない。**SP2-00（specs/18 起草・人間承認）が
> 最初のゲート**。specs/18 は起草済み（SP2-00）で、SP2-01〜04 の受け入れ条件は specs/18 参照で
> 肉付け済み — **有効になるのは specs/18 の人間承認をもって**（承認までは SP2-01 以降を起動しない）。

status: `todo` | `in_progress` | `blocked` | `done`

| 順 | タスク | 依存 | 人間ゲート | status |
|---|---|---|---|---|
| 0 | [SP2-00 specs/18 起草（SP2 詳細仕様）+ キュー肉付け](SP2-00.md) | — | **spec 承認**（adr_approval 相当） | done |
| 1 | [SP2-01 Demo エンティティ本格化 + CRUD ルート](SP2-01.md) | SP2-00 | コミット | done |
| 2 | [SP2-02 箱のライフサイクル（lazy 解決・削除後始末・VPD 基盤）](SP2-02.md) | SP2-01 | コミット + **VPD 初回セットアップ・DBMS_LOCK 権限付与・旧 demo 資源クリーンアップ削除の承認**（specs/18 §3.2・§3.2.1・§4.3 — 承認対象と実変更の一致を完了条件で照合） | in_progress |
| 3 | [SP2-03 DemoContext 解決先の実装 + dbchat デモスコープ化](SP2-03.md) | SP2-02 | コミット | todo |
| 4 | [SP2-04 Internal テナンシ分離（Identity Domains 実接続）](SP2-04.md) | SP2-00 ＋ 人間の事前作業 | **iam_identity**（IdP 設定は人間） | blocked |

> 第0波 = SP2-00（spec 承認まで停止）。第1波 = SP2-01 ∥ SP2-04（SP2-04 は人間の事前作業が
> 未完なら blocked にして先へ）。第2波 = SP2-02。第3波 = SP2-03。

## ステージ完了条件（specs/18 §6 で確定。ステージ報告で人間が確認）

- specs/18 が人間承認済み。全タスク Codex review PASS・test/lint クリーン・実環境 E2E（または理由付き SKIPPED）通過。
- 実環境で「デモ作成（即 ready）→ 箱の lazy 生成（RAG store / datasets 表 / 会話 —
  `demo_<id>` は論理名前空間: specs/18 §3.1）→ デモスコープ操作（chat/rag/dbchat）→ デモ削除で
  specs/18 §3.2 の後始末完走（表・vector store・登録簿行・会話が消え、再 DELETE が 404）」の
  一連が確認できる。他ユーザーのデモ id は全ルート一貫して 404（fail-closed 回帰なし）。
- SP2-04: 実 Identity Domain トークンで 401/200/404 マトリクスを実機確認（SP1 の SKIPPED 解消）。
  Internal 配備設定で `AUTH_REQUIRED=true` が既定（Public/main の既定は不変）。
- `dev` が常時デプロイ可能（main 由来機能の回帰なし。既存テスト・`npm run build` 緑）。

## スコープ境界（specs/17 §1・§6）

- ビルダー（SP3）・マーケットプレイス（SP4）・`connector.invoke` は対象外。
- 統一 Capability インターフェース（案2）への移行はしない。
- Public（main 枝）の user 単位ルートの挙動・パスは変えない。

## 実行ログ（stage-runner が追記）
- 2026-07-06: SP2-00 完了（codex review-19 PASS / blocker 0。E2E は docs タスクのため理由付き SKIPPED）。
  `integrate_task.sh` で `feat/sp2-demo-tenancy` へ統合。residual 7 件（M001〜M006・N001 — 大半は
  §3=SP2-02 スコープ）は SP2-00 worktree の STATE.md に file:line 付きで記録。
- 2026-07-06: **specs/18 人間承認**（ユーザー入力「specs/18 を承認する。SP2-01 以降のキュー消化を…」）
  → SP2-01 以降のチケットが有効化。
- 2026-07-06: 第1波起動 = SP2-01（専用ペイン・in_progress）。SP2-04 は人間の事前作業
  （specs/18 §5.2: Identity Domain アプリ登録・テストユーザー2名・secret 投入）未完のため **blocked**。
- 2026-07-06: SP2-01 完了（codex review-2 PASS / blocker 0。単体 277 passed・ruff クリーン。
  実 ADB E2E: migration 017〜021 fresh/冪等/fault-injection 23 チェック + 実トークン 2 ユーザー
  CRUD 31 チェック ALL PASS）。統合後の再検証 = 280 passed・ruff クリーン。residual 4 件は
  SP2-01 worktree の STATE.md 参照（③一覧ページング契約は spec 側判断が必要 — ステージ報告で提示）。
- 2026-07-06: 第2波起動 = SP2-02（専用ペイン・in_progress）。タスク内人間ゲート
  （VPD 初回セットアップ・DBMS_LOCK EXECUTE 付与・旧 demo 資源クリーンアップ）は
  APPROVAL-REQUEST 提示で停止→人間承認後に実行の契約で起動。
