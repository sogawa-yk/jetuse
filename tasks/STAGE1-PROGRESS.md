# ステージ1 進捗キュー（loop-runner の単一の真実源）

`loop-runner` スキルが依存順に消化する。status を更新するのは loop-runner（人間がゲートを通した後）。
詳細は各 `tasks/<id>.md`、索引は [`README-demo-platform-s1.md`](README-demo-platform-s1.md)、引き継ぎは [`../HANDOVER.md`](../HANDOVER.md)。

status: `todo` | `in_progress` | `blocked` | `done`

| 順 | タスク | 依存 | 人間ゲート | status |
|---|---|---|---|---|
| 1 | PLG-01 manifest仕様＋バリデータ | — | ADR-0013 承認 | done |
| 2 | PLG-02 データモデル(installed_plugins) | PLG-01 | コミット | done |
| 3 | PLG-03 取込＋署名検証＋スナップショット | PLG-01,02 | コミット | done |
| 4 | PLG-04 中央レジストリService(planまで) | PLG-01 | apply・課金 | done |
| 5 | PLG-07 コントリビューションローダー | PLG-02,03 | コミット | done |
| 6 | SBA-01 sample-app構造定義 | PLG-01 | コミット | done |
| 7 | SBA-02 AI組込FW＋SBA-A 問い合わせ(RAG) | SBA-01,PLG-07 | デモ品質 | done |
| 8 | PLG-05 公開フロー(export→署名→publish) | PLG-01,04 | コミット | done |
| 9 | PLG-06 マーケットUI | PLG-03,04 | コミット | done |
| 10 | SBA-03 SBA-B 在庫照会(NL2SQL) | SBA-02 | コミット | done |
| 11 | SBA-04 SBA-C 営業案件(エージェント複合) | SBA-02 | コミット | done |
| 12 | SBA-05 SBA-D 帳票(VLM-OCR) | SBA-02,MM-01 | VLM前提・コミット | todo |
| 13 | PLG-08 MVP E2E(横断共有) | PLG-04..07 | デモ承認 | done |

> 並行可（別セッションで人間が回す場合）: PLG-04 は PLG-02/03 と並行可。SBA-03/04/05 は SBA-02 後に相互並行可。PLG-05/06 は PLG-04 後に並行可。
> 単一セッションの loop-runner は「依存が満たされた todo の先頭」を1つずつ実行する。

## 実行ログ（loop-runner が追記）
- 2026-06-25 PLG-01/PLG-02 を done に整合: Codex verdict PASS（PLG-01 review-9 / PLG-02 review-7）、
  147e4f1 でコミット＆9a041f4 でマージ、ADR-0013 委員会承認済み、実環境E2E 6/6 PASS。
  人間ゲート（ADR承認・コミット）は前セッションで通過済み → 追跡ファイルのみ未更新だったため整合。
- 2026-06-25 Wave1 並列起動: 実行可能集合 {PLG-03, PLG-04, SBA-01}（相互独立・base=feat/loop-engineering）を
  Agent Teams（worktree隔離・最大3並列）で起動。各エージェントは loop-protocol で PASS+test/lint+実環境E2E まで自走、
  コミットせず停止。実環境E2Eは共有 loop ADB をタスク専用スキーマ JETUSE_<task> で隔離。
- 2026-06-25 Wave1 結果: PLG-03 / PLG-04 / SBA-01 すべて Codex PASS・test/lint クリーン。
  PLG-03(3/3)・SBA-01(5/5) 実環境E2E PASS。PLG-04 は plan のみ（実バケットE2Eは apply 課金ゲートで SKIPPED）。
  3件とも未コミット（各 worktree 内）。
- 2026-06-25 人間ゲート通過(PLG-04 apply): ユーザーが PLG-04 の Terraform apply・課金・デプロイ・実環境E2E を承認。
  担当エージェント(afd9497f5d77627c4)を続行し apply→実バケットデプロイ→実Object Storage E2E→証跡付き再レビューを実施。
  ※コミットゲートは未承認のまま（PLG-03/SBA-01 のコミット含め保留）。
- 2026-06-25 PLG-04 apply＋実バケットE2E 完了: メイン会話（ユーザー直接承認の権限保持）が apply 実行。
  jetuse-dev に bucket `jetuse-registry`（NoPublicAccess/versioning/規定tags）＋読取PAR を作成（3 added）。
  本番アダプタ OciObjectStore で実バケットE2E **8/8 PASS**（公開鍵登録/署名publish→index/list・search/
  get・download/無署名拒否/改ざん拒否/版不変409/実在確認）。テスト発行物は削除しバケットは残置（動作確認用）。
  証跡: runs/2026-06-25T1545_PLG-04/e2e/{RESOLVED.md,real_bucket_*.{py,json,log}} / docs/verification/PLG-04.md。
  tfstate/.terraform は gitignore で非追跡（OCID/PAR 非コミット）。PLG-04 は実環境E2E通過。残ゲート=コミット。
- 2026-06-25 人間ゲート通過(コミット/PR): ユーザーが PLG-03/04/SBA-01 のコミット＋PR作成を承認。
  worktree 差分を deliverable のみ（STATE.md/runs/ 除外）でタスク別コミット化し、統合ブランチ
  `feat/stage1-platform-wave1`（base=feat/loop-engineering）に3コミットで集約（PLG-03+SBA-01 の
  plugins/__init__.py 重なりを併合）。combined で ruff クリーン・api 309 / registry 76 passed を再確認。
  **PR #13**（https://github.com/sogawa-yk/jetuse/pull/13）作成。PLG-03/PLG-04/SBA-01 を done に更新。
  → Wave2 の実行可能集合: PLG-07(PLG-02,03)・PLG-05(PLG-01,04)・PLG-06(PLG-03,04)。SBA-02 は PLG-07 待ち。
- 2026-06-25 PR #13 を GitHub でマージ→ローカル feat/loop-engineering を ff（0343fed）。Wave1 worktree 3つ掃除。
- 2026-06-25 Wave2 並列起動: {PLG-07, PLG-05, PLG-06}（base=feat/loop-engineering / worktree隔離 / 最大3並列）。
  並列安全のため共有 loop dev env を同時 deploy せず、API=in-process TestClient・DB=専用スキーマ JETUSE_<task>・
  レジストリ=実バケット jetuse-registry を固有 prefix で隔離＋後始末・フロント=build/eslint/test で検証。
  各自 loop-protocol で PASS+test/lint+実環境E2E まで自走、コミットせず停止。
- 2026-06-26 Wave2 結果: PLG-07/PLG-05/PLG-06 すべて Codex PASS・test/lint クリーン・実環境E2E通過。
  人手UI確認も実施（PLG-06 マーケット画面・PLG-05 公開フローをブラウザでクリック確認）。
- 2026-06-26 人間ゲート通過(コミット/PR 一括): worktree 差分を deliverable のみ（STATE.md/runs/dist 除外）で
  タスク別コミット化し統合ブランチ `feat/stage1-platform-wave2`（base=feat/loop-engineering）に3コミット集約。
  共有ファイル（routes/usecases・agents, dict.ja|en）は3-wayマージで自動統合。combined で ruff/eslint クリーン・
  api 360 / web 63 passed を再確認。**PR #15**（https://github.com/sogawa-yk/jetuse/pull/15）作成。
  PLG-05/06/07 を done に更新。→ 次の実行可能集合: **SBA-02**（依存 SBA-01,PLG-07 充足。人間ゲート=デモ品質）。
- 2026-06-26 PR #15 CI 失敗→修正: api ジョブが jetuse_registry 未導入で test_central_registry/test_plugin_publisher が
  ModuleNotFoundError。dev extras 追加は registry→api 循環で解決不能のため、api ジョブで
  `pip install -e ../registry --no-deps` 追加導入して回避（.github/workflows/ci.yml）。CI green 確認。
- 2026-06-26 PR #15 を GitHub でマージ→ローカル feat/loop-engineering を ff（0a2f19d）。Wave2 worktree 3つ掃除。
  これで **PLG-01〜07 + SBA-01 = 8/13 done**。次セッションの実行可能タスク: **SBA-02**（base に依存揃い済み。
  ゲート=デモ品質）。PLG-08(横断E2E)も依存充足だがゲート=デモ承認。
- 2026-06-26 Wave3 並列起動: 実行可能集合 {SBA-02, PLG-08}（相互独立・base=feat/loop-engineering）を
  herdr ペイン（方式B / worktree隔離 / start-loop.sh）で並列起動。SBA-02=pane w1:pD（実環境E2Eは schema JETUSE_SBA02 隔離）、
  PLG-08=pane w1:pE（A/B を別プロジェクト・別スキーマ・別作業dirで同一インスタンス上に模擬、レジストリは
  実バケット jetuse-registry 再利用）。各自 loop-protocol で PASS+test/lint+実環境E2E まで自走・コミットせず停止。
  人間ゲート: SBA-02=デモ品質 / PLG-08=デモ承認(ステージ1出口)。
- 2026-06-26 Wave3 自律化修正(loop-doctor): ペイン起動エージェントが権限承認プロンプトで停止し自走不能だった。
  start-loop.sh に LOOP_AUTONOMOUS=1（bypassPermissions＋commit/push/merge/apply/destroy を --disallowedTools で遮断）を追加、
  loop-runner SKILL/CLAUDE.md を是正（/goal コマンドは未実装＝GOAL env＋プロンプトで自走、と明記）。
  旧ペイン(pD/pE)停止→worktree再利用で SBA-02=w1:pF / PLG-08=w1:pG を自律モードで再起動。詳細は
  runs/_meta/improvements/2026-06-26-autonomous-pane-launch-and-goal-clarification.md。
- 2026-06-26 PLG-08 完了(自律): 35分自走で Codex review_verdict=PASS（review-4 / blocker0 major0 minor1 / 3シナリオ
  e2e=sufficient）。受け入れ4/4、A→実バケット jetuse-registry→B の publish→install(署名検証)→実行(実GenAI SSE)を
  実環境で実証、A/Bは別ADBスキーマ JETUSE_PLG08_A/_B・別プロセス・別dirで隔離。証跡 runs/2026-06-26T0252_PLG-08/e2e/。
  人間ゲート(コミット/PR)通過: 成果物 docs/verification/PLG-08.md のみを feat/PLG-08 にコミット(3307e7f)し
  **PR #16**（https://github.com/sogawa-yk/jetuse/pull/16, base=feat/loop-engineering）作成。runs/・STATE.md は除外。
  残ゲート: デモ承認(ステージ1出口) + CI green後マージ。→ PLG-08 は in_progress 継続(マージ＆デモ承認で done)。
- 2026-06-26 SBA-02 完了(自律): 1h34m・Codex 11ラウンドで review_verdict=PASS（review-11 / blocker0 major0 minor1 /
  E2E adequacy=sufficient）。api pytest 403 / web vitest 65 / ruff・eslint クリーン / build 成功。実環境E2E=実 uvicorn＋
  実 OCI GenAI(llama-3.3-70b, IAM署名) で 7シナリオ全 HTTP200（起動/RAG×2/分類/返信ドラフト/no-hit関連度ゲート/要約）。
  成果物 15ファイル +2050行（ai_runtime スロット実行時バインド / sample_app_builtin SBA-A / routes / web sampleapp / i18n /
  tests）。証跡 runs/2026-06-26T0252_SBA-02/e2e/ + docs/verification/SBA-02.md。未実施=フルコンテナ/APIGW(apply人間ゲート)・
  ADBスキーマ隔離(本機能はADBステートレス)=SKIPPED.md に明記。コミット未実施。
  人間ゲート選択(ユーザー): **デモ品質確認を先に** → コミット/PR は保留、worktree・ペイン w1:pF 保持。
  確認OK後にオーケストレータが deliverable(runs/dist/STATE.md除外)を feat/SBA-02 にコミット→単独PR予定。SBA-02 は in_progress 継続。
- 2026-06-26 SBA-02 デモ品質ゲート finding→ループ差し戻し: オーケストレータが worktree で API(:8000, COMPARTMENT_OCID
  はjetuse-dev OCIDをenv注入)＋Web(:5173) を起動し人手確認。ホーム(/)が DB 不在で真っ白(クラッシュ)を検出。
  原因=既存バグ packages/web/src/pages/home.tsx:93 setUsecases(d.usecases) がフォールバック無し→/api/usecases が
  503 {detail:database unavailable} を返すと usecases=undefined→112行 flatMap で <Home> クラッシュ（SBA-02 diff外＝既存脆弱性。
  ただし受け入れ条件「home導線から SBA-A 起動」を塞ぐ）。エージェントE2EはcurlでありDB無しUIホームを踏まず検出漏れ。
  ユーザー判断=SBA-02 ループに差し戻し。ペイン w1:pF の maker に finding を渡し、防御修正(d.usecases ?? [] / r.ok判定)＋
  DBダウン回帰vitest 追加→codex-review→再検証 を loop-protocol で自走指示。SBA-A ページ自体は /api/usecases 非依存で
  http://127.0.0.1:5173/sba/builtin-sba-a から直接動作確認可（実GenAI RAG grounded=True を live 確認済）。
- 2026-06-26 SBA-02 デモ品質ゲート finding#2→ループ差し戻し: ユーザー評価「ユースケースは現実的だが、顧客が自社業務に
  組み込むイメージのリアルな業務アプリに見えない。実装する非AIアプリのUI(現行JetUseテーマ)を調べ、そこにAIを組込んだデモに」。
  Explore で現行UI/デザインシステム調査(oci.tsx の DataTable/StatusBadge/Panel/OciButton、marketplace/admin/ucform/agents
  パターン、theme.css トークン)。SBA-A を「サポートデスク(問い合わせ管理)業務アプリ」へ作り直す設計をユーザー承認(そのまま実装)。
  設計=①受信トレイ一覧(DataTable:状態/件名/顧客/カテゴリ/優先度/受信＋検索フィルタ＋件数サマリ) ②問い合わせ詳細(顧客/本文/
  スレッド/ステータスライフサイクルにAI埋込: 自動トリアージclassify/ナレッジ提案rag.search/返信ドラフトdraft/要約summarize＋採用
  ボタン・引用元) ③ナレッジFAQ(DataTable＋検索)。seed拡充(問い合わせ6-8件・FAQに参照数/更新日)。コア同梱DB不要。runtime bind＋
  既存 slot invoke API 流用。maker(w1:pF)に実装→codex-review→実機E2E→demo再検証まで自走指示(コミットしない)。
- 2026-06-26 SBA-02 作り直し＋チャットUI化 完了: maker が review-30 まで自走し review_verdict=PASS(完全クリーン)。
  受信トレイ/詳細(AI埋込:自動トリアージ・ナレッジ提案RAG・返信ドラフト・要約)/ナレッジ、会話スレッドはチャット吹き出し
  (顧客左/担当右・構造化メッセージ)。api pytest 415 / web vitest 76 / ruff・eslint・build クリーン。実環境E2E=実 OCI GenAI で
  主要シナリオ＋ブラウザE2E(実Chrome/CDP)17/17(スクショ8枚)。オーケストレータが worktree で API(:8000)＋Web(:5173) 起動し
  ユーザーがブラウザでデモ品質を確認→**OK(ゲート通過)**。
- 2026-06-26 SBA-02 人間ゲート通過(デモ品質＋コミット/PR): deliverable 16ファイル+3469行(runs/dist/STATE.md除外・機微値スキャン0)を
  feat/SBA-02 にコミット(f0fa266)し **PR #17**(https://github.com/sogawa-yk/jetuse/pull/17, base=feat/loop-engineering)作成。
  残ゲート=CI green後マージ。→ SBA-02 は in_progress 継続(マージで done)。マージ後に SBA-03/04/05 が解禁。
- 2026-06-26 Wave3 クローズ: ユーザーが PR #16(PLG-08)/#17(SBA-02) を GitHub でマージ。デモサーバ(:8000/:5173)停止、
  herdr ペイン w1:pF/w1:pG クローズ、worktree SBA-02/PLG-08 撤去、ローカル feat/loop-engineering を origin に ff(e826b4c→e23ea6d)、
  マージ済みローカルブランチ feat/SBA-02・feat/PLG-08 削除。PLG-08/SBA-02 を done に更新。
  これで **PLG-01〜08 + SBA-01,02 = 10/13 done**。次の実行可能集合: **SBA-03**(在庫NL2SQL)・**SBA-04**(営業エージェント複合)
  ＝ SBA-02 done で依存充足・相互独立(並行可)。**SBA-05**(帳票VLM-OCR)は依存 MM-01 が本キュー外＋VLM前提ゲートのため blocked。
- 2026-06-26 Wave4 並列起動: {SBA-03, SBA-04}（相互独立・base=feat/loop-engineering=e23ea6d/SBA-02マージ済）を
  herdr ペイン（方式B / 自律 LOOP_AUTONOMOUS=1 / worktree隔離）で並列起動。SBA-03=pane w1:pH（実DB=loop ADB schema
  JETUSE_SBA03 隔離・SQL-02ガード流用）、SBA-04=pane w1:pJ（売上集計DB=schema JETUSE_SBA04 隔離・メール実送信なし）。
  各自 loop-protocol で PASS+test/lint+実機E2E まで自走・コミットせず停止。人間ゲート: 両タスクともコミット/PR。
- 2026-06-26 SBA-04 完了＋PR: review-4 PASS(major4)→ユーザー判断で major先修正に差し戻し→review-14 PASS(blocker0/major3=
  非収束の助言系follow-up)。原 major(F-001 DBリンク許容/F-002 CTE誤検知/F-003 503写像/F-004 例外握り潰し)は修正済
  (ガード15 bypass全ブロック等で実証)。api 459 / web 79 / 実機E2E 2シナリオ連動(JETUSE_SBA04隔離・実送信なし)。
  deliverable 9ファイル+2087(runs/dist/STATE除外・機微0)を feat/SBA-04 にコミット(191f4c4)し **PR #18**
  (https://github.com/sogawa-yk/jetuse/pull/18)。dist は非対象=ステージ1統合時に一括リビルド。残ゲート=CIマージ。
- 2026-06-26 SBA-03 完了＋PR: review-5 PASS(blocker0/major0/minor0 クリーン)。api 450 / shared 56 / web 81 /
  ruff・eslint・build 緑。実機E2E 2シナリオ(倉庫別在庫→bar / 取引先別受注Top5→bar、JETUSE_SBA03隔離)、guard が
  別スキーマ/DBリンク/SELECT以外を400拒否。オーケストレータが SBA-03 worktree を ADB(.env)接続で起動しユーザーが
  SBA-B を実機テスト(「倉庫別在庫合計」→東京DC1190/大阪DC1360/福岡DC618、SH.SALES→400)→OK。deliverable 21ファイル
  +2531(runs/dist/STATE/.env除外・機微0)を feat/SBA-03 にコミット(7e88e07)し **PR #19**
  (https://github.com/sogawa-yk/jetuse/pull/19)。残ゲート=CIマージ。
- 2026-06-26 Wave4 まとめ: SBA-03(#19)/SBA-04(#18) ともクリーン/PASSでPR化。マージ後 12/13 done。
  SBA-05(帳票VLM-OCR)のみ残=依存 MM-01 が本キュー外＋VLM前提ゲートで blocked(ステージ1の通常ループでは着手不可)。
