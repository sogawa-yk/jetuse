# ADR-0022: Experience Builder（v2）を dev 上で MVP-first に進める

- 状態: **ドラフト（人間承認待ち）**
- 日付: 2026-06-30
- 関連: [プロダクトコンセプト](../architecture/jetuse-product-concept.md) / [実装方針](../architecture/experience-builder-implementation-strategy.md) / [初期構想](../architecture/ai-application-builder-vision.md)
- 起票: stage-runner / stage-0 / EXB-00

## コンテキスト

`main` は OCI 上で各 AI 機能（チャット / RAG / NL2SQL / Agent / OCR / 音声 等）が実動する安定版である。
次期 JetUse は、これらを **Capability として再利用**し、その上に**プリセールスエンジニアが顧客別の
Web / SaaS Experience を生成**する「Reference-Guided Experience Builder」を目指す（コンセプト §1、実装方針 §1）。

正本は[プロダクトコンセプト](../architecture/jetuse-product-concept.md)と[実装方針](../architecture/experience-builder-implementation-strategy.md)であり、[初期構想](../architecture/ai-application-builder-vision.md)は初期検討の記録（実装方針へ更新済み・参考）と位置づける。これらは次を強く規定する。

- 第一仮説は「非専門プリセールスが、main の実機検証済みリファレンス実装を使い、顧客が業務適合性を
  評価できるデモを完成できること」。**再利用率・作成速度・Capability 数は副次指標**（実装方針 冒頭）。
- 第一仮説が実証される前に、**汎用 Catalog / Resolver / Workflow Runtime / Hosted Runtime / Marketplace を
  作り込まない**（実装方針 §15 / コンセプト §15）。
- `main` の AI 実装は書き直さず、**Provider Adapter から再利用**する（実装方針 §3.5 / §12.2）。
- 次期実装は `main` から分岐した専用統合ブランチで進め、`main` へ逆流させない（実装方針 §4）。

## 決定

1. **ブランチ方針**: 実装方針 §4 の `next/experience-builder` を、本リポジトリでは **`dev`**（`main` 派生・
   ループ方法論を載せたブランチ）に読み替えて v2 統合の根とする。`main` は v1 安定版として直接開発しない。
   - 根（base）= `dev`。ステージ統合 = `feat/stage-<N>`（dev 分岐・隔離・自動 commit+merge・push しない）。
   - タスク worktree = `feat/<task>`。push / dev への PR / apply / IAM / 真の決定を伴う ADR 承認は人間ゲート。
2. **MVP スコープ**: 最初の実証は **引用付き RAG（`answer.with-citations@1`）の縦切り1本**に限定する。
   1 つの実 RAG（main 由来）を、型付き Action Contract と薄いクライアントで、制約された Redwood Experience
   から利用する。Slack Reference Integration は Gate 3 で縦に足す。
3. **作らない／汎用化しないもの（Gate 成立まで）**: 汎用 Catalog/Resolver サービス、Workflow Runtime、
   Agent/OCR/NL2SQL の網羅、Hosted Runtime、Slack 以外の SaaS、任意 SaaS Connector 動的生成、Dify 相当
   キャンバス、顧客セルフサービス Builder、生成 UI からの OCI SDK / 既存個別 API 直叩き。
   - **既存 Marketplace（ADR-0013 の plugin/registry 実装）は v1 機能として維持**し、削除・改変しない。
     v2 では Experience Template / Channel Adapter 等を流通させる**汎用化・拡張は行わない**（コンセプト §14
     「Marketplace は最初の MVP の中心ではない」）。v2 と既存 Marketplace の統合判断は Gate 成立後。
4. **再利用方針**: `main` の AI 実装（GenAI クライアント / RAG / NL2SQL / OCR / 音声 / Repository 等）は
   書き直さず Provider Adapter から委譲する。新設は主に外向け API・契約・Descriptor・SDK に限る。
5. **品質ゲート**: Build 成功だけを完成としない。実 AI 接続・代表シナリオ・引用・
   Streaming/Loading/Empty/Error/Retry・デモ台本・Preflight を確認した版だけを Demo Bundle として固定する
   （実装方針 §3.8）。各タスクは Codex レビュー PASS ＋ 実環境 E2E（または理由付き SKIPPED）を完了条件とする。

## 帰結

- v1（`main`）の既存デモは移行期間中も動作する（後方互換。回帰比較を行う）。
- ステージは「広く作る」のではなく「縦に薄く通す」ため、汎用基盤の早期構築を避けられる（YAGNI と整合）。
- Gate を満たさない場合は汎用基盤を拡張せず、Reference Descriptor / UI-UX / Channel Pattern / Quality Gate の
  改善を優先する（実装方針 §14）。
- 既知の技術的負債: semver 比較が `registry_client`（api）と `jetuse_registry`（registry）に二重実装で残る
  （別途判断）。本 ADR の範囲外。

## 未決・人間が承認すべき点

- 本方針（dev 根・MVP=RAG縦切り・汎用基盤を Gate まで作らない・main 再利用）の承認。
- ステージ進行（Stage 0→1→2→3）の着手承認。
