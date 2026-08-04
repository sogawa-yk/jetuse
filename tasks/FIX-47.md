# タスク: FIX-47 Issue #47 — 公開 ORM スタックのテナンシ可搬性（RAG アップロード 500）修正

## 目的
Issue #47「RAGチャットのファイルアップロードで HTTP 500」の根治。公開 ORM スタック
（infra/orm + OCIR 公開イメージ）を**自テナンシ固有の手動セットアップに依存せず**別テナンシで
動くようにする（環境依存の排除）。修正後は jetuse:test コンパートメントに実リソースを作成して
クリーンルーム E2E まで実施する（施主指示 2026-07-10）。

## 事前調査で確定済みの事実（orchestrator probe 2026-07-10。再検証不要・前提としてよい）
- 事象: 別テナンシの RM デプロイ（**旧公開イメージ** — コンテナログの適用 migration が 001–015）で
  `POST /api/rag/files` → `ensure_store()` → CP `vector_stores.create` が
  **404 NotAuthorizedOrNotFound** → 未処理例外で 500。`OciResourcePrincipalAuth` 初期化直後の失敗
  = RP 署名リクエスト。報告者は DG / IAM Policy / RP / compartment 設定は確認済みと主張。
  Console（user principal・ネイティブ API）からは同一 compartment に Vector Store 作成可。
- 自テナンシプローブ（2026-07-10・user principal・ap-osaka-1）: CP `vector_stores.create` は
  **GenerativeAiProject ゼロの jetuse:test でも成功**（completed 到達・削除済み）。
  → 「compartment に project が無いと CP create が 404」仮説は**棄却**。この日の大阪 CP create は健全。
- jetuse:test compartment = `jetuse` の子 `test`
  （OCID は `oci iam compartment list -c <jetuse> --name test` で解決。実値のコミット禁止）。
  GenAI project ゼロのクリーンルーム。
- **排除できていない環境依存（確定・修正対象）**:
  1. **PROJECT_OCID 未配線**: DP 状態 API（files.create / attach / files.list 等）は `OpenAi-Project`
     ヘッダ必須（specs/00 未文書仕様）なのに、infra/orm の `api_environment` に PROJECT_OCID が無い・
     stack 変数も無い・`.env.example` にも無い。GenerativeAiProject は TF provider 未対応
     （infra/terraform/environments/loop/main.tf 注記）で、自環境は手動 CLI 作成した project に依存。
     → 仮に CP create が通っても、その先の DP 呼び出し（`make_inference_client(with_project=True)` が
     **空の OpenAi-Project ヘッダ**を送る）で別テナンシは必ず落ちる。
  2. **IAM 最小権限セット未実証**: 自環境の RP は `manage all-resources`（jetuse-internal-dg）で動いて
     おり、infra/orm の iam モジュールの絞った statement 群（`use generative-ai-family` +
     `manage generative-ai-vector-store` / `generative-ai-vectorstore-file` / `generative-ai-file`）が
     agentic API に十分かはクリーンルーム未実証。報告者の 404（RP でのみ発生）はここが最有力容疑。
  3. **エラーの表面化不足**: CP/DP の 4xx が rag ルートで未処理のまま 500 になり、現場で
     policy / DG matching rule / project / リージョン対応のどれが原因か切り分け不能。
- RM ZIP（GitHub リリース orm-main）は Terraform + SPA dist のみで **API コードを含まない**
  （scripts/package-orm-stacks.sh）。報告者の「RM ZIP 上の genai.py 修正」は実行コードに反映され得ない
  （API は OCIR イメージ経由でしか変わらない）— 診断上のノイズとして扱う。
- OCI Python SDK は GenerativeAiProject CRUD 対応済み
  （`GenerativeAiClient.create/list/get/delete_generative_ai_project` — .venv で確認済み）。
- 大阪 GenAI agentic は 2026-07-08〜09 に store/file 索引の障害（docs/tips.md 2026-07-08 2件）。
  E2E リージョン選定に影響（下記 E2E 前提）。
- **コードベース監査（2026-07-10）で PROJECT_OCID の影響半径拡大を確認**: 既定チャットモデル
  `gpt-oss-120b` は responses 系（models.py `DEFAULT_MODEL`）で chat.py:540 が
  `with_project=(api=="responses")` → **別テナンシでは既定チャットも全滅**。会話永続化
  （chat.py:65,73 `with_project=True`）も project 必須で、失敗時は stateless へ silent fallback
  （メモリ喪失）。いずれも本タスクの genai.py fail-fast + PROJECT_OCID 配線で同時に根治する。
  監査で見つかったその他の環境依存は PORT-01（infra）/ PORT-02（api 縮退）に分割
  （キュー: tasks/FIX47-PROGRESS.md）。

## 仕様参照
specs/00-architecture.md（DP は OpenAi-Project 必須）、specs/09-rag.md、specs/18 §3（箱ライフサイクル・
write-ahead 台帳）、docs/tips.md（大阪障害・project OCID はリージョン別）

## 前提（依存タスク / 人間の事前作業）
- base ブランチ: **main**（Public 変更 — infra/orm・packages/api・iam モジュール。
  `docs/guides/branching-and-releases.md`）。起動は `BASE_BRANCH=main` を渡す。
- **人間ゲート（このタスク固有）**: jetuse:test への RM apply のうち **IAM（Dynamic Group + Policy）
  作成部分は適用前に人間承認必須**（CLAUDE.md）。承認対象の statement 一式を提示して停止する。
- E2E は施主指示（2026-07-10）により **jetuse:test に実リソースを作成して実施**
  （loop-config の e2e.compartment=jetuse-dev をこのタスクに限り上書き。リソース名は
  `jetuse-spike-fix47` プレフィックス必須）。

## 対象 area
api（packages/api: genai.py / rag.py / bootstrap or health）。
infra（infra/orm・infra/terraform/modules/iam）は area 定義外のため、`terraform validate` と
RM plan 成功を完了条件に**明示的に**含める（loop-config.yml は編集しない — 仕組みの人間ゲート）。

## 作業内容
1. **クリーンルーム再現（診断が先）**: HEAD の infra/orm + 現行公開イメージ（`jetuse-api:latest` の
   まま）を RM スタック `jetuse-spike-fix47` として jetuse:test に deploy
   （`enable_dynamic_group=true`・`enable_runtime_policy=true`。**IAM 部分は人間承認後に** apply。
   RM ジョブは `oci resource-manager job` 経由 — ローカル apply は権限層で遮断される）。
   gateway 経由で RAG upload を実行し、Issue #47 の 404 が再現するか確認する。
   - **再現した場合**: 不足 policy statement を二分探索で特定（自環境 DG との差分から当たりを付ける。
     project の read/manage、agentic 系 resource-kind 等）→ iam モジュールへ追加して根治。
   - **再現しない場合**: 残る差分はテナンシ側（agentic API の利用可否/allowlist 等）。下記 4 の診断
     エンドポイントと runbook を成果物にし、Issue #47 への確認依頼コメント案を書く（投稿は人間ゲート）。
2. **PROJECT_OCID の配線と自動解決（依存排除の本丸）**:
   - infra/orm: `variable "project_ocid"`（既定 ""）+ schema.yaml 項目 +
     `api_environment.PROJECT_OCID` を追加。
   - アプリ側: `settings.project_ocid` が空のとき、初回 GenAI 利用時（または bootstrap）に SDK で
     compartment 内の ACTIVE project を検索 → 無ければ `jetuse-<prefix>-project` を自動作成して採用。
     解決結果はプロセス内キャッシュ（毎回作らない・既存があれば再利用）。自動作成に必要な policy
     （`manage generative-ai-project` 相当）を実測し iam モジュールの statement へ追加。
   - genai.py: `with_project=True` で project が未解決なら**空ヘッダを送らず**、actionable な
     メッセージ（PROJECT_OCID 設定 or 自動作成権限の付与を促す）で即時 raise。
   - `.env.example` に PROJECT_OCID を追記。
3. **エラーの表面化**: rag ルート（upload / ensure_store 経路）で CP/DP 由来の 400/403/404 を
   500 のまま漏らさず、原因ヒント付き 503/502 に変換（「DG matching rule / policy statements /
   PROJECT_OCID / リージョンの agentic API 対応を確認」の粒度。秘密値・OCID 実値はログに出さない）。
4. **プリフライト診断**: `GET /api/rag/health`（または既存 health の拡張）で
   ① CP `vector_stores.list` ② DP `files.list`（OpenAi-Project 付き） ③ project 解決状態
   の 3 点を検査し、どこで落ちるかを構造化して返す（Issue 報告者が自己診断できる粒度・認可済み前提）。
5. **修正版イメージの E2E 投入**: 修正コードで API イメージをビルドし、**専用タグ**
   （例 `jetuse-api:fix47-e2e`）で OCIR へ push → stack 変数 `api_image_url` で参照して再 deploy。
   **`:latest` タグへの push は禁止**（公開経路。正規リリースは release.yml + 人間ゲート）。
6. **ドキュメント**: docs/verification/fix47-clean-room-e2e.md（再現有無・最小 IAM statement 群・
   E2E 結果）。実機の新発見は docs/tips.md へ追記。

## 完了条件（検証可能な述語で）
- packages/api: `.venv/bin/pytest packages/api/tests` 全緑・`.venv/bin/ruff check packages/api`
  クリーン（新規分のユニットテスト含む: project 自動解決の分岐・エラー変換・health の 3 点検査）。
- infra: `terraform validate`（infra/orm）成功、jetuse:test への RM plan 成功。
- codex-review の review_verdict=PASS。
- 下記 E2E シナリオを jetuse:test 実環境で通過し、証跡を runs/<run-id>/e2e/ に記録。

## E2E シナリオ（完了ゲート・min_scenarios=2 以上）
前提: RM スタック `jetuse-spike-fix47`（jetuse:test）。リージョンは原則 ap-osaka-1（Issue と同条件）。
ただし大阪 agentic 障害（tips 2026-07-08〜09）の継続は「store 作成 + file attach + 索引化完了」を
1 セットのプローブで先に判定し、継続中なら該当部分のみ ord（us-chicago-1。ORM 対応 4 リージョンに
含まれ公開イメージも push 済み）で代替してその旨を証跡に記す。大阪 VCN 枠不足で apply が落ちる場合も
同様に ord へ切替可（修正自体はリージョン非依存）。
1. **クリーンルーム RAG E2E（本命）**: PROJECT_OCID 未指定・修正版イメージで deploy →
   gateway 経由 `POST /api/rag/files` が 500 にならず成功（project 自動作成が発動 →
   store 作成 → file attach → 索引化完了）→ RAG チャットで file_search の grounded 応答を確認。
   project が compartment に自動作成されたことを CLI（list-generative-ai-projects）で確認。
   併せて**既定モデル（gpt-oss-120b）のチャット応答**と**会話 2 ターン目の文脈保持（STM）**を確認
   （PROJECT_OCID の影響半径 — 監査 2026-07-10）。
2. **明示 PROJECT_OCID E2E**: PROJECT_OCID を stack 変数で明示して再 deploy（または env 更新）→
   同 upload フローが指定 project 配下で成功（自動作成が**発動しない**ことも確認）。
3. **ネガティブ（表面化）**: 無効な PROJECT_OCID を注入（or 人間承認の範囲で policy statement を
   一時除去）→ API が 500 でなくヒント付き 4xx/503 を返し、`/api/rag/health` が失敗点を特定して
   返すことを確認。
証跡: リクエスト/レスポンス・コンテナログ・CLI 出力。スタック `jetuse-spike-fix47` は
**PORT-01 / PORT-02（tasks/FIX47-PROGRESS.md）の E2E でも再利用するため残置**し、キュー全体の
完了後に destroy して jetuse:test を掃除する（jetuse-spike- プレフィックスのリソースのみ。
自動作成 project も削除。後始末の正本はキュー側）。

## 成果物
- コード: infra/orm（variables/schema/locals）・infra/terraform/modules/iam・
  packages/api（genai.py / rag.py / bootstrap or health / tests）・.env.example
- docs/verification/fix47-clean-room-e2e.md（再現有無・最小 IAM・E2E 証跡）
- Issue #47 への返信コメント案（修正内容 + 報告者向け確認手順。**投稿は人間ゲート**）

## 禁止事項
- 認証情報・テナンシ/コンパートメント OCID 実値・エンドポイント実値のコミット
  （TF_VAR / .env / RM 変数で注入）
- `jetuse-spike-` プレフィックス以外のリソース削除、jetuse:dev / jetuse:public の既存リソース変更
- OCIR `:latest` タグへの push（正規リリース経路は release.yml + 人間ゲート）
- IAM（DG/Policy）の無承認 apply、コミット / PR / push / Issue コメント投稿の無承認実行
- loop-config.yml・スキル・hooks の編集（仕組みの人間ゲート）
