# 実環境 E2E 証跡（Public版ワンクリックデプロイ）

## 実施範囲と承認

ユーザー指示（2026-07-28）により、**DEPLOYTEST プロファイル / `jetuse-test` コンパートメント**で
Public版（main）のワンクリックデプロイを検証した。テナンシ管理者アカウントを持つ利用者が
ボタンからデプロイできる状態を目標とする、という前提も同指示による。

- 作成先: DEPLOYTEST テナンシ（ホーム `ca-toronto-1`）/ `us-chicago-1` / コンパートメント `jetuse-test`
- 作成物: 公開スタックが作るもの一式（Dynamic Group 3 / Runtime Policy / root の namespace 参照 Policy /
  Identity Domain + OIDCアプリ + demoユーザー / VCN / ADB / Object Storage / Container Instance /
  Functions / API Gateway / Logging）。**既存リソースの変更・削除は行っていない**（対象は新規の専用
  コンパートメントのみ）。
- teardown: **完了済み**。全スタックを destroy → delete し、検証用 OCIR リポジトリと auth token も削除。
  コンパートメント `jetuse-test` に検証由来のリソースは残っていない（結果と残存確認は `teardown.txt`）。
  destroy 自体も本差分の検証対象（修正前は必ず失敗していた）。

## 秘匿値の扱い

本ディレクトリには**実パスワード・OCID・実エンドポイントを残さない**（リポジトリ規約）。
`e2e-results.json` は生成後にマスキングしてある。デプロイ出力（`app_url` / `demo_password` 等）は
検証者のローカルのみで扱い、コミットしない。

## 経路

1. `orm-main` リリースの `jetuse-orm.zip`（= READMEのデプロイボタンが指す実物）を ORM Stack として
   apply し、**不具合を再現**。
2. 修正後の作業ツリーから同じ手順で作った zip で新 Stack を apply して**再検証**。
   最終コードでは**まっさらな新規スタックを作成 → E2E → destroy** まで通した。
   コンテナ側の修正は、その作業ツリーからビルドしたイメージを検証用 OCIR へ push し、
   `api_image_url` / `fn_router_image` で**digest ではなく検証用タグ**を明示指定して配備した
   （タグとイメージ digest は `image-digests.txt`、対象ツリーの識別は `source-ref.txt`）。
3. ブラウザ検証は Chromium（Playwright）でログイン〜各機能を実操作。

`e2e-results.json` が最終の合否一覧（**38/38 PASS**、4xx/5xx レスポンス 0 件）。
詳細レポートは `docs/verification/PUBLIC-DEPLOY-E2E.md`。
