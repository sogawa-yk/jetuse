# ステージ1 進捗キュー（stage-runner の単一の真実源）— SP1: JetUse API

デモ生成プラットフォーム再設計（`specs/17-demo-platform-redesign.md` / ADR-0015）の第一ステージ＝
**SP1: JetUse API**（能力カタログ + DemoContext seam + デモスコープ縦切り）。
**base=`main`**（SP1 は Public 共通の土台 — specs/17 §7）、ステージ統合ブランチ `feat/sp1-jetuse-api`。
PASS したタスクを stage-runner がステージブランチへ自動 commit+merge する。push / main への PR /
apply / IAM は自走中も停止（人間ゲート）。

status: `todo` | `in_progress` | `blocked` | `done`

| 順 | タスク | 依存 | 人間ゲート | status |
|---|---|---|---|---|
| 1 | [SP1-01 能力ディスクリプタ8件 + GET /api/capabilities](SP1-01.md) | — | コミット | todo |
| 2 | [SP1-02 demos 最小レジストリ + DemoContext seam](SP1-02.md) | — | コミット | todo |
| 3 | [SP1-03 デモスコープ能力ルート縦切り（chat + rag）](SP1-03.md) | SP1-01, SP1-02 | コミット | todo |

> 第1波 = SP1-01 ∥ SP1-02（相互独立・並列可）。第2波 = SP1-03。
> 8能力（chat/rag.search/dbchat/agents/voice/minutes/translate/docunderstand）のルートはすべて main に
> 実在する（translate/docunderstand は `routes/voice.py` 内）ため、ルート新設タスクは無い。

## ステージ完了条件（specs/17 §9。ステージ報告で人間が確認）

- 3タスクすべて Codex review PASS・test/lint クリーン・実環境 E2E（または理由付き SKIPPED）通過。
- `GET /api/capabilities` が 8 能力のカタログ（OpenAPI 由来技術詳細 + 手書きディスクリプタ）を返す。
  裏方ルート（admin/conversations/tools 等）は載らない。
- `/api/demos/{demo_id}/...` 配下が `DemoContext` を経由し、**他ユーザーのデモ id では 404** になる
  （所有権検証 fail-closed の実機確認）。Public 用 user 単位ルートは回帰なし。
- `main` が常時デプロイ可能（既存テスト・`npm run build` 回帰なし）。

## スコープ境界（specs/17 §8）

- Demo エンティティの本格 CRUD・箱のプロビジョニング（スキーマ/ベクタストア生成）は **SP2**。
  SP1-02 の demos テーブルは所有権検証に必要な最小列のみ（specs/17 §9 の受け入れ条件が根拠）。
- `connector.invoke`・統一 Capability インターフェース（案2）・ビルダー・マーケットは対象外。

## 実行ログ（stage-runner が追記）
- （未開始）
