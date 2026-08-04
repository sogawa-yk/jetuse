# レビュアー向けコンテキスト（tasks/FIX-47.md からの引用。diff の設計判断の根拠）

1. **project 自動作成はタスク仕様の明示要求**（tasks/FIX-47.md 作業内容 2）:
   「`settings.project_ocid` が空のとき…ACTIVE project を検索 → 無ければ `jetuse-<prefix>-project` を
   自動作成して採用。…自動作成に必要な policy（`manage generative-ai-project` 相当）を実測し
   **iam モジュールの statement へ追加**」。E2E シナリオ 1（受け入れ条件）も
   「PROJECT_OCID 未指定で deploy → **project 自動作成が発動** → …」を要求する。
   前回レビュー（review-1）の blocker 2 件を受け、次の opt-in 設計に変更済み:
   - アプリ: `PROJECT_AUTOCREATE`（settings 既定 **false** = 検出のみ・fail-fast）
   - iam モジュール: `enable_project_autocreate`（モジュール既定 **false** で statement 除外）
   - 公開 ORM スタック: `enable_project_autocreate`（スタック既定 true — ワンクリック要件。
     schema.yaml の IAM 設定グループでオフにでき、env とpolicy が連動）
   なお「IAM/テナンシ変更は人間ゲート」はこのリポジトリでエージェントが行う**運用操作**の制約で
   あり、顧客が自テナンシへデプロイする公開スタックの宣言的 IAM モジュール（既存で DG/Policy を
   作成する）のコード変更を禁じるものではない。実際の jetuse:test への apply は人間承認待ちで停止中。

2. **E2E は 2026-07-13 に全シナリオ実施済み**（RESULTS.md が総括・SKIPPED.md はスキップ無しを明記）:
   シナリオ0=Issue #47 のクリーンルーム再現（旧イメージで 500 + STM 記憶喪失）、
   シナリオ1=修正版で自動作成→RAG grounded 応答→STM 記憶保持、シナリオ2=明示 PROJECT_OCID
   （自動作成の非発動）、シナリオ3=ネガティブ（503+ヒント、health の失敗点特定）。
   実施環境は人間ゲート通過後の jetuse:test / us-chicago-1（大阪 VCN 枠超過のため。
   タスク文書の E2E 節が事前承認する代替リージョン）。IAM は施主が「これがあればユーザーは
   デプロイできる」最小一致セットとして定義した既存 DG jetuse-deploy-test-dg + 手動作成 policy
   （iam-report.md）で、スタック側は enable_dynamic_group=false / enable_runtime_policy=false
   （施主指示: apply に IAM を含めない）。
3. **旧 plan ログ（rm-plan-1/2, local-plan-test.log）は 2026-07-10 の権限切り分けの歴史的証跡**。
   現在は解消済み。apply 中の VCN dnsLabel 失敗・ADB rename ウォレット stale 等の発見は
   RESULTS.md「新発見」節と docs/tips.md に記録し PORT-01/PORT-02 スコープへ送った
   （FIX-47 の diff には含めない — スコープ規律）。
