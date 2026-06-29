# ADR-0019: Platform RAG 検索のテナント→ベクトルストア登録簿

日付: 2026-06-29
状態: **承認済（2026-06-29 施主承認）** — BE-04（`/api/platform/rag/search` の 501 解除）で起票。
施主は per-tenant Project 分離方式を承認した（前提: tenant = Project OCID = GenAI Project OCID）。
コードは本 ADR の決定に追従する。

> spec-driven 原則（CLAUDE.md）に従い、仕様（specs/16-platform.md §13 / ADR-0014）が明示しない
> 「テナント→ストア解決の実体」を定義する必要が生じたため、実装と同時に本 ADR を起票した。

## 背景

`/api/platform/rag/search` は ADR-0014 のブローカー経由でテナント（Project OCID）の RAG 検索を
**OCI Responses file_search 委譲**で行う。検索対象は「テナント所有のベクトルストア」に限定し、越境させない
（秘密＝`vector_store_id` は本体のみ保持）。しかし**テナント→ベクトルストアの解決元**は spec/ADR-0014 に
規定が無い。

既存の `rag_stores`（migration 005）は **OIDC ユーザ単位**（`owner_sub` = `user.subject`。`rag.add_file` が
ユーザ sub で作成）であり、**テナント（Project OCID）単位ではない**。これを流用すると
`get_store_id(tenant=Project OCID)` が常に空振りし、テナントに文書があっても検索が成立しない（BE-04 の
Codex レビュー BE04-001 が指摘）。

## 決定（ドラフト）

### 1. テナント単位の登録簿を新設する（ユーザ単位 rag_stores と分離）
`platform_rag_stores(tenant PK, vector_store_id, created_at, updated_at)`（migration 025）を正本とする。
- 検索: `rag.get_tenant_store_id(tenant) -> vector_store_id | None`。
- 登録: `rag.register_tenant_store(tenant, vector_store_id)`（upsert / 冪等）。
- `/api/platform/rag/search` はこの登録簿だけからストアを解決する。**呼び出し元はストア id を渡さない／
  受け取らない**ため、別テナントのストアへは構造的に到達できない（authorize の tenant 一致と二重境界）。

ユーザ単位の `rag_stores`（チャット RAG）とはキー体系が異なるため**別表に分離**する（キーの取り違え防止）。

### 2. ストア未登録テナントは「空ヒットの 200」（越境ではなくデータ未取込）
登録が無いテナントは fail-closed で 200・`hits:[]` を返す（403 は越境＝authorize 段で扱う）。

### 3. 委譲の作法（実機確定 / ap-osaka-1・gpt-oss-120b + file_search）
- `instructions` パラメータや長い和文プリアンブルを input に足すと file_search 併用時に 500 を誘発 →
  query をそのまま渡す。
- `tool_choice="required"` で file_search を必ず実行（`auto` は検索をスキップし空ヒットになりやすい。
  検索専用エンドポイントなので必須実行が正しい）。

## 採用方針と前提（施主承認済 2026-06-29）

3. **テナント→GenAI Project の対応**: 採用＝**per-tenant Project 分離**。`rag.search` は
   `make_inference_client(with_project=True, project_ocid=tenant)` とし、tenant（=Project OCID）を
   `OpenAi-Project` に固定する。前提は **ADR-0014 の「tenant = Project OCID」と GenAI Project OCID が
   同一**であること。両者を分離運用するテナンシでは、tenant→GenAI Project の対応表が別途必要。
   共有 Project（`settings.project_ocid`）方式は不採用（ストア id だけが境界になり Project 分離が効かない）。

   > **越境の主境界（一次境界）は OCI ではなくアプリ側**: ① authorize の tenant 一致、② 登録簿解決
   > （呼び出し元は store id を渡さない／受け取らない）の二段で構造的に閉じる。OCI 側の Project 分離は
   > **best-effort の第二境界**として `OpenAi-Project` を tenant に固定するが、これ単体に依存しない。
   > **実機確認（ap-osaka-1 / 2026-06-29 / BE-04 E2E）**: 当該テナンシの OpenAI 互換層では
   > `file_search` / `vector_stores.retrieve` とも **Project 単位の厳密な所属検証を保証しない**
   > （同一コンパートメントの別 Project からも同じストアに到達し得た）。よって OCI 側 Project 分離を
   > 「到達不能の保証」とは見なさず、一次境界（authorize＋登録簿）を正本とする。
   > なお `vector_stores.retrieve` は **CP エンドポイント限定**（推論エンドポイントは 404）。
   > 登録時のストア実在検証（`verify_tenant_store_access`）は CP クライアントで行う。

## 未解決事項（follow-up）

1. **登録（ingestion）パスの所在**: テナント文書の取込パイプライン（どのタスク／経路が
   `register_tenant_store` を呼ぶか）は本 ADR の範囲外。INFRA／別タスクで確定する。現状は登録簿と
   登録 API のみ用意し、UI/取込連携は follow-up。
2. **移行方針**: 既存のユーザ単位 `rag_stores` をテナント単位へ寄せるか、テナント取込を独立に持つか。
   現状は**独立**（既存 chat RAG は無改変）。

## 影響

- 追加: migration 025 / `rag.get_tenant_store_id` / `rag.register_tenant_store` / `rag.search`（テナント解決）。
- 契約: `/api/platform/rag/search` は 501→実体。応答に `vector_store_id` を含めない（秘密保持）。
- 既存 chat RAG（`rag_stores` / `ensure_store` / file_search）は無改変。
