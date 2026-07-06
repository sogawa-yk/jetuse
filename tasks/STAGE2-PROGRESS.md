# ステージ2 進捗キュー（stage-runner の単一の真実源）— SP2: テナンシ + Demo エンティティ

デモ生成プラットフォーム再設計（`specs/17-demo-platform-redesign.md` §1・§6 / ADR-0015）の第二ステージ＝
**SP2: テナンシ + Demo エンティティ**（Demo の一級化 + 箱のプロビジョニング + Identity Domains ユーザー分離）。
**base=`dev`**（SP2 は Internal 固有 — specs/17 §7）、ステージ統合ブランチ `feat/sp2-demo-tenancy`。
PASS したタスクを stage-runner がステージブランチへ自動 commit+merge する。push / dev への PR /
apply / IAM / Identity Domain は自走中も停止（人間ゲート）。

> **spec-driven**: SP2 の詳細仕様は specs/17 §6 に概略しかない。**SP2-00（specs/18 起草・人間承認）が
> 最初のゲート**であり、SP2-01 以降の受け入れ条件は specs/18 承認をもって確定する（起票時点の記述は暫定）。

status: `todo` | `in_progress` | `blocked` | `done`

| 順 | タスク | 依存 | 人間ゲート | status |
|---|---|---|---|---|
| 0 | [SP2-00 specs/18 起草（SP2 詳細仕様）+ キュー肉付け](SP2-00.md) | — | **spec 承認**（adr_approval 相当） | todo |
| 1 | [SP2-01 Demo エンティティ本格化 + CRUD ルート](SP2-01.md) | SP2-00 | コミット | todo |
| 2 | [SP2-02 箱のプロビジョニング（demo_<id> スキーマ生成・削除後始末）](SP2-02.md) | SP2-01 | コミット | todo |
| 3 | [SP2-03 DemoContext 解決先の実装 + dbchat デモスコープ化](SP2-03.md) | SP2-02 | コミット | todo |
| 4 | [SP2-04 Internal テナンシ分離（Identity Domains 実接続）](SP2-04.md) | SP2-00 ＋ 人間の事前作業 | **iam_identity**（IdP 設定は人間） | todo |

> 第0波 = SP2-00（spec 承認まで停止）。第1波 = SP2-01 ∥ SP2-04（SP2-04 は人間の事前作業が
> 未完なら blocked にして先へ）。第2波 = SP2-02。第3波 = SP2-03。

## ステージ完了条件（暫定 — specs/18 承認で確定。ステージ報告で人間が確認）

- specs/18 が人間承認済み。全タスク Codex review PASS・test/lint クリーン・実環境 E2E（または理由付き SKIPPED）通過。
- 実環境で「デモ作成 → 箱（`demo_<id>` スキーマ）生成 → デモスコープ操作（chat/rag/dbchat）→ デモ削除で
  後始末」の一連が確認できる。他ユーザーのデモ id は一貫して 404（fail-closed 回帰なし）。
- `dev` が常時デプロイ可能（main 由来機能の回帰なし。既存テスト・`npm run build` 緑）。

## スコープ境界（specs/17 §1・§6）

- ビルダー（SP3）・マーケットプレイス（SP4）・`connector.invoke` は対象外。
- 統一 Capability インターフェース（案2）への移行はしない。
- Public（main 枝）の user 単位ルートの挙動・パスは変えない。

## 実行ログ（stage-runner が追記）
- （未開始）
