# ADR-0017: L3 実行基盤の OKE(Kubernetes)移行（JetUse 本体＋生成デモ）

日付: 2026-06-27
状態: **提案(案)**（DEP-03 で起票。施主承認待ち＝人間ゲート）。
**本 ADR は ADR-0015 / ADR-0016 の「L3 実行基盤の選択(Container Instances)」を supersede する。**
両 ADR の基盤非依存な核（D8 固定基盤・新規実行基盤を増やさない方針／秘密を持たない宣言的配備仕様／
ブローカー一本のデータ注入／二重閉包・発行後自己検証／短期 TTL・呼び出しごと発行／秘密を IaC state に
残さない）は **OKE でもそのまま保つ**。Container Instances 版(DEP-01/02)は stage-4 ベースラインとして残す。

> 経緯: ADR-0015 §「基盤の方針転換(2026-06-27)」/ ADR-0016 §「基盤の方針転換」で予告した OKE 移行を、
> 本 ADR で正式に確定する。施主決定: **JetUse 本体・生成デモ・インストール済みユースケースアプリ(L3)を
> すべて OKE へ移行**（memory `oke-migration-decision`）。

## 背景

DEP-01/02 で L3 ホスト型デモを **OCI Container Instances** 上に配備する設計を確定した
(`deploy.py` = 宣言的配備仕様 / `deploy_inject.py` = Platform API ランタイム注入)。実運用で次の制約が露見した:

1. **デモの deploy/delete が trivial でない**: Container Instance は env が作成時固定で、トークン更新が
   事実上 restart/redeploy 相当（ADR-0016 §5 の MVP 注記）。多数の生成デモ／インストール済みユースケース
   アプリ(L3)を「namespace を切って撒く／消す」粒度で扱えない。
2. **ライフサイクル／棚卸し**: デモ単位のスケール・ローリング更新・ヘルスチェック・撤去を宣言的に回す
   標準機構が薄い（タグ棚卸しに依存）。
3. **本体と L3 の実行基盤が分かれている**: JetUse 本体(compute/CI)とデモ(CI)で運用面が二重化。

Kubernetes(OKE) は Deployment/Service/Namespace/Secret/RBAC により、上記を **K8s ネイティブ**に解決する。
デモ・ユースケースアプリは **K8s ワークロード**(Deployment/Helm release・namespace) として deploy/delete が
trivial になり、トークン更新は **Secret 更新＋投影反映 or rolling update** で回る。

確定済みの再利用可能資産（基盤非依存の核。OKE でも不変）:
- **ADR-0009**: 3 SDK 別ホスト型 ReAct（SDK→Application OCID 解決規則）。
- **ADR-0011**: 配布イメージは OCIR(ap-osaka-1)。OKE の worker からの pull もここに集約。
- **ADR-0014 / platform_broker / platform_grants**: スコープ付き短期トークン。L3 は秘密鍵を持たずトークンのみ提示(D5)。
- **`deploy.py` / `deploy_inject.py` の核**: 秘密を持たない宣言的配備仕様生成、二重閉包・発行後自己検証、TTL。

## 決定（案）

### 1. L3／本体の実行基盤を OKE(Oracle Kubernetes Engine) に確定する

JetUse 本体・生成デモ・インストール済みユースケースアプリ(L3)を **単一の OKE クラスタ**上で動かす。
Container Instances（ADR-0015/0016 の前提）を supersede する。**D8（固定リファレンス基盤・デモ側は
アプリ層成果物のみ差し込む）は維持**: デモは「namespace ＋ Deployment/Secret/ConfigMap マニフェスト」という
アプリ層成果物に閉じ、クラスタ・ノードプール・VCN・RBAC・broker は固定基盤として供給する。

### 2. OKE クラスタ構成（IaC・apply は人間ゲート）

- **配置**: コンパートメント `jetuse-dev`、リージョン `ap-osaka-1`（`.env` の OCID。**リポジトリに OCID を
  コミットしない**）。
- **専用 VCN（新規）**: **既存 `develop` VCN は参照しない**。OKE 専用に VCN／サブネット／NSG を新設する
  （`infra/terraform/environments/oke/`）。OKE の標準分割に従い 3 サブネットを切る:
  - **k8s API エンドポイント**サブネット（プライベート。control plane endpoint。NSG で 6443 を VCN 内＋
    worker から許可）。
  - **worker(node)** サブネット（プライベート。NAT/Service Gateway 経由で egress。OCIR pull・GenAI・
    Platform API へ到達）。
  - **service load balancer** サブネット（`type=LoadBalancer` の Service 用。MVP は内部 LB 既定。外部公開は
    本体 Ingress 用に別途人間承認）。
- **クラスタ**: managed OKE。`endpoint_config` はプライベート API エンドポイント（パブリック API は
  人間承認時のみ）。`cluster_pod_network_options` は VCN-native(OCI_VCN_IP_NATIVE) を既定とし、pod CIDR を
  worker サブネットに収容（フリート上限を IaC 変数で制御）。
- **ノードプール**: 1 プール・**最小サイズ**（既定 `node_pool_size=2`、`VM.Standard.E4.Flex` / OCPU・メモリは
  変数）。autoscale は MVP 非対象（人間承認で追加）。**恒常課金が発生するため apply は人間ゲート**。
- **IAM**: クラスタ運用に必要な dynamic group / policy（worker の OCIR pull・Vault 参照等）は **人間が手動**で
  設定する（`enable_iam=false` 維持。memory `agent-no-tenancy-perms`）。本 ADR は必要ポリシーの**列挙**に留める。

### 3. JetUse 本体の OKE ホスティング

- 本体 API(FastAPI) を **Deployment + Service** で動かす（`infra/k8s/jetuse-api/`）。外部公開は
  **Service `type=LoadBalancer`（OCI LB）または Ingress**（API Gateway 連携は当面維持可）。
- 非秘密設定は ConfigMap、秘密（ADB ウォレット等は本体のみが保持。L3 には配らない）は K8s Secret か
  OCI Vault + CSI（将来）。本体の Secret 管理は L3 とは別経路（本体は DB 直結、L3 はブローカー一本）。
- **移行手順（compute/CI → OKE）**は `docs/verification/DEP-03.md` と本 ADR §7 に記す（段階移行: イメージは
  OCIR 既存を再利用 → OKE へ Deployment → API GW/LB のルート切替 → 旧 CI/compute 撤去）。

### 4. デモ／ユースケースアプリの K8s ネイティブ deploy/delete

`deploy.py` の配備ターゲットを container-instance（tfvars）から **K8s マニフェスト**に置換する:

- **1 デモ = 1 namespace**（決定的命名 `jetuse-demo-<sample_app>[-<tenantN>]`。tenant ハッシュ 12 hex。ADR-0016 §6 の命名規約を
  namespace へ写像）。namespace に標準ラベルを付す（`app.kubernetes.io/managed-by=jetuse-deploy` /
  `jetuse.dev/kind=hosted-demo` / `jetuse.dev/demo=<sample_app>` / `jetuse.dev/tenant=<tenant_hash>`）。
  **生の tenant/project OCID は入力値としてのみ扱い、label/manifest には非秘密ハッシュ（12 hex）だけを出す**
  （OCID 露出・label 長超過を避ける）。
- **マニフェスト**: Namespace ＋ ConfigMap（**非秘密 env のみ**）＋ Deployment（envFrom で ConfigMap/Secret を
  参照）＋ Service。`deploy.py` がこれらを **決定的に**生成する（YAML / JSON）。新規インフラ（VCN/LB/IAM）は
  作らない（D8）。
- **deploy**: `kubectl apply -f`（または Helm install）。**delete**: `kubectl delete namespace <ns>`（または
  Helm uninstall）で namespace ごと撤去 ＝ deploy/delete が trivial。
- **RBAC（最小権限）**: デモ Pod は **K8s API を使わない**（データ到達は broker 経由）。よって専用
  ServiceAccount を **`automountServiceAccountToken: false`** で置き、**Role/RoleBinding は作らない**
  （= namespace 内 API 権限すらゼロ＝最小権限。API を使う設計に変わった時のみ namespace 限定 Role を足す）。
  `deploy.py` の `render_manifests()` は **Namespace / ServiceAccount / ConfigMap / Secret(注入) /
  Deployment / Service / ResourceQuota** を生成する（Role/RoleBinding は上記理由で非生成）。
  越境・暴走は namespace 分離＋**ResourceQuota** で抑える（**LimitRange**（コンテナ既定 limit）は
  任意の追加ハードニング＝follow-up。現状は Deployment に明示の requests/limits を付けて代替）。

### 5. Secret 注入（K8s ネイティブ・核は維持）

ADR-0016 の注入物を **base_url(非秘密)＋短期トークン(秘密) の 2 つに限定**する核を K8s に写像する:

- **非秘密（base_url ＋ 静的 JETUSE_*）**: **ConfigMap**（committed なマニフェストに載る。state/Secret に
  秘密を混ぜない）。Deployment は `envFrom: configMapRef`。
- **秘密（短期トークン）**: **K8s Secret**（`type=Opaque`, `data: {JETUSE_PLATFORM_TOKEN: <base64>}`）。
  `stringData` ではなく **base64 済み `data`** を使う（server-side apply は stringData の field 管理が
  不安定で refresh の in-place 更新が反映されないことがあるため。`data` は apply 経路非依存で決定的）。
  Deployment は `envFrom: secretRef`。Secret は **オーケストレータがアウトオブバンドで apply**
  （`kubectl apply`／API。`deploy_inject.build_runtime_injection().render_secret_manifest()` が生成）。
  **Terraform を通さない**（state にトークンを残さない＝ADR-0016 §5 の核を維持）。Secret マニフェストは
  **コミットしない**（短期・揮発）。
- **二重閉包・発行後自己検証・fail-closed は不変**（`deploy_inject` の `build_runtime_injection` 核をそのまま
  使う。配備仕様閉包 ∩ 承認グラント閉包、発行直後 `verify_broker_token`）。
- **DB 認証情報は注入しない(D5)**。注入物のキー allowlist（`{JETUSE_PLATFORM_API_BASE_URL}` /
  `{JETUSE_PLATFORM_TOKEN}`）を K8s マニフェスト描画でも保つ。

### 6. トークン更新（refresh）と失効（K8s ネイティブ）

- **更新**: TTL（`platform_token_ttl_seconds`、broker 上限 900 秒）内に **Secret を更新**して反映する。
  反映方式は 2 系統を許容し、MVP は (a):
  - **(a) rolling update（既定・MVP）**: Secret を新値で apply → Deployment を **rolling restart**
    （`kubectl rollout restart deploy/<name>` 相当 ＝ env-from-secret は Pod 再生成で反映）。`should_refresh`
    判定で TTL 内に再注入＝再 apply＋restart。env が作成時固定だった CI の制約は OKE で解消（ローリングで無停止）。
  - **(b) projected volume 自動反映（将来）**: Secret を **ファイル投影**（volume mount）し、アプリが定期再読込
    すれば Pod 再生成なしで反映（kubelet の Secret 同期）。env 経路ではなくファイル経路が要るため、アプリ側の
    再読込実装が前提（INFRA で上乗せ）。
- **失効（revoke）**: ADR-0016 §4 を維持。`platform_grants.revoke_grant` 後の再発行（＝次回 refresh）は
  `grant_revoked` で拒否され、**失効の有効化窓 = TTL**（発行済みは TTL まで有効、その後の更新で必ず止まる）。
  即時失効（jti 失効リスト）は MVP 非対象。

### 7. apply・課金・IAM は人間ゲート

- **OKE クラスタ／ノードプール／VCN の `terraform apply`（恒常課金）は人間ゲート**。本タスク(DEP-03)は
  **設計＋IaC plan/validate＋K8s 化＋ローカル/実 E2E（クラスタ前提なし範囲）**まで。実 OKE への apply・実 deploy は
  オーケストレータ（人間）が行う（手順は `docs/verification/DEP-03.md` と `runs/<id>/e2e/SKIPPED.md`）。
- **IAM ポリシー／dynamic group は人間が手動**（`enable_iam=false` 維持）。
- コミット/PR/push は人間承認後。

## 理由

- K8s は「多数のワークロードを namespace 単位で deploy/delete・更新・棚卸し」する標準機構を持ち、デモ／
  ユースケースアプリの L3 ライフサイクル要件に素直に一致する（ADR-0016 が CI で苦労した点を解消）。
- 固定基盤(D8) は崩さない: クラスタ/VCN/RBAC/broker は固定基盤、デモはアプリ層マニフェストのみ。
- 注入の核（base_url＋短期トークンの 2 つ・二重閉包・秘密を state に残さない）は基盤非依存に書けており、
  K8s ConfigMap/Secret へそのまま写像できる（最小権限・越境防止 / ADR-0014 D5 を維持）。
- 本体と L3 の実行基盤を OKE に一本化し運用を単純化（イメージは OCIR 既存を再利用＝ADR-0011 不変）。

## 却下した代替案

- **A. Container Instances 継続（ADR-0015/0016 のまま）**: デモの大量 deploy/delete・無停止トークン更新・
  namespace 単位の棚卸しが弱く、L3 のスケール要件に合わない（本 ADR の背景 1〜3）。
- **B. 既存 `develop` VCN に OKE を相乗り**: 既存リソースは参照のみ（CLAUDE.md）。OKE は専用サブネット/NSG
  設計（API endpoint/worker/LB）を要し、既存 VCN を変更せざるを得ない。**専用 VCN 新設**で分離する。
- **C. 短期トークンを K8s Secret に長期常駐 or Terraform 管理**: TTL（数分）と合わず、state へ秘密残留。
  アウトオブバンド apply（Terraform を通さない・コミットしない）で揮発させる（ADR-0016 §5 の核を維持）。
- **D. デモごとに専用 VCN/LB/IAM を生成**: D8 違反。namespace＋RBAC＋ResourceQuota の論理分離で代替する。

## 影響範囲 / 未決事項

- 影響:
  - `infra/terraform/modules/oke/`（新規: cluster + node_pool）、`infra/terraform/environments/oke/`（新規:
    専用 VCN/サブネット/NSG ＋ module 結線。**plan/validate まで**）。
  - `infra/k8s/jetuse-api/`（新規: 本体 Deployment/Service/ConfigMap）。
  - `jetuse_core/deploy.py`（配備ターゲットを container-instance tfvars → K8s マニフェスト描画に置換。
    `build_deploy_spec` の検証核・allowlist は不変）。
  - `jetuse_core/deploy_inject.py`（K8s Secret/ConfigMap 描画を追加。`build_runtime_injection` の二重閉包・
    自己検証・TTL は不変）。
  - `infra/terraform/environments/hosted-demo/`（Container Instances 版。stage-4 ベースラインとして残置。
    新規デモ配備は K8s 経路）。
- 必要 IAM（人間が手動で確定）: OKE service へのクラスタ作成ポリシー、worker の dynamic group（OCIR pull・
  必要なら Vault/GenAI 参照）、Cloud Controller Manager の LB 作成権限。
- **本体の OCI 認証 on OKE（移行 follow-up・人間ゲート）**: 既存コードは `AUTH_MODE=resource_principal` で
  Functions 用 env ベース signer（`get_resource_principals_signer` / `oci_genai_auth.OciResourcePrincipalAuth`）を
  使う。これは **OKE Pod では成立しない**。OKE では Workload Identity（SDK の workload identity signer ＋
  `oci_genai_auth` の対応）か instance principal へ切り替えるコード対応＋**実 OKE 検証**が要る（GenAI 推論は
  third-party `oci_genai_auth` 依存のため実 OKE での検証が前提＝apply ゲート）。本タスクは本体ホスティングの
  manifest／移行手順までを確定し、auth signer 実装は follow-up とする（configmap で AUTH_MODE を設定しない）。
- 未決(INFRA 以降): projected volume 方式の自己再読込（無停止 refresh §6(b)）、jti 失効リストによる即時失効、
  OKE 上の本体 Secret を OCI Vault + CSI で管理、マルチテナンシでの OCIR ミラー（ADR-0011 既知制約）、
  cluster autoscaler、本体の API Gateway → OKE Ingress 完全移行のルート切替手順の実機確定。
