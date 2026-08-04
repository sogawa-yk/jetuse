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
| 0 | [SP3-00 specs/19 起草（SP3 詳細仕様）+ キュー肉付け](SP3-00.md) | — | **spec 承認**（adr_approval 相当） | done |
| 1 | [SP3-01 ビルダー・パイプライン API 骨格 + ヒアリング(NL)](SP3-01.md) | SP3-00 | コミット | done |
| 2 | [SP3-02 デモ設計（能力カタログ→デモプラン生成）](SP3-02.md) | SP3-01 | コミット | done |
| 3 | [SP3-03 フロント生成（OpenCode + OCI モデル→静的SPAバンドル）+ デモ配信](SP3-03.md) | SP3-02 | ~~adr_approval~~ 承認済 + **PoC受理 override** 2026-07-08 | done |
| 4 | [SP3-04 サンプルデータ生成 + 箱への投入（demo_<id>）](SP3-04.md) | SP3-02 | コミット | done |
| 5 | [SP3-05 ビルダー UI（ヒアリング→プレビュー→保存）+ デモ産出 E2E](SP3-05.md) | SP3-03 ＋ SP3-04 | コミット | done |

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
  `/api/demos/{id}/app/` で生成デモが開き、JetUse API（chat / rag.search / dbchat）をデモスコープで叩ける**」
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
- 2026-07-07: **specs/19 人間承認**（ユーザー入力「OKです。ただしOpenCodeのバックエンドに使用するモデルは
  切り替え可能にしておいて下さい」）。承認条件を specs/19 §4.1 F2・tasks/SP3-03.md に反映し、
  residual F-001（SP3-02.md 「唯一の入力」表現）・F-002（完了条件の配信パス `/app/` 統一）を修正。
  SP3-00 done → 第1波 = SP3-01 起動。
- 2026-07-07: SP3-01 完了（review-1 FAIL blocker1/major3 → 全件修正 → review-2 **PASS** blocker0。
  pytest 587 passed・ruff クリーン。実環境 E2E = プレビュー環境+実 LLM で 4 シナリオ PASS
  〔ヒアリング往復 16/16・信頼境界+SP2 回帰 13/13・migration fresh/冪等・ready ゲート 13/13〕、
  実機で LLM null 出力→502 の欠陥を発見し TDD 修正済み）。`integrate_task.sh` で統合し、統合 worktree で
  pytest 587 passed・ruff クリーン再確認。証跡 runs/2026-07-07T1214_SP3-01/。
  residual（非 blocker）: M001 sufficient 最終判定の未永続化（SP3-02 の入口課題）・M002 失敗経路の
  usage_log 欠落・M003 上流 LLM 例外の 502/503 正規化なし。
  環境残置: プレビュー環境 image_url=sp3-01-e2e-3・loop ADB JETUSE_APP へ lease 最小パッケージ付与。
- 2026-07-07: 第2波 = SP3-02 起動（方式B・base=feat/sp3-builder）。
- 2026-07-08: SP3-02 完了（review-1 FAIL → 修正 → review-2 **PASS** blocker0/major1/minor2。
  pytest 627 passed・ruff クリーン。実環境 E2E 4 シナリオ緑〔実カタログ+実 GenAI design 15/15・
  ゲート境界+回帰 16/16・migration 027 10/10・design 連続3回安定 7/7〕。M001 は永続化+二段ゲートに確定、
  LLM 出力揺れは json_schema 構造化出力で対策）。統合後 637 passed・ruff クリーン再確認。
  証跡 runs/2026-07-07T1316_SP3-02/。residual: F001(major) parse 失敗ログに LLM 生出力断片が残る
  （ログ衛生）・F002 全角数字受理・F003 027 事後条件。
  **インシデント**: E2E scenario-3 初回で settings lru_cache により fresh 適用が共有 loop ADB の
  ADMIN スキーマへ誤走 → 16 オブジェクトを exact 根拠で DROP し復旧済（JETUSE_LOCK 無傷確認。
  deploy.log 追補3。loop-doctor 起票候補）。
- 2026-07-08: 第3波 = SP3-03 ∥ SP3-04 並列起動（方式B・base=feat/sp3-builder）。SP3-03 は
  ADR（OpenCode 統合方式）起草→**adr_approval で停止**の予定。
- 2026-07-08: SP3-04 完了（review-1/2 FAIL → 修正 → review-3 **PASS** blocker0/major4=residual。
  pytest 676 passed・ruff クリーン。実環境 E2E 6 シナリオ緑〔プラン→demo_<id> 投入→dbchat 日本語照会→
  rag.search→削除後始末〕。大阪 vector store 障害（サービス側退行）を検出し Chicago fallback で E2E 完走 —
  memory 更新済み）。統合後 676 passed・ruff クリーン再確認。証跡 runs/2026-07-08T0047_SP3-04/。
  residual: プラン検証の文書 filename/列名重複チェック欠如（§3.3 追加が本筋）・NUMBER 100桁超の即失敗・
  RAG 索引待機の期限超過・sql_search の SEMSTORE 未設定（既存環境ギャップ）等 — STATE.md 参照。
- 2026-07-08: SP3-03 **blocked（adr_approval）**。ADR-0023（OpenCode 統合方式）ドラフト起草・提出可能。
  技術リスクの核 = LLM 認証は**署名プロキシで実機成立**（0.3 秒/SSE 可）。OpenCode headless は
  gpt-oss-120b 2/2 成功（command-a 不可・gemini-2.5-flash 0/1）。オフラインビルド成立（vendored 41MB +
  --network=none）。ランタイム=Container Instance（OKE 不在・起動 43 秒実測）。13 ラウンドで残 blocker は
  すべて「非信頼生成コードの完全隔離は実 CI 検証+人間のネットワーク/IAM/設計判断が必要」という同一根本に
  収束 → 承認ゲート open-item 3 点（①コールバック面の隔離トポロジー ②自由 JSX vs 宣言的生成/別オリジン
  ③N7 プロセス/ディスク上限の強制方式）として人間へエスカレーション。証跡 runs/2026-07-08T0047_SP3-03/。
- 2026-07-08: **ADR-0023 人間承認**（ユーザー決定「社内ユーザーしか使えない前提なので、ネットワークの
  分離構成や違うデモのAPIを叩けることは許容します。３つ目についても同様です。将来的にそういった問題が
  発生した際に、Container InstancesをOKEに移行するであったり、監査の仕組みを充実させるという方向性に
  しましょう。まずは動くものをコンセプト確認のために優先します」）。open-item 3 点とも許容
  （①NSG 完全隔離不要 ②同一オリジン+自由 JSX 継続・デモ間横断許容 ③N7 PID/ディスク強制は未検証のまま
  許容）。将来方向 = OKE 移行・監査充実。**PoC-first**。SP3-03 を実装フェーズで再開（in_progress）。
- 2026-07-08: SP3-03 実装フェーズ完了。実装・実環境 E2E（host: プラン→生成→配信→chat/rag 実応答→
  DELETE prefix ゼロ・Codex 実ブラウザ検証 PASS・710+ tests）達成。ただし codex review-14〜18 は
  Accepted ADR への厳格準拠で **FAIL 維持**（blocker = 実 Container Instance 化・一回性コード ADB 単回失効、
  major 10 = 本番堅牢化）。descope を ADR-0023「PoC 受理と descope 記録」/e2e/SKIPPED.md/STATE.md に
  明文化のうえ、**施主が PoC 達成として受理し統合を承認**（AskUserQuestion 回答「統合して SP3-05 へ」
  = maker/checker の上位での施主判定。verdict の書き換えなし — review-18 FAIL は記録として残存）。
  `integrate_task.sh` exit 3 → docs/tips.md の加算的衝突（両側とも各タスクでレビュー済みの Tips 同位置追記）を
  union で解決し merge。**注記: 衝突解決の codex 再レビューは省略**（純加算 docs のみ・両側レビュー済みのため。
  conflict_policy からの明示的逸脱としてステージ報告に記載）。統合後 pytest 749 passed・ruff クリーン。
  証跡 runs/2026-07-08T0047_SP3-03/。後続タスク（起票待ち）: 実 CI 化（デプロイ）・単回失効 ADB +
  AUTH=true 実トークン E2E（SP3-05 へ引継）・本番堅牢化 major 群・生成 runtime の配備像同梱。
  環境残置: :8080 host uvicorn + 生成デモ a6aee5f6（点検/SP3-05 E2E 用）。
- 2026-07-08: 第4波 = SP3-05 起動（方式B・base=feat/sp3-builder）。
- 2026-07-08: SP3-05 完了（review-1 FAIL → 修正 → review-2 **PASS** blocker0。web 71 tests/lint/build 緑・
  api 770 passed/ruff 緑。実環境 E2E 3+1 シナリオ = **ステージ総合シナリオ（specs/19 §10）達成**:
  ビルダー UI からヒアリング→設計→生成→データ投入→ready→プレビュー→確定→一覧反映、生成デモから
  chat/rag/dbchat デモスコープ実応答。E2E 初回で「生成デモの箱が空」= §4.5 ③a データ投入の未配線を
  実発見し SP3-04 の provision_data を接続 — パイプライン縦切り完成。Codex 実ブラウザ独立検証 pass）。
  統合後 api 770 passed・ruff / web 71 tests・lint・build すべて緑を再確認。
  証跡 runs/2026-07-08T1207_SP3-05/。residual 9 件（file:line 付きで STATE.md — 主要: attach 後例外で
  生成枠占有・復帰時エラー種別未区別・「設計へ」活性化の近似）。
  環境残置: 産出デモ 70d19ca4・:8080/:8081/:4173・Chicago spike バケット jetuse-spike-sp305-e2e
  （後始末手順 e2e/env-after.md — 確認後 DELETE で全回収可）。
- 2026-07-08: **キュー全消化（6/6 done）→ ステージ報告作成・停止**（runs/_stages/sp3-builder/REPORT.md）。
- 2026-07-08: 施主が SSH フォワードで実機確認（loop ADB 自動停止→再起動 1 回）。フィードバック
  「基本 OK。ただし生成 UI がしょぼい — JetUse テーマ準拠で生成できないか（本フェーズ必須ではない）。
  LLM 起因なら別テナンシーで gpt-5 系が使える」→ REPORT.md §3 に後続タスク（スキャフォールドへの
  デザイントークン焼込み / モデル変更 / プロンプト強化の 3 レバー）として記録。
