# SEC-03 検証レポート: IP制限・レート制限

- 日付: 2026-06-13 / ブランチ: `task/sec-03` / 仕様: specs/15-hardening.md
- 比較ドキュメント: `docs/comparison/access-control.md`

## 実装

- **API Gatewayレート制限ポリシー**をデプロイメント仕様に追加（`rate_limiting`、送信元IP単位）
  - `rate_limit_rps`（dev=20、0で無効）/ `rate_limit_key`（既定CLIENT_IP）をTerraform変数化
  - GWデプロイメントの更新のみ（CI再作成なし）で適用
- IP制限はNSG ingress CIDR制限を本番化時の方針として文書化（WAFは比較のみ）

## 実機検証（2026-06-13）

- 逐次30連射: 全て通常応答（curlのTLS往復で20req/s未満のため未発火 — 想定どおり）
- **60並列バースト: 24/60が429**（閾値超過分がレート制限される）→ ✅ 発火確認
- 認証(401)・正常系への影響なし（429は超過分のみ）

## 判断

- 社内利用想定で**送信元IP単位20 req/s**を既定値とした。単一NAT経由の場合は値の引き上げ or
  `TOTAL`キーへの切替で対応（comparison参照）
- 認証(OIDC/JWT)で実質アクセス制御済みのため、IP制限の本実装は本番要件化時にNSGで対応
