# 実施できなかった範囲と理由（RAGM-03。無言スキップ禁止）

## 1. jetuse-dev への配備（Container Instance + API Gateway の公開 URL）

- **何を**: `loop-config.yml` の web area `deploy_cmd` = `ops/dev-env-up.sh loop --apply`
  （API イメージを build/push → Terraform apply → SPA を Object Storage へ配信 → 公開 URL）。
- **なぜ実施できない**: この作業機（macOS）に**コンテナランタイムが無い**
  （`podman` / `docker` とも未インストール。`dev-env-up.sh` は `podman build` で始まる）。
  加えて `infra/terraform/environments/app/loop.tfvars` がこの worktree に無く、
  作成には ADB スキーマのパスワード等の人間の事前作業が要る。
- **代わりに実施したこと**: 同じアプリコードを**実 OCI 認証で**ローカル起動し
  （`uvicorn` + `.env` の `AUTH_MODE=config_file`。起動時に ap-osaka-1 の `vector_stores` へ
  実際に到達している）、SPA の dev サーバ経由＝**画面と同じ HTTP 経路**で検証した。
- **この差で確認できていないもの**: API Gateway / LB 経由の応答、Identity Domain 認証、
  `AUTH_MODE=resource_principal`。いずれも本タスクが触っていない層で、
  `docs/verification/PUBLIC-DEPLOY-E2E.md` が別途担保している。
  本タスクの差分（能力カタログの静的な記述と、その描画）は配備形態に依存しない。

## 2. 取り込み状況バッジ（ADB を含む 4 バッジ）の実データ表示

- **何を**: ファイル一覧に実ファイルを載せて `VS / ADB / SAI / OS` のバッジを実応答で見ること。
- **なぜ実施できない**: この worktree の `.env` に `ADB_DSN` / ウォレット設定が無く、
  `/api/rag/files` が 503（`DPY-4000: unable to find "" in tnsnames.ora`）になるため、
  実ファイル一覧を出せない。ウォレット配置は人間の事前作業（`docs/guides/dev-environments.md`）。
- **代わりに実施したこと**: バッジの 4 状態（indexed / pending / error / disabled）×
  ADB を含む並びを frontend の単体テストで固定した
  （`packages/web/src/pages/rag/BackendStatusBadges.test.tsx`）。
  バッジの**意味**は変えていない（表示対象に `adb` が増えただけ）。
- **副次的に判明した既存不具合を 1 行で直した**: `/api/rag/files` が 503 を返すと
  `d.files` が undefined になり、`files.some(...)` で **RAG ページ全体が白画面**になっていた
  （本タスク以前からの挙動。E2E がこれで止まったため `Array.isArray(d?.files)` で防いだ）。

## 3. `adb` バックエンドでの実検索（チャンク出典が実際に返ること）

- **何を**: 画面から ADB を選んで質問し、セル範囲つき出典が返ることまで。
- **なぜ実施しない**: それは RAGM-02 / PREP-01 の受け入れ範囲で、実 ADB に対して
  実施・記録済み（`docs/verification/RAGM-02.md` / `docs/verification/PREP-01.md`）。
  本タスクは「その能力差を API と画面から**見えるようにする**」ことが範囲であり、
  上記 2 の理由で DB 接続も無い。

## 4. VPD / 業務表 JOIN の実証

本タスクの非ゴール。むしろ**未実証であることを画面と API に出す**のが成果物側の要件で、
`support: "unverified"` として表示されることをシナリオ 1/2 で確認した。
