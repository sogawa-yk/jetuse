# 検証レポート: ORMワンクリックの実apply (jetuse-dev)

- 対象: `infra/orm/` ワンクリックスタック(INFRA-03)
- 環境: `jetuse-dev` コンパートメント(yuki.sogawa配下) / ap-osaka-1
- 日付: 2026-06-23〜24
- 方式: `terraform apply` で実デプロイ → Playwright で OIDCログインE2E → 全レイヤAPIを実機確認
- イメージ: 公開GHCRの代わりに OCIR public(`kix.ocir.io/idqcucnenh88/jetuse-orm-{api,fn-router}`)で検証
- IAM(動的グループ/ポリシー)はテナンシ権限が要るためユーザーが手動作成(plan.md INFRA-03 想定どおり)

## 結果: 全機能 実機E2E 成功 ✅

| 確認 | 経路 | 結果 |
|---|---|---|
| OIDC PKCE ログイン(demoユーザー) | SPA→Identity Domain→復帰 | ✅ |
| `/api/me` (トークン検証) | CI | 200 / subject=demo |
| チャット生成(gpt-oss-120b) | CI + GenAI(Responses API) | 200 / ストリーム生成 |
| `/api/db/datasets`・`/api/usecases` | CI + ADB | 200(usecase 5件) |
| `/api/presets` | **Functions** + ADB | 200 |
| `/api/dbchat/select-ai-models` | **Functions** | 200(Llama/Cohere) |
| SPA配信・`config.json`(実client_id) | Object Storage/GW | ✅ |

→ OIDCログイン〜CI/Functions両系統〜ADB〜GenAIまで、デプロイ済みアプリで一通り動作。

## 実applyで判明し修正した不具合(すべてボタン用に恒久化)

1. **署名証明書(JWKS)が非公開** — 新規Identity Domainは既定で `/admin/v1/SigningCert/jwk` が401になり、
   APIのJWT検証が失敗。`oci_identity_domains_setting.signing_cert_public_access=true` をTerraform化
   (`modules/identity-domain-app`)。
2. **ADBウォレット取得** — RP生成は権限の機微で不安定。Terraformがウォレットを**base64テキスト**で
   バケットへ配置し、コンテナはobject readで取得・デコード(`db.py`/`spa.tf`/`settings.adb_wallet_base64`)。
3. **Responses APIのcompartment** — `CompartmentId` ヘッダだけだと Responses API が
   400 "Compartment ID must be provided"。**`opc-compartment-id` も併送**するよう `genai.py` を修正
   (Chat Completionsは従来ヘッダ。既存dev環境にも潜在した不具合)。
4. **API Gateway→Functions 呼び出し権限** — IAMに
   `Allow any-user to use functions-family ... where request.principal.type='apigateway'` を追加
   (無いと Functions ルートが500)。`modules/iam` に恒久化。
5. **ADBウォレット用権限** — `read` では不足のため `use autonomous-database-family` へ(`modules/iam`)。
6. **起動UX** — bootstrap失敗時にAPIが最大25分起動しない件を、bootstrapを**バックグラウンド化**し
   uvicorn即起動へ(`entrypoint.sh`)。DB系は準備完了まで503でフェイルセーフ。

## メモ
- 検証中に ADB が**夜間自動停止**(テナンシ運用)し一時的にDB系が503 → 手動起動で復旧。
  ボタンの新規デプロイ時はADB稼働状態のため影響なし。
- 公開GHCRイメージは `main` への CI(release.yml)で生成される。検証では同等の公開OCIRで代替した。
- 検証用リソースは `jetuse-dev` に prefix `jetuse` で作成(使い捨て)。
