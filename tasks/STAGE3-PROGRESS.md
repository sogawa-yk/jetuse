# ステージ3 進捗キュー（stage-runner の単一の真実源）— SP3: ビルダー

デモ生成プラットフォーム再設計（`specs/17-demo-platform-redesign.md` §1・§6 / ADR-0015）の第三ステージ＝
**SP3: ビルダー**（ヒアリング(NL) → 能力カタログを LLM に渡してデモ設計 → OpenCode + OCI モデルで
静的SPA生成 → サンプルデータ生成・投入 → `Demo` として保存）。
**base=`dev`**（SP3 は Internal 固有 — specs/17 §7）、ステージ統合ブランチ `feat/sp3-builder`。
PASS したタスクを stage-runner がステージブランチへ自動 commit+merge する。push / dev への PR /
apply / IAM / **ADR 承認**は自走中も停止（人間ゲート）。

> **spec-driven**: SP3 の詳細仕様は `specs/19-sp3-builder.md`（SP3-00 で起草済み・人間レビュー待ち）。
> **SP3-00（specs/19 起草・人間承認）が最初のゲート**。SP3-01〜05 の受け入れ条件は specs/19 §9 参照で
> 肉付け済みであり、**有効になるのは specs/19 の人間承認をもって**（承認までは SP3-01 以降を起動しない）。
>
> **技術リスクの明示**: SP3 の核 = 「OpenCode + OCI モデルによる静的SPA生成」を**サーバ側で安全に回す**
> こと。ここは未検証の技術限界が出やすい（生成ランタイム・サンドボックス・生成物の安全性）。
> SP3-03 で **ADR（OpenCode 統合方式）を起草→承認**してから実装する。限界に当たったら実装を止め、
> `docs/decisions/` に findings を残して判断を仰ぐ（施主方針 2026-07-07: 技術的限界まで攻める）。

status: `todo` | `in_progress` | `blocked` | `done`

| 順 | タスク | 依存 | 人間ゲート | status |
|---|---|---|---|---|
| 0 | [SP3-00 specs/19 起草（SP3 詳細仕様）+ キュー肉付け](SP3-00.md) | — | **spec 承認**（adr_approval 相当） | blocked |
| 1 | [SP3-01 ビルダー・パイプライン API 骨格 + ヒアリング(NL)](SP3-01.md) | SP3-00 | コミット | todo |
| 2 | [SP3-02 デモ設計（能力カタログ→デモプラン生成）](SP3-02.md) | SP3-01 | コミット | todo |
| 3 | [SP3-03 フロント生成（OpenCode + OCI モデル→静的SPAバンドル）+ デモ配信](SP3-03.md) | SP3-02 | **adr_approval**（OpenCode 統合 ADR）+ コミット | todo |
| 4 | [SP3-04 サンプルデータ生成 + 箱への投入（demo_<id>）](SP3-04.md) | SP3-02 | コミット | todo |
| 5 | [SP3-05 ビルダー UI（ヒアリング→プレビュー→保存）+ デモ産出 E2E](SP3-05.md) | SP3-03 ＋ SP3-04 | コミット | todo |

> 第0波 = SP3-00（spec 承認まで停止）。第1波 = SP3-01。第2波 = SP3-02。
> 第3波 = SP3-03 ∥ SP3-04（ともに SP3-02 のデモプランに依存・相互独立）。
> 第4波 = SP3-05（生成フロント＋データが揃ってからプレビュー/保存/産出 E2E）。

## E2E 方針（施主指示 2026-07-07）

以降の実環境 E2E は**デプロイ済みのプレビュー環境**（RM スタック `jetuse-dev-app` / gateway
`https://jstvwfl2fhsx55p2zaubm5ui6m.apigateway.ap-osaka-1.oci.customer-oci.com`、AUTH オフ・ADB 再利用・
GenAI はリソースプリンシパル）に対して行う。新規スパイクを都度立てない。
参照: memory `sp2-preview-rm-deploy-dev-app`。DB 系は `demo_<id>` / `JETUSE_SP3_xx` スキーマで隔離。

## ステージ完了条件（specs/19 §10 で確定。ステージ報告で人間が確認）

- specs/19 が人間承認済み。全タスク Codex review PASS・test/lint クリーン・実環境 E2E
  （または理由付き SKIPPED）通過。
- デプロイ済み環境で「**フィールドSA がヒアリングに答える → ビルダーが能力カタログからデモを設計 →
  OpenCode+OCI で静的SPA を生成 → サンプルデータを `demo_<id>` に投入 → `Demo` として保存 →
  `/api/demos/{id}/...` で生成デモが開き、JetUse API（chat/rag/dbchat 等）をデモスコープで叩ける**」
  の一連が通る（旧 UC-03 の「非開発者が5分でデモ作成」を Internal ビルダーで満たす）。
- 生成フロントは**バックエンドを生成しない**（JetUse API を叩くだけ）。生成物の安全性
  （デモスコープ外を叩けない・秘密を埋め込まない）が構造的に担保される。
- `dev` が常時デプロイ可能（main 由来機能・SP2 の回帰なし。既存テスト・`npm run build` 緑）。

## スコープ境界（specs/17 §1・§6・§8）

- マーケットプレイス（SP4・配布/署名）は対象外。`connector.invoke`（外部接続能力）は対象外
  （必要になれば specs/19 で別途切り出し判断）。
- 統一 Capability インターフェース（案2）への移行はしない（カタログ出力形は不変前提）。
- 既存 usecases（Public ショーケース）は SP3 ビルダーと統合しない（ペルソナ別）。
- Public（main 枝）の user 単位ルートの挙動・パスは変えない。

## 実行ログ（stage-runner が追記）
- 2026-07-07: ステージ起動（feat/sp3-builder を origin/dev=a5eeb58 に ff 済）。第0波 = SP3-00 を方式B（herdr ペイン）で起動。
- 2026-07-07: SP3-00 完了（codex review-1 PASS / blocker 0 / minor 2。E2E は docs タスクのため理由付き SKIPPED）。
  `integrate_task.sh` で `feat/sp3-builder` へ統合（specs/19・docs/comparison/frontend-generation-runtime.md・
  SP3-01〜05 肉付け）。area=docs のため test/lint 対象なし。residual minor 2 件（F-001 tasks/SP3-02.md:9、
  F-002 tasks/STAGE3-PROGRESS.md:41 — 旧表記）は spec 承認レビュー時に修正推奨。証跡 runs/2026-07-07T1020_SP3-00/。
- 2026-07-07: **specs/19 の人間承認待ちで停止**（adr_approval 相当ハードゲート。承認まで SP3-01 以降を起動しない）。
