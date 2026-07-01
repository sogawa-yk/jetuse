# ADR-0022: Experience Builder（v2）を dev 上で MVP-first に進める

- 状態: **Accepted（施主承認 2026-07-01）**。§4 は施主指摘を反映して更新済み（AI ロジックは再利用・API 層は
  新設統合 API・MVP はアダプタ・後続で統合 API へ寄せるリファクタリングフェーズを明示）。
- 日付: 2026-06-30（起票）／ 2026-07-01（承認）
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
4. **再利用と API 統合方針**（施主指摘で明確化 2026-07-01）: 「書き直さない」対象は **AI ロジック**であり、
   **API 層はむしろ新設の統合 API に寄せる**。両者を区別する。
   - **AI ロジックは書き直さない**: `main` の GenAI クライアント / RAG / NL2SQL / OCR / 音声 / Repository は
     Provider Adapter から `jetuse_core` を委譲再利用する（実装方針 §3.5）。実機検証済みの中身を壊さない。
   - **API 層は新設の統合 API**: Action / Run API・契約・Descriptor・SDK を新しい **JetUse API** として作る
     （実装方針 §3.1「共通化するのは実行契約」）。これが将来の**唯一の実行入口**になる。
   - **MVP**: まず Provider Adapter で RAG 縦切り1本を速く通す。既存 UI は当面 `main` の画面別ルート
     （`/api/chat` 等）のまま**並存**させる（実装方針 §12.4）。
   - **後続フェーズ（縦切りが安定後・明示的に挟むリファクタリング）**: Action / Run API を
     **canonical な JetUse API に昇格**させ、**既存 UI もそれ経由に移行**、画面別の旧個別ルートは段階的に廃止する。
     ＝アダプタは MVP の踏み台であり、「既存 UI とビルダー生成 UI が**同じ統合 API を呼ぶ**」形へ寄せる
     フェーズを計画に含める（旧ルート廃止は Builder 生成 UI が必要機能を網羅した後に判断・実装方針 §12.4）。
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

## 承認記録

- 2026-07-01 施主承認: 本方針（dev 根・MVP=引用付きRAG縦切り1本・汎用基盤を Gate まで作らない・既存
  Marketplace は v1 維持・main の AI ロジック再利用）を承認。§4 は「AI ロジックは書き直さず、API 層は
  新設統合 API に寄せる。MVP はアダプタ、後続で Action/Run API を canonical 化し既存 UI も移行する
  リファクタリングフェーズを明示」に更新のうえ承認（ロードマップ Stage 4 に反映）。
- これにより stage-0 を `dev` へ統合（PR）してよい。Stage 1（EXB-03/04/05）着手も承認。
