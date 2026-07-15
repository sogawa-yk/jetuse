# FIX-47 クリーンルーム E2E 検証レポート（Issue #47: RAG アップロード 500）

- 日付: 2026-07-10 起草 / **2026-07-13 E2E 完了**
- 対象: 公開 ORM スタック（infra/orm + OCIR 公開イメージ）の別テナンシ可搬性
- 環境: jetuse:test（GenAI project ゼロのクリーンルーム）/ RM スタック `jetuse-spike-fix47`
  （us-chicago-1 — 大阪 VCN 枠超過のため切替・修正はリージョン非依存）/ IAM は人間作成の
  最小セット（既存 DG `jetuse-deploy-test-dg` + runtime policy 20 statements。「これがあれば
  ユーザーはデプロイできる」想定の一致セット — 施主定義）
- 結論: **Issue #47 をクリーンルームで再現し、修正版で根治を実証**（E2E 4シナリオ合格）

## 1. 事象と根本原因

Issue #47 は「別テナンシの RM デプロイで `POST /api/rag/files` が HTTP 500」。
コード監査と実機プローブで確定した環境依存は 3 点:

1. **PROJECT_OCID 未配線（CRITICAL）**: DP 状態 API（Files / Vector Store files / Conversations /
   Responses）は `OpenAi-Project` ヘッダに GenerativeAiProject OCID が必須（specs/00 未文書仕様）なのに、
   infra/orm はこれを注入せず、アプリは**空ヘッダ**を送っていた。自環境は手動作成した project +
   `.env` でしか動いていなかった。影響半径は RAG だけでなく**既定チャット（gpt-oss-120b =
   responses 系）と会話メモリも全滅**（chat.py:540 / chat.py:65,73）。
2. **IAM 最小権限セット未実証（HIGH）**: 自環境 RP は `manage all-resources` で動いており、
   iam モジュールの絞った statement 群が agentic API に十分かは未実証だった。
3. **エラーの表面化不足**: CP/DP の 4xx が rag ルートで未処理のまま 500 になり、現場で切り分け不能。

## 2. 修正内容

| 領域 | 変更 |
|---|---|
| packages/api/jetuse_core/genai.py | `resolve_project_ocid()` 新設: 設定 > プロセス内キャッシュ > compartment 内 ACTIVE project 検索（全ページ走査）> 自動作成（`jetuse-project`。**PROJECT_AUTOCREATE=true のときのみ** — ベアランタイム既定は検出のみ。作成後 ACTIVE 未達なら raise しキャッシュしない）の順で解決。解決不能なら `ProjectResolutionError`（actionable メッセージ）で即時 raise し、**空の OpenAi-Project ヘッダを送らない** |
| packages/api/jetuse_core/rag.py | `health_check()` 新設: ①project 解決 ②CP `vector_stores.list` ③DP `files.list`（OpenAi-Project 付き）の 3 点検査を構造化して返す |
| packages/api/service/routes/rag.py | `GET /api/rag/health` 追加。rag ルート全経路で CP/DP 由来の 401/403/404→503、その他 4xx/5xx→502 に変換（DG matching rule / policy / PROJECT_OCID / リージョン対応を確認せよのヒント付き。OCID 実値は返さない） |
| infra/terraform/modules/iam | `enable_project_autocreate`（モジュール既定 false = opt-in）で runtime policy に `manage generative-ai-project` を追加（自動作成の create/get に必要） |
| infra/orm | `variable "project_ocid"`（既定 ""）+ `variable "enable_project_autocreate"`（**スタック既定 true** — ワンクリックの無手動セットアップ要件）+ schema.yaml 項目 + `api_environment.PROJECT_OCID / PROJECT_AUTOCREATE` を配線。schema version 20260710 |
| .env.example | PROJECT_OCID 追記 |

ユニットテスト 14 件追加（project 解決の分岐・エラー変換・health 3 点検査）。
`pytest packages/api/tests` 261 件全緑 / ruff クリーン / `terraform validate` 成功 /
iam モジュール `terraform test` 6 run 全緑。

## 3. クリーンルーム再現（2026-07-13 実施・再現成功）

旧公開イメージ（`jetuse-api:latest`）+ project ゼロの jetuse:test で:

- `POST /api/rag/files` → **素の 500 "Internal Server Error"（Issue #47 再現）**。実例外は
  DP `/openai/v1/files` が空 OpenAi-Project ヘッダで **400** → 未処理のまま 500
  （報告者環境は CP 段の 404 と段差はあるが、「状態系 API + project 欠如 → 未処理 5xx」は同根）。
- **会話メモリ（STM）も全滅を実証**: 2ターン目が直前の発話内容を想起できない
  （OCI Conversation 作成失敗 → stateless への silent fallback）。
- 監査の精緻化: **ステートレスチャットは空 project ヘッダでも成功**する（全滅は状態系に限定）。

## 4. E2E 結果（2026-07-13 完了 — 全シナリオ合格。証跡: runs/2026-07-10T0842_FIX-47/e2e/RESULTS.md）

- [x] シナリオ0: Issue #47 再現（上記 §3）
- [x] シナリオ1: クリーンルーム RAG E2E — **project 自動作成（jetuse-project）発動** → upload 200 →
      約40秒で索引化 → **file_search grounded 応答（出典付き正答）** + 既定モデルチャット +
      **STM 2ターン記憶保持**
- [x] シナリオ2: 明示 PROJECT_OCID — health 3点 ok（source=env）・upload/索引化成功・
      **自動作成の非発動**（project 総数 1 のまま）
- [x] シナリオ3: ネガティブ（無効 PROJECT_OCID）— upload が **503 + 原因ヒント**（500 を漏らさない）・
      `/api/rag/health` が **data_plane を失敗点として特定**

## 5. 最小 IAM セットの実証（Issue #47 の最有力容疑への回答）

人間作成の最小セット = 既存 DG `jetuse-deploy-test-dg`（matching rule: jetuse:test の
computecontainerinstance / fnfunc / autonomousdatabase / generativeaisemanticstore）+
runtime policy **20 statements**（3 DG 構成を単一 DG に畳んで distinct。全文は
runs/2026-07-10T0842_FIX-47/iam-report.md）+ テナンシの namespace 読取 1 文。
この構成で project 自動作成〜RAG 全経路の動作を実証 = **iam モジュールの絞った statement 群
（+ 新規 manage generative-ai-project）は agentic API に十分**。
→ 報告者の 404 は「DG matching rule がデプロイ先リソースを包含していない」または
「statements の不足/対象 compartment 不一致」が濃厚で、`/api/rag/health` で自己診断可能になった。

## 6. E2E 中の新発見（後続チケットへ）

1. **ORM on-behalf-of は `inspect tenancies` 必須**（region_subscriptions が null → plan 不能。
   制限 deployer に効く Issue #55 由来の可搬性ギャップ）→ PORT-01
2. **prefix 15文字超で VCN dnsLabel 上限超過**（schema に長さ検証なし）→ PORT-01
3. **ADB in-place rename で wallet リソースが stale のまま** → 新 tnsnames と不整合で DB 全断。
   恒久修正 = wallet リソースに `replace_triggered_by`（db_name）→ PORT-01。
   アプリ側 /tmp ウォレットキャッシュの無検証も縮退不全 → PORT-02
4. **新規 GenerativeAiProject の DP 伝播に数分**かかる（その間 DP 404）。本修正の表面化により
   503+ヒント+health で運用可能（docs/tips.md 追記）
5. 大阪 VCN 枠超過（テナンシ実態）→ 対応4リージョン内の代替（ord）で回避可を実証
