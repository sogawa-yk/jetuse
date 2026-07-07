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
| 2 | [SP2-02 箱のライフサイクル（lazy 解決・削除後始末・VPD 基盤）](SP2-02.md) | SP2-01 | コミット + **VPD 初回セットアップ・DBMS_LOCK 権限付与・旧 demo 資源クリーンアップ削除の承認**（specs/18 §3.2・§3.2.1・§4.3 — 承認対象と実変更の一致を完了条件で照合） | done |
| 3 | [SP2-03 DemoContext 解決先の実装 + dbchat デモスコープ化](SP2-03.md) | SP2-02 | コミット | done |
| 4 | [SP2-04 Internal テナンシ分離（Identity Domains 実接続）](SP2-04.md) | SP2-00 ＋ 人間の事前作業 | **iam_identity**（IdP 設定は人間） | in_progress |

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
- 2026-07-06: SP2-02 完了（codex review-15 PASS / blocker 0 / major 6・minor 1 は residual。
  実 ADB+実 GenAI E2E 104 チェック（JETUSE_SP2_02 隔離・DBMS_LOCK/DBMS_RLS/VPD 実機可否 12/12）。
  統合後の再検証 = 363 passed・ruff クリーン。**Gate 1〜3（共有スキーマへの VPD 権限付与・
  DBMS_LOCK EXECUTE 付与・旧 demo 資源クリーンアップ）は人間承認待ち・未実行** —
  APPROVAL-REQUEST.md 提示済み（runs/2026-07-06T1751_SP2-02/e2e/）。承認・実行・APPROVAL.md 証跡は
  ステージ報告の残ゲートとして提示。residual M001〜M006/N001 は SP2-02 worktree の STATE.md 参照。
- 2026-07-06: 第3波起動 = SP2-03（専用ペイン・in_progress）。
- 2026-07-07: 人間承認3件を反映 — (a) SP2-02 Gate 1〜3 全承認（Gate 2=最小案）→ 実行（Gate 2/3 実施・
  review-19 PASS。Gate 1 は稼働アプリスキーマ不在のためデプロイ環境へ繰延。証跡 e2e/APPROVAL.md）。
  Gate 2 のコード変更（vpd/demo_lease/setup-vpd/bootstrap）を再統合（秘匿値なし手動確認 — ガードは
  検証レポート内の変数名言及に反応した誤検知）。(b) SP2-03 の残 blocker B001（共有 ADB 閉じた実験の
  事後承認）/ B002（ADR-0022 を選択肢 B+C で Accepted）を承認 → C の段階的硬化実装 → codex review-15 PASS。
  SP2-03 統合（コンフリクトなし）。統合後の再検証 = api 521 passed + jetuse_shared 28 passed・ruff クリーン
  （jetuse_shared/webtools.py の既存 E501 2件は dev と同一・SP2-03 非由来）。
- 2026-07-07: SP2-01〜03 統合完了。残ゲート = SP2-04（IdP 人間事前作業待ち・blocked）+ push/dev への PR
  （人間）+ SP2-02 Gate 1 のデプロイ環境実施。ステージ報告書 runs/_stages/sp2-demo-tenancy/REPORT.md へ。
- 2026-07-07: SP2-04 の人間事前作業を施主承認のもとエージェントが実施（jetuse/dev 配下は権限あり）。
  永続 Identity Domain `jetuse-dev-idp`（Free/ACTIVE、jetuse/dev、home=osaka・CRUD は home region IAD 経由）+
  ROPC confidential app `jetuse-dev-api` + テストユーザー2名を作成。実トークンで iss/aud/sub 実測済み。
  SP2-04 を blocked → in_progress（第4波起動）。実測 OIDC 値・秘匿値・token ヘルパーは
  セッション scratchpad（非コミット）。詳細は memory [[sp204-identity-domain-jetuse-dev-idp]]。
