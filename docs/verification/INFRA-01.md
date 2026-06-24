# INFRA-01 検証レポート: Terraformモジュール群

日付: 2026-06-10（apply検証まで完了）
仕様: specs/02-infra.md
状態: **完了**（applyはユーザー承認済み。IAMモジュールのみ権限不足で人間手順書化）

## 実行結果サマリ

| チェック | 結果 |
|---|---|
| `terraform fmt` / `validate` | クリーン |
| `terraform plan` | エラーなし（当初26リソース） |
| `terraform apply`（人間承認済み） | **23リソース作成成功**。IAM3リソース（動的グループ2+ポリシー1）は `404-NotAuthorizedOrNotFound` — エージェントユーザーにテナンシ権限なし → `enable_iam=false` を既定化し `docs/setup/iam.md`（人間作業）に切り出し |
| 静的配信実測（ADR-0004） | `GET /` → **200 text/html**、CSS/JS → **200**（Content-Type正答）。API GW `/{object*}` → PAR URL `$${request.path[object]}` マッピング成立 |
| 冪等性 | apply後 `plan` 無差分（detailed-exitcode=0）→ **destroy 23削除 → 再apply 23作成 → plan無差分**。フルサイクル成立 |

## 実測で確定した仕様（ADR-0004の検証事項の結論）

1. **方式A（非公開バケット+読取PAR）成立**: API GWのHTTPバックエンドURLにPAR基底 + `$${request.path[object]}` で正しくオブジェクトが返る
2. **Content-Typeはオブジェクトメタデータがそのまま返る** → アップロード時に `--content-type` 指定必須（デプロイスクリプトの要件。Cache-Controlも同様にメタデータで付与可能）
3. **SPAディープリンクは404**（PARのObjectRead挙動）。フォールバックはAPP-02で実ルート確定後に決定（候補: ルート列挙 / ハッシュルーティング）
4. **PARの `bucket_listing_action="Deny"` 明示は禁止**: APIが値を返さず毎applyで再作成（=URL変化→デプロイメント更新）になる。未指定（=リスト不可）とすることで冪等化。実測でハマったため module にコメント明記
5. ADB(ECPU 2, 20GB)作成 約1m40s、API GW作成 約2m、destroyはADBが支配的（約2分）

## 現在のdev環境（稼働中・課金対象はADB）

- VCN 10.1.0.0/16 ほかネットワーク一式 / バケット3（spaにはSPIKE-07ギャラリーのビルドを配置済み）/ ADB `jetusedev` / OCIR `jetuse-dev-api` / Functions App `jetuse-dev-fnapp` / API GW（ホスト名は `terraform output apigw_hostname`。**再作成のたびに変わる**点に注意）
- Container Instanceは `api_image_url` 未設定のため未作成（APP-01イメージのOCIR push後に有効化）

## 残課題（後続タスク）

- [ ] 人間: `docs/setup/iam.md` の動的グループ+ポリシー作成 → リソースプリンシパル検証
- [ ] APP-01イメージのコンテナ化→OCIR push→ `api_image_url` 設定→CI+チャットルート有効化（SSE経路のAPI GW疎通確認）
- [ ] SPAディープリンクのフォールバック方式（APP-02）
