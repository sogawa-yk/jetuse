# ADR-0016: Internal 安定版リリースライン `internal-stable` の追加（ADR-0014 を追補）

日付: 2026-07-06
状態: 承認（2026-07-06 施主承認。枝名は `internal-stable` で確定）
関連: ADR-0014（追補対象）／docs/guides/branching-and-releases.md

## 背景

ADR-0014 は `main`=Public 正式版・`dev`=Internal 統合＆リリース元、の 2 長期ブランチを定めた。
Internal 版は当初「`dev` にタグ（`internal-vX.Y.Z`）を打ってリリース」する運用だった。

構想の更新により、Internal 版は**施主（ベンダー）が単一インスタンスを常時ホスティング**し、フィールドSA が
Identity Domains 認証でアクセスする形になった。`dev` は先行機能の統合で常に churn するため、これを本番
配信元にすると「デプロイ中に `dev` の未リリース作業へ引きずられる」「安定版への hotfix が困難」という問題が出る。

## 決定

1. Internal 安定版ブランチ **`internal-stable`** を新設する。**施主のホスト本番はこの枝を配信元にする**。
2. 長期ブランチを 3 本にする:
   - `main` = Public 安定版・Deploy ボタン配布元（常時デプロイ可）。
   - `dev` = Internal 統合（開発）。`main ⊆ dev`。
   - `internal-stable` = Internal 安定版。`dev → internal-stable`（リリース点で merge）+ tag `internal-vX.Y.Z`。
3. **非対称の根拠**: Public は各ユーザーが tag からセルフホスト → `main` 自体が安定版で足りる（安定枝不要）。
   Internal は施主が常時ホスト → 配信元を統合枝から切り離す価値がある。
4. フロー:
   - Public/共通: `main → feature/public-* → main →（sync）→ dev`
   - Internal 機能: `dev → feature/internal-* → dev`
   - Internal リリース: `dev → internal-stable` + tag。本番は `internal-stable` を配信。
   - Internal hotfix: `internal-stable → hotfix/* → internal-stable →（forward）→ dev`
   - Public 緊急: `main → hotfix/* → main →（sync）→ dev`
5. `dev → main` の全体 merge は引き続き禁止（ADR-0014 を維持）。`internal-stable` も `main` へ merge しない。

## 影響

- `docs/guides/branching-and-releases.md` に `internal-stable` を追記し、Internal リリース手順を
  「`dev` にタグ」から「`dev → internal-stable` + タグ」へ更新する。
- Branch protection: `internal-stable` も direct push 禁止・PR/CI 必須・Internal release owner review。
- 枝名は `internal-stable`（代替: `release/internal`）。採択時に確定する。

## 非ゴール / 留保

- 本 ADR の採択・ガイド改訂・branch protection 設定は人間ゲート。
- ブランチ作成（`dev` / `internal-stable`）は承認後に実施する。
