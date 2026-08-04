# FIX-47 実環境 E2E 結果（2026-07-13・jetuse:test / us-chicago-1・RM stack jetuse-spike-fix47）

環境: クリーンルーム（GenAI project 0 件を CLI で確認 = s1-projects-before.txt）。
IAM は人間作成の最小セット（既存 DG jetuse-deploy-test-dg + runtime policy 20 statements
= iam-report.md）。リージョンは大阪 VCN 枠超過のため ord へ切替（タスク文書の事前承認範囲）。

## シナリオ0: Issue #47 再現（旧公開イメージ jetuse-api:latest）

| 項目 | 結果 | 証跡 |
|---|---|---|
| POST /api/rag/files | **500 "Internal Server Error"（素・ヒント無し）= Issue #47 再現** | repro-rag-upload.* |
| 実例外 | DP `/openai/v1/files` が空 OpenAi-Project で 400 → 未処理 500 | repro-container-exception.log |
| GET /api/rag/health | 404（旧ビルドに存在しない = 旧イメージの証明） | repro-rag-health.status |
| STM 2ターン目 | **記憶喪失**（silent fallback = 監査どおり会話メモリ全滅） | repro-stm-turn2.sse |
| ステートレスチャット | 成功（空 project ヘッダはステートレス呼び出しでは許容 — 監査の精緻化） | repro-default-chat.sse |

## シナリオ1: クリーンルーム RAG E2E（修正イメージ・PROJECT_OCID 未指定）

| 項目 | 結果 | 証跡 |
|---|---|---|
| project 自動作成 | **jetuse-project が自動作成され ACTIVE**（health の project.source=auto、CLI で確認） | s1-project-created.txt |
| /api/rag/health | 3点 ok（project/CP/DP）。※作成直後は DP 伝播待ちで一時 ok=false（下記発見4） | s1-rag-health-recovered.json |
| POST /api/rag/files | **200** → 約40秒で索引化 completed | s1-rag-upload.* / s1-rag-files-indexed.json |
| RAG grounded 応答 | 文書固有の事実（テオ/深い藍色/莫山先生）を**出典付きで正答** | s1-rag-chat.sse/.txt |
| 既定モデルチャット | gpt-oss-120b 応答 OK | s1-default-chat.* |
| STM 2ターン | **turn2 が「抹茶ラテ」を正しく想起**（旧イメージの記憶喪失と対照） | s1-stm-turn1/2.sse |

## シナリオ2: 明示 PROJECT_OCID（stack 変数で自動作成 project を指定）

| 項目 | 結果 | 証跡 |
|---|---|---|
| /api/rag/health | 3点 ok・project.source=**env** | s2-rag-health.body |
| upload → 索引化 | 200 → completed | s2-rag-upload.* / s2-rag-files-indexed.json |
| 自動作成の非発動 | project 総数 **1 のまま** | s2-projects-after.txt |

## シナリオ3: ネガティブ（無効 PROJECT_OCID 注入）

| 項目 | 結果 | 証跡 |
|---|---|---|
| POST /api/rag/files | **503 + 原因ヒント**（500 を漏らさない） | s3-rag-upload.* |
| /api/rag/health | ok=false・**data_plane を失敗点として特定**（project=env ok / CP ok） | s3-rag-health.body |

## E2E 中の新発見（可搬性・別チケット/レポート送り）

1. ORM on-behalf-of 実行は `inspect tenancies` が無いと region_subscriptions が null → plan 不能（PORT-01）
2. prefix 15文字超で VCN dnsLabel 上限超過 → apply 失敗。schema に長さ検証なし（PORT-01）
3. **ADB の in-place rename（prefix 変更）で wallet リソースが再生成されず stale → DB 全断**。
   復旧はウォレット再生成+バケット上書き+CI 再起動（fix_wallet_and_restart.sh）。恒久修正=
   wallet リソースへ replace_triggered_by（PORT-01）。加えてアプリ側 /tmp ウォレットキャッシュが
   無検証で再フェッチしない（PORT-02 縮退不全リストへ）
4. 新規作成直後の GenerativeAiProject は DP から見えるまで**数分の伝播遅延**があり、その間 DP 404
   → 本修正の表面化により 503+ヒント+health 自己診断で運用可能（docs/tips.md 追記）
5. 人間作成の最小 IAM セット（jetuse-deploy-test-dg + 20 statements）で project 自動作成〜RAG まで
   **全経路動作を実証** = 公開スタックの iam モジュール statement 群（+ generative-ai-project）の十分性を確認
