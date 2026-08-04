# 実施できなかった範囲と理由（RAGM-04。無言スキップ禁止）

## 1. マネージド側（Files API → Vector Store）への実アップロード

- **何を**: `POST /api/rag/files` を最後まで通し、マネージド側の属性 `kind` と
  ADB 側の `kind` 列に**同じ文字列**が入ったことを両方から読み出すこと。
- **なぜ実施できない**: 検証に使ったコンパートメント（`.env` の `COMPARTMENT_OCID`。表示名は
  **`jetuse`** — `CLAUDE.md` の「jetuse-proto」・`loop-config.yml` の「jetuse-dev」はどちらも
  この 1 つを指す呼び名。`env.log` で確認）に **ACTIVE な GenerativeAI project が 0 件**で、
  `resolve_project_ocid()` が失敗する（`.env` の `PROJECT_OCID` は空・`PROJECT_AUTOCREATE=false`）。
  RAG のアップロードは project 必須のため 503 になる（`scenario-1.md` 1c の生応答）。
  自動作成に切り替えると共有名 `jetuse-project` のリソースを新規作成することになり、
  スパイク用プレフィックス（`jetuse-spike-`）の外なので**人間ゲート**。勝手に作らない。
- **代わりに実施したこと**:
  - 不正な `kind` の 422 は **OCI を呼ぶ前**の門番（`rag_metadata`）で起きるため、
    この環境要因の影響を受けずに実 API で確認できている（`scenario-1.md` 1a/1b）。
  - ADB 側は実 ADB へ本当に取り込み、`kind='spec'` が**文字列で**入ることを表から確認した。
  - マネージド側へ同じ値が渡ることは、`rag.add_file` が同一の `attrs["kind"]` を両方へ渡す
    実装（変換なし）と単体テスト `test_kind_is_passed_to_the_adb_backend` で固定してある。
- **この差で確認できていないもの**: マネージド側に実際に格納された属性値の読み出し。
  型が文字列に固定された今、両者が食い違う経路は実装上残っていない（変換箇所を消したため）。

## 2. jetuse-dev への配備（Container Instance + API Gateway の公開 URL）

- **何を**: web area の `deploy_cmd` = `ops/dev-env-up.sh loop --apply`。
- **なぜ実施できない**: この作業機（macOS）に `podman` が無く（`dev-env-up.sh` は `podman build` で始まる）、
  `infra/terraform/environments/app/loop.tfvars` もこの worktree に無い（作成は人間の事前作業）。
  RAGM-03 と同じ制約。
- **代わりに実施したこと**: 同じアプリコードを実 OCI 認証・実 ADB で起動し、
  SPA の dev サーバ経由（＝画面と同じ HTTP 経路）で検証した。api area の `deploy_cmd`
  （実 ADB へのマイグレーション適用）は実施済み（`deploy.log`）。
- **この差で確認できていないもの**: API Gateway / LB 経由の応答、Identity Domain 認証、
  `AUTH_MODE=resource_principal`。いずれも本タスクが触っていない層で、
  `docs/verification/PUBLIC-DEPLOY-E2E.md` が別途担保している。

## 3. `rag_adb.search` のフィルタ経路（`kind` での実絞り込み）

- **何を**: ADB バックエンドに `kind='spec'` で絞った検索を実行して結果を比べること。
- **なぜ実施しない**: チャット API から ADB へ絞り込み条件を渡す口が**そもそも無い**
  （`rag_filters` は vector_store 専用で、それ以外は 400。RAGM-03 の能力表示でも
  「絞り込み △ 条件付き」として明示済み）。`rag_adb.build_where` が受けるのは
  内部呼び出しの `current_version='Y'` だけで、その経路は RAGM-02 で検証済み。
- 本タスクの範囲は「同じ値が両バックエンドに同じ型で入る／同じフィルタが同じ応答になる」ことで、
  それは `scenario-1.md` 1b（4 バックエンドとも 422）と単体テストで固定した。
