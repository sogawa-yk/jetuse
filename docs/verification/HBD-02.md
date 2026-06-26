# HBD-02 検証レポート — ダイアログ式ヒアリングUI

- タスク: `tasks/HBD-02.md`
- area: web（＋ HBD-01 hearing API への疎通）
- base: `feat/stage-2`（HBD-01 hearing API 統合済）
- run: `runs/2026-06-26T1821_HBD-02/`

## 1. 実装サマリ

スタンダードモードのダイアログ式ヒアリングUIを `packages/web/src/pages/hearing.tsx` に新設。
ルート `/hearing`（別名 `/builder/standard`）を追加し、左ナビ「スタンダード構築」から到達できる。

フロー（`docs/enhance/202607-hearing-flow.md` §2）:
1. **入力ステップ**: ヒアリングメモを貼付 → `POST /api/hearing/sessions` でセッション作成 →
   `POST .../suggest` で GenAI が Q1..Q6 のデフォルトを**提案**（`genai_suggested` で保存、SA は確認・修正のみ）。
2. **順次 Q&A**: `GET /api/hearing/questions` の質問スキーマ（Q1..Q6、auto 除外）を1問ずつ表示。
   single/multi の選択を `PUT .../answers/{qid}`（`source=sa`）で保存。進捗 n/6・ステップドット表示。
   分岐（§3 例: Q2「業務DB」×Q3「集計分析」→ SBA-B 格上げ）は Q3 画面で予告ノートを表示。
3. **推薦提示**: 「確定して推薦」→ `POST .../recommend`。主SBA＋AI部品＋コネクタ＋UI＋シード＋
   **決定ルールの根拠（rationale）**＋合成バリデーション注意を画面提示（ブラックボックス化しない）。
   「この構成で確定」→ `POST .../recommend/confirm`。
4. **再開**: URL `?sid=` で `GET .../sessions/{sid}` を取得し、回答・推薦を復元（途中離脱→再開）。
   自作セッションの sid を URL に載せても再フェッチで回答を上書きしないようガード（`loadedSidRef`）。

非ゴール（HBD-03 構成生成・HBD-04 合成バリデーション）は対象外。提示する選択肢は API の質問スキーマに限定。

## 2. 静的検証（packages/web）

| 項目 | コマンド | 結果 |
|---|---|---|
| 単体/UIテスト | `npm --prefix packages/web run test` | **94 passed**（14 files。新規 `hearing.ui.test.tsx` 10件含む） |
| Lint | `npm --prefix packages/web run lint` | **clean**（0 errors） |
| ビルド | `npm --prefix packages/web run build` | **成功**（tsc -b + vite build） |
| i18n キー整合 | `dict.test.ts` | ja/en キー集合一致・空値なし |

`hearing.ui.test.tsx` の網羅（mock fetch、主要分岐）:
- 入力ステップ: メモ→AI提案で Q1 にデフォルト選択＋「AI提案」バッジ・進捗表示。
- 順次 Q&A＋分岐: Q2 業務DB×Q3 NL2SQL → SBA-B 格上げノート → 確定 → 推薦提示 → 回答保存疎通 → 確定。
- 再開: `?sid=` で回答・既存推薦を復元。
- エラー: recommend 422（未回答）を Q&A ステップに留めて表示。

## 3. 実環境 E2E（jetuse-dev / 固定 loop ADB 再利用）

委譲証跡は `runs/2026-06-26T1821_HBD-02/e2e/`。**コミット対象の証跡**は各シナリオの JSON と
`SKIPPED.md`（本 diff に含む）。`deploy.log`（再現手順）はリポジトリ方針で `*.log` が gitignore
されるためローカル保持（`.gitignore` 参照。Codex レビューには `run_codex_review.sh` が添付する）。
ヒアリングUI が呼ぶ API シーケンスを **実 `jetuse-loop-adb`（スキーマ `JETUSE_HBD02`）** へ往復させ、
FastAPI（`hearing.router`）→ 実 oracledb → 実テーブルで永続を実証した。

| シナリオ | 内容 | 結果（証跡） |
|---|---|---|
| 1 | サポート×文書×RAG-QA → **SBA-A** 推薦・確定・実 ADB 永続（6回答）・再開復元 | PASS `scenario-1.json` |
| 2 | 業務DB×集計分析 → 主役 **SBA-B 格上げ**＋途中離脱→再開で全回答復元 | PASS `scenario-2.json` |
| 3 | 実 GenAI でメモ→Q1..Q6 デフォルト提案を抽出・`genai_suggested` 保存（入力ステップ） | PASS `scenario-3-suggest.json` |
| 4 | AI提案をそのまま採用 → `source` 昇格（`genai_suggested`→`sa`）→ 推薦・確定 | PASS `scenario-4-promotion.json` |
| 5 | 所有権境界: 別ユーザーの `sid` は GET/PUT/recommend とも **404**（`owner_sub` 強制）、本人は 200 | PASS `scenario-5-ownership.json` |
| 6 | Q1=その他 → 主SBA未確定（`sample_app=null`）・最近傍提案・confirm は **409** で拒否 | PASS `scenario-6-other.json` |

> 再開 `?sid=` の認可は API（HBD-01）が `owner_sub` で強制する（`get_session(user.subject, sid)` →
> 他者/不正な sid は 404）。UI は 404 を握りつぶさず表示し、他者の回答・推薦を復元しない。
> 別 `sid` へ遷移する際は表示状態を初期化し、失敗時に旧セッションが残らない（stale state 防止）。
> Q1=その他で `sample_app` が null のときは確定ボタンを無効化し、最近傍提案を参考に Q1 修正へ誘導する
> （API の 409 を画面で行き止まりにしない）。以上は `hearing.ui.test.tsx`＋ scenario-5/6 で実証。

- 実 ADB 行で確認: `hearing_answer` に Q1..Q6 が `source=sa` で永続、`recommendation` に
  `sample_app`/`ui`/`seed_strategy` と `confirmed_at`、`hearing_session.status=confirmed`。
- 分岐の実証: Q1=support のまま Q2=[business_db]×Q3=nl2sql で `sample_app=SBA-B`（決定ルール格上げ）。
- 非実施: ブラウザ実機の SPA フルデプロイ（`.env`/loop.tfvars 不在＋terraform apply 人間ゲート）。
  UI 描画/操作分岐は vitest（jsdom）で担保。理由は `SKIPPED.md` 参照。

### E2E 実行で使った jetuse-dev リソースの承認スコープ
- E2E は `jetuse-loop-adb`（再利用 loop ADB）に対し admin パスワードの都度リセットと
  タスク専用スキーマ `JETUSE_HBD02` の作成を行った。これは CLAUDE.md「jetuse-dev 内の開発用
  実リソースの作成・変更・削除（ユーザー承認済み 2026-06-25）」＋ memory `loop-e2e-adb-jetuse-dev`
  の**承認済み運用**であり、CLAUDE.md「参照のみ」の保護対象（VCN develop / インスタンス dev /
  バケット）には**該当しない**（詳細は `deploy.log`「承認スコープ」節）。

## 4. 残る人間ゲート

- コミット / PR / push（未実施）。
- IAM/テナンシ変更、保護対象の既存リソース（VCN develop / インスタンス dev / バケット）変更
  （本タスクでは未着手）。jetuse-loop-adb / JETUSE_HBD02 への E2E 操作は承認済みスコープ内。
