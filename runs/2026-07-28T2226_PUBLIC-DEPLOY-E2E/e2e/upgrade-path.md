# 既存Stack（旧実装）→ 修正版へのアップグレード経路の実機検証

Codex レビュー F001（旧実装で作られた既存 Stack を更新すると、UserPasswordChanger が
同一パスワードを `pwdpolicyViolation` で拒否して apply が失敗する）に対する実環境確認。

## 手順

1. **旧実装（`orm-main` リリースの zip）で作った最初の Stack** をそのまま残しておく。
   このStackの demo ユーザーは「SCIM の User へ直接 password を書いた」状態＝`mustChange=true` で、
   出力のパスワードではログインできない（＝報告された不具合そのもの）。
2. その Stack の config-source だけを**修正版 zip** に差し替えて apply（変数は変更しない）。
3. 出力の `demo_password` でログインし、パスワード変更を強制されないことを確認。

## 結果

- apply: **SUCCEEDED**。plan では
  `random_password.demo must be replaced` →
  `module.identity_domain_app[0].terraform_data.demo_password will be created` となり、
  local-exec の UserPasswordChanger が成功（`pwdpolicyViolation` は発生せず）。
- `demo_password` は**ローテートされた**（旧値 → 新値）。これは意図した挙動で、
  `random_password.demo` の `keepers` を進めることで実現している。値を変えないと
  ①履歴違反で apply が落ちる ②仮に通っても `mustChange` が外れずログインできないまま、の二重で詰む。
- 新しい `demo_password` でログイン →
  - `pwdmustchange` へリダイレクトされない
  - SPA が描画される
  - `GET /api/me` が `{"subject":"demo","is_admin":true}`（`ADMIN_USERS` 配線も更新で有効化）

## 補足（パスワード履歴違反の扱い・最終実装）

local-exec は 3 回まで再試行するが、`pwdpolicyViolation` は**試行回数にかかわらず失敗扱い**にする。
「mustChange=false」も「直前の試行が別の理由で失敗した」も、要求した値が**現在の**パスワードで
ある証明にはならず、成功扱いにすると出力の `demo_password` でログインできない状態を隠すため。

- 通常運用ではこの分岐に入らない（`keepers` により毎回新しいパスワードを発行するため）。
- 入った場合（PUT がサーバー側で成功して応答だけ失われた等）は、スタック変数
  `demo_password_version` を変えて再 apply すればパスワードが再発行され復旧できる。
  エラーメッセージにもこの手順を出す。
- **この応答喪失経路は実環境では再現していない**（意図的に起こせないため）。実機で確認したのは
  正常系（新規作成・旧実装からの更新でのローテーション）のみ。

## 追記: 最終コードでの再適用

Codexレビュー2巡目の指摘（履歴違反の判定を厳密化 / IAM terraform test の文数 / 仕様書の記述）を
反映した**最終コード**で、検証用 Stack を再度 apply し、E2E を通しで再実行した（**38/38 PASS**）。
`keepers` によるパスワードローテーションも再現し、新しい `demo_password` でそのままログインできることを確認。
