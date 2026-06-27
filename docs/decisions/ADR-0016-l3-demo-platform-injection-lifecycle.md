# ADR-0016: L3 デモの Platform API ランタイム注入とデモ配備ライフサイクル

日付: 2026-06-27
状態: **提案中**(DEP-02 で起票。ADR-0015 §7「デモ配備のライフサイクル(更新/破棄/命名規約)は実 apply
前に DEP-02 で確定する」を受けて確定案を出す。承認は人間ゲート)。

> ADR-0015 は L3 ホスト型デモの **配備仕様生成**(DEP-01: 秘密を持たない宣言的 tfvars 生成)までを確定し、
> 実トークン発行のタイミング/失効・デモ単位の命名規約・更新/破棄(ライフサイクル)を **DEP-02 以降で確定** と
> 明示的に残した(§7・§8 未決事項)。本 ADR はその残課題を確定する。

## 背景

DEP-01 の `ContainerDeploySpec` は **秘密を一切持たない**(`required_secrets` は論理名のみ、Vault OCID も
実値も tfvars/state に出さない)。一方、デモコンテナが実際にテナントデータへ到達するには Platform API の
**ベース URL** と **ブローカー発行の短期トークン**(ADR-0014)が起動時に要る。この「実注入の配管」と、
実コンテナを起こす前提となる「デモ配備のライフサイクル(増やし続けない保証)」を確定する必要がある。

## 決定(案)

### 1. Platform API ランタイム注入(`jetuse_core/deploy_inject.py`)

コンテナ起動時に注入するのは **2 つだけ**:

- **ベース URL**(非秘密 env `JETUSE_PLATFORM_API_BASE_URL`): `settings.platform_api_base_url`(https 固定)。
- **短期トークン**(秘密 `JETUSE_PLATFORM_TOKEN`): `platform_grants.issue_token` が発行する短期 JWT。

`build_runtime_injection(spec, tenant, plugin_id)` がこの 2 つを `RuntimeInjection` として組み立てる。
`env()` は **非秘密のみ**、`secret_env()` は **トークンのみ** を返し、秘密と非秘密を経路として分離する。

### 2. DB 認証情報は注入しない(D5 / ADR-0014)

注入物は base_url(非秘密)＋ token(秘密)に **構造的に限定**する(キー allowlist =
`{JETUSE_PLATFORM_API_BASE_URL}` / `{JETUSE_PLATFORM_TOKEN}`)。`deploy_inject` は `adb_*` 等の DB 資格を
読まない・載せない。デモコンテナは DB 資格情報・DB ネットワーク経路を持たず、**ブローカー発行の短期
トークンだけ**がテナントデータへの経路(ADR-0014 D5 / ADR-0015 §4 を実装で固定)。

### 3. 承認スコープに厳密に閉じる(二重閉包・fail-closed)

トークンに載るスコープは次の **二重の閉包**に限定する:

1. **配備仕様閉包**: 要求スコープは配備仕様 `required_scopes`(= active コネクタ束縛由来でデモが宣言した
   必要スコープ)の部分集合のみ。宣言外スコープは `deploy_inject` が `scope_outside_spec` で拒否。
2. **承認グラント閉包**: `platform_grants.issue_token` が承認グラント(`platform_scope_grants`)の部分集合
   のみ載せ、承認超過は `scope_not_granted` で拒否。グラント無し=`no_grant`、失効=`grant_revoked`。

発行直後に `verify_broker_token` で自己検証し、**載ったスコープと失効時刻を権威値として確定**する
(発行=検証の乖離・未知スコープ混入を入口で検出)。

### 4. トークンのライフサイクル(発行/失効/更新)

- **発行粒度 = 呼び出しごと**(ADR-0014 §2 / platform_grants の確定を踏襲)。コンテナ起動・更新のたびに
  新規トークンを発行し、セッションで使い回さない(リプレイ露出窓の最小化)。
- **TTL は短期**(`settings.platform_token_ttl_seconds`、既定 300 秒。ブローカー上限 900 秒)。
- **更新(refresh)**: 長時間稼働するデモコンテナは TTL 内に **再注入(= `build_runtime_injection` の再呼び出し)**
  でトークンを更新する。判定は `should_refresh`(失効まで skew 秒を切ったら更新)。更新時に承認グラントが
  再評価されるため、承認の変更・失効は次回更新で反映される。
- **失効(revoke)**: `platform_grants.revoke_grant` 後の再発行(= 次回起動/更新)は `grant_revoked` で拒否
  される。実トークンの **即時失効(jti 失効リスト)は MVP 非対象**のため、**失効の有効化窓 = TTL**
  (発行済みトークンは TTL まで有効、その後の更新で必ず止まる)。この有界露出を受容し、TTL を短く保つ
  ことで担保する。即時失効が要件化したら jti 失効リスト(INFRA)で上乗せする。

### 5. 秘密/非秘密の経路分離(短期トークンを Terraform state に残さない)

- 静的・非秘密 env(deploy.py 生成)は committed な `*.auto.tfvars.json` で与える(DEP-01 不変)。
- **ベース URL(非秘密)** は `hosted-demo` 環境の `platform_api_base_url` で Terraform 経由注入する
  (非秘密のため state に残っても漏えいにならない)。
- **短期トークンは Terraform 経路を通さない**。Terraform に渡した値は resource 入力として **state に
  保存される**(`sensitive` は CLI 表示のマスクのみで、state には残る)。短期トークンを state に残さない
  ため、トークンは **起動時のアウトオブバンド注入**で渡す:
  - **MVP**: オーケストレータが `deploy_inject.build_runtime_injection().secret_env()` を実行中コンテナへ
    直接注入する(Terraform を介さない)。`should_refresh` 判定で TTL 内に再注入して更新する。
  - **目標(INFRA)**: コンテナ自身が起動時/更新時にブローカーのトークンエンドポイントから短期トークンを
    自己取得する(OIDC 発行主体認証 / INFRA-02)。
  これにより短期トークンは tfvars にも長期 state にも残らず、TTL で揮発する(ADR-0015 §3 の方針を強化)。

### 6. デモ配備の命名規約(naming convention)

増やし続けない保証の前提として、デモ配備は **決定的な命名**にする(CLAUDE.md「むやみに増やさない」):

- **display_name**: `<prefix>-api`。`prefix` は deploy.py が `jetuse-demo-<sample_app>` を健全化して生成
  (英小文字/数字/ハイフン・長さ上限。DEP-01 確定)。マルチテナントで衝突しうる場合は呼び出し側が
  `prefix` にテナント短縮子を足す(例 `jetuse-demo-sba-a-<tenant8>`)。
- **タグ(freeform)**: `jetuse:kind=hosted-demo` / `jetuse:demo=<sample_app>` / `jetuse:tenant=<project_ocid>` /
  `jetuse:managed-by=jetuse-deploy`。破棄・棚卸しの絞り込みキーにする(タグ実体の付与は実 apply 着手時に
  container-instance モジュールへ薄く追加。本 ADR は規約を確定)。

### 7. デモ配備の更新(redeploy)と破棄(teardown)

- **更新**: 同一 `display_name`(+ タグ)に対する `terraform apply` は **同一コンテナの差し替え**(in-place
  replace)。デモ単位で 1 コンテナ(D8: デプロイ上限=コンテナ)を保ち、再配備でコンテナを増やさない。
  イメージ/構成/トークンが変わっても配備単位は同じ display_name に収束する(冪等)。
- **破棄**: `terraform destroy`(hosted-demo 環境)でデモコンテナを撤去する。デモは **ephemeral** とみなし、
  デモ終了後はタグ(`jetuse:kind=hosted-demo`)で棚卸し・撤去する運用とする。短期トークンは TTL で自然失効
  するため破棄後に有効な認可は残らない。承認グラント自体の撤去は `revoke_grant`(行は監査のため残す)。
- **apply / destroy は人間ゲート**(ADR-0015 §6 不変)。本タスク(DEP-02)では実 apply / 実 destroy はしない。

## 理由

- 注入物を base_url + 短期トークンの 2 つに構造的に絞ることで、DB 資格情報・broker 署名鍵などが
  デモコンテナへ渡る経路を原理的に塞ぐ(最小権限・越境防止 / ADR-0014 D5)。
- 二重閉包と発行後自己検証で「承認した範囲ちょうど」を保証する(承認超過・未知スコープを fail-closed)。
- 短期 TTL + 呼び出しごと発行 + 更新で承認変更を伝播させ、即時失効機構が無い MVP でも露出窓を有界化する。
- 決定的命名 + タグ + 同一 display_name への収束で、デモ配備が増え続けない(CLAUDE.md / D8)。

## 却下した代替案

- **A. 短期トークンを committed tfvars / Vault secret に置く**: TTL(数分)とウォレット/state の寿命が合わず、
  かつ committed 経路への秘密残留(ADR-0015 §3 が避けた表面)を増やす。sensitive な apply 時注入を採用。
- **B. デモごとに display_name を一意連番にする**: 配備が単調増加し棚卸し困難(D8・CLAUDE.md 違反)。
  デモ単位で決定的な display_name に収束させ、更新は差し替えにする。
- **C. コンテナに OIDC クライアント秘密を渡し自己でトークン取得**(ADR-0015 §3(a) の許可秘密経路):
  将来の長時間稼働・自己更新には有効だが、MVP では発行主体をオーケストレータに集約し注入経路を
  ブローカー一本に保つ方が監査・最小権限の検証が容易。自己取得は INFRA(OIDC 発行主体認証)で上乗せする。

## 影響範囲 / 未決事項

- 影響: `jetuse_core/deploy_inject.py`(新規)、`settings.platform_api_base_url`(新規・空既定)、
  `infra/terraform/environments/hosted-demo`(**非秘密の** `platform_api_base_url` var 追加のみ。短期トークンは
  Terraform を通さない=state に残さない)。DEP-01 の公開シグネチャ・committed tfvars 契約は不変(追加のみ)。
- 未決(INFRA 以降): jti 失効リストによる即時失効、コンテナ自己トークン更新(OIDC 発行主体認証 / INFRA-02)、
  マルチテナンシでの OCIR ミラー(ADR-0011 既知制約)、タグ実体の container-instance モジュール付与。
