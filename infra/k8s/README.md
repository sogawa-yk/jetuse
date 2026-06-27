# JetUse on OKE — K8s マニフェスト / 運用手順（ADR-0017 / DEP-03）

L3 実行基盤を Container Instances から **OKE(Kubernetes)** へ移行した（ADR-0017）。
クラスタ／ノードプール／専用 VCN は `infra/terraform/environments/oke`（plan 止まり・apply は人間ゲート）。
本ディレクトリは **アプリ層のマニフェスト**（本体ホスティング＋デモ配備の雛形）。

## 構成

```
infra/k8s/jetuse-api/      # JetUse 本体(FastAPI)の Deployment/Service/ConfigMap/Secret 雛形
```

デモ／ユースケースアプリ（L3）のマニフェストは **コードで決定的に生成**する（コミットしない）:
- 非秘密一式（Namespace/ConfigMap/Deployment/Service ほか）: `jetuse_core.deploy.build_deploy_spec(...).render_manifests_yaml()`
- 注入（base_url ConfigMap ＋ 短期トークン Secret）: `jetuse_core.deploy_inject.build_runtime_injection(...).render_injection_manifests(spec)`

## 1. JetUse 本体の OKE ホスティング（compute/CI → OKE 移行手順）

前提: OKE クラスタが apply 済み（人間ゲート）。`kubectl` の kubeconfig を取得:

```bash
oci ce cluster create-kubeconfig --cluster-id <CLUSTER_OCID> \
  --file $HOME/.kube/config --region ap-osaka-1 --token-version 2.0.0 \
  --kube-endpoint PRIVATE_ENDPOINT
```

移行ステップ（段階移行・ダウンタイム最小）:

1. **イメージ**: 既存 OCIR イメージ（`kix.ocir.io/<ns>/jetuse-dev-api:<tag>`。ADR-0011）をそのまま再利用。
2. **設定/秘密**: `jetuse-api/configmap.example.yaml` を実値で作成（非秘密）。秘密は **コミットせず**
   `kubectl create secret`（または OCI Vault + CSI）で作成（`jetuse-api/secret.example.yaml` 参照）。
   - 本体だけが DB 資格情報/broker 署名鍵を保持する（L3 デモには配らない＝ADR-0014 D5）。
3. **OCI 認証(移行 follow-up・人間ゲート)**: 本体の OCI 認証 on OKE は **未確定**。既存コードは
   `AUTH_MODE=resource_principal`(Functions 用 env ベース signer)で、これは **OKE Pod では成立しない**。
   方式を 1 つに固定してコード対応＋実 OKE 検証する(apply ゲート):
   - **OKE Workload Identity**: `serviceaccount.yaml` の `jetuse-api-sa` を IAM の dynamic group に紐付け、
     deployment の `automountServiceAccountToken: true` ＋ SDK の workload identity signer ＋
     `oci_genai_auth` の WI 対応を実装。
   - **instance principal**: worker ノードの dynamic group ＋ SDK の InstancePrincipals signer に切替。
   実装するまで configmap に `AUTH_MODE` を設定しない(誤って OKE 上で認証失敗させない)。IAM は人間が手動
   (`enable_iam=false` 維持)。
4. **デプロイ**（`jetuse-api/` は**雛形**。`deployment.yaml` は `${JETUSE_API_IMAGE}` を envsubst で
   埋めてから apply する。OCI 認証 on OKE 未確定のため本体の実 OKE 起動は移行 follow-up＝人間ゲート）:
   ```bash
   kubectl apply -f infra/k8s/jetuse-api/namespace.yaml
   kubectl apply -f infra/k8s/jetuse-api/serviceaccount.yaml
   kubectl apply -f infra/k8s/jetuse-api/configmap.example.yaml   # 実値版
   kubectl -n jetuse-system create secret generic jetuse-api-secrets --from-literal=...   # コミットしない
   export JETUSE_API_IMAGE=kix.ocir.io/<ns>/jetuse-dev-api:<tag>
   envsubst < infra/k8s/jetuse-api/deployment.yaml | kubectl apply -f -
   kubectl apply -f infra/k8s/jetuse-api/service.yaml             # 既定: 内部 LB
   kubectl -n jetuse-system rollout status deploy/jetuse-api
   ```
   ヘルスチェックは `/healthz`(FastAPI 実装。service/main.py)。
5. **ルート切替（OCI 認証の OKE 検証が通った後）**: 既存 API Gateway のバックエンドを、旧 Container
   Instance/compute から OKE Service（内部 LB の private IP / または Ingress）へ向け替える。
   Service は **平文 HTTP（port 80 → targetPort 8000）** で公開するため、**API Gateway の backend scheme
   は HTTP**（TLS 終端は API Gateway 側）。LB で TLS 終端する場合のみ `service.yaml` に LB TLS annotation
   ＋証明書を追加する。**本体の OCI 呼び出しが OKE 上で疎通することを確認してから**段階切替する
   （step 3 follow-up が前提）。`/healthz` を Service 経由で疎通確認してから切替。
6. **旧基盤撤去**: 旧 Container Instance / compute の停止・撤去（人間ゲート。`terraform destroy` 相当）。

外部公開（public LB / Ingress）は `service.yaml` の annotation を人間承認で変更する（ADR-0017 §3）。

## 2. 生成デモ（L3）の K8s ネイティブ deploy / delete / token 更新

### deploy（1 デモ = 1 namespace。マルチテナントは tenant ハッシュで分離）

deploy / refresh / delete は **同じ base `--prefix`・同じ `--tenant`・同じ answers** を使う。最終 namespace
は `build_deploy_spec(prefix=..., tenant=...)` が **tenant の非秘密ハッシュ(12 hex)を付けて決定的に導出**する
（`jetuse-demo-<app>-<tenantN>`）。これで同一デモを複数テナントに配備しても namespace/Secret/ConfigMap/
Deployment が衝突しない。deploy と injection は **必ず同じ prefix/tenant** を渡す（別名 Secret を作らない・
他テナントの Secret を上書きしない）。

```bash
# jetuse_core は packages/api 配下。repo root から `python -c` する手順では PYTHONPATH を通す
# （editable install 済みなら不要。render_injection.py は自前で sys.path 補正するため影響なし）。
export PYTHONPATH=packages/api
PREFIX=jetuse-demo-faq                 # base prefix。deploy/refresh/delete で一貫して使う
TENANT=ocid1.tenancy.oc1..<project>    # テナント識別子。namespace の一意化に使う(非秘密ハッシュ化)
PLUGIN=acme/demo-app                   # 発行主体プラグイン ID。deploy 時に Deployment 注釈へ固定する
ANSWERS='{"Q1":"support","Q2":["docs"],"Q3":"rag_qa","Q4":"slack","Q5":"chat_form","Q6":"sample"}'
IMAGE=kix.ocir.io/<ns>/jetuse-demo:latest

# 0) 最終 namespace を base prefix + tenant から導出（delete でも同じ式を使う）
NS=$(python -c "import json,os;from jetuse_core.deploy import build_deploy_spec;from jetuse_core.synth import synthesize;from jetuse_core.recommend import recommend;\
print(build_deploy_spec(synthesize(recommend(json.loads(os.environ['ANSWERS']))), image_url=os.environ['IMAGE'], prefix=os.environ['PREFIX'], tenant=os.environ['TENANT']).namespace)")

# 1) 非秘密マニフェストを生成して apply（Namespace/ConfigMap/Deployment/Service/ResourceQuota/SA）
#    tenant→namespace に tenant ハッシュ＋jetuse.dev/tenant label。plugin→Deployment に
#    jetuse.dev/plugin-id 注釈を固定（注入の plugin すり替え防止の ground truth。injection と同じ値）。
python -c "import json,os;from jetuse_core.deploy import build_deploy_spec;from jetuse_core.synth import synthesize;from jetuse_core.recommend import recommend;\
print(build_deploy_spec(synthesize(recommend(json.loads(os.environ['ANSWERS']))), image_url=os.environ['IMAGE'], prefix=os.environ['PREFIX'], tenant=os.environ['TENANT'], plugin_id=os.environ['PLUGIN']).render_manifests_yaml())" \
  | kubectl apply -f -

# 2) 注入（base_url ConfigMap=非秘密 ＋ 短期トークン Secret=秘密）をアウトオブバンドで apply
#    render_injection.py は同じ prefix/tenant から **同一 namespace/Secret 名** を導出する。
#    Secret はコミット/Terraform/state を通さない（短期 TTL で揮発）。
#    --server-side: token を last-applied-configuration annotation に残さない（client-side apply の
#    漏えい回避。review 対応）。注入物は kubectl が field manager となり、annotation に平文を書かない。
python tools/render_injection.py --prefix "$PREFIX" --answers "$ANSWERS" --image "$IMAGE" \
  --tenant "$TENANT" --plugin "$PLUGIN" --base-url https://platform.example/... \
  | kubectl apply --server-side -f -
```

deploy 後、Deployment の `envFrom` が ConfigMap（静的＋runtime）と Secret（token）を参照し、Pod に
注入される。`required_scopes` が空のデモは注入 Secret/ConfigMap を参照しない（fail-closed）。

> `render_injection.py` は **トークン発行前に**、再構築 spec の `required_scopes` が **実デプロイ済み
> Deployment の `jetuse.dev/required-scopes` 注釈（ground truth）** と一致するか kubectl で検証する。
> deploy 時と違う answers を渡して宣言外スコープのトークンを同名 Secret へ上書きする運用迂回を塞ぐ
> （不一致／Deployment 不在は fail-closed＝トークン未発行。無効化スイッチは無い）。

### delete（namespace ごと撤去 = trivial）

```bash
# 同じ PREFIX/TENANT/ANSWERS から NS を導出して削除（上の手順 0 と同一式）
kubectl delete namespace "$NS"        # Deployment/Service/Secret/ConfigMap ごと消える
```

棚卸し（増やし続けない保証・ADR-0016 §6 のラベル規約）:

```bash
kubectl get ns -l jetuse.dev/kind=hosted-demo            # ホスト型デモの一覧
kubectl get ns -l jetuse.dev/demo=<sample_app>           # デモ別
```

### token 更新（refresh）

短期 TTL（既定 300 秒、broker 上限 900 秒）。`should_refresh` が True になったら **Secret を新値で再 apply →
rolling restart** で反映する（ADR-0017 §6・env-from-secret は Pod 再生成で反映）:

```bash
# deployment/namespace 名は tenant ハッシュ込み（deploy と同一式で導出）
DEP=$(python -c "import json,os;from jetuse_core.deploy import build_deploy_spec;from jetuse_core.synth import synthesize;from jetuse_core.recommend import recommend;\
print(build_deploy_spec(synthesize(recommend(json.loads(os.environ['ANSWERS']))), image_url=os.environ['IMAGE'], prefix=os.environ['PREFIX'], tenant=os.environ['TENANT']).prefix)")
# 新トークンで Secret を再 apply（deploy と同じ PREFIX/TENANT/ANSWERS → 同一 Secret 名 = in-place 更新）
# --server-side で annotation に平文 token を残さない（refresh も同様）。
python tools/render_injection.py --secret-only --prefix "$PREFIX" --answers "$ANSWERS" --image "$IMAGE" \
  --tenant "$TENANT" --plugin "$PLUGIN" --base-url https://platform.example/... \
  | kubectl apply --server-side -f -
# rolling restart で Pod に反映（無停止）
kubectl -n "$NS" rollout restart deploy/"$DEP"
```

更新時に承認グラントが再評価されるため、**承認失効は次回更新（= TTL 窓内）で伝播**する。即時失効
（jti 失効リスト）は MVP 非対象（INFRA で上乗せ）。

## セキュリティ姿勢（不変・基盤非依存）

- **秘密非注入**: 注入物は base_url(非秘密)＋短期トークン(秘密)の 2 つのみ（キー allowlist）。DB 資格情報は
  L3 に配らない（D5）。
- **二重閉包**: トークンのスコープ = 配備仕様 `required_scopes` ∩ 承認グラント。発行直後に自己検証。
- **秘密を state に残さない**: 短期トークン Secret はコミット/Terraform/state を通さない（アウトオブバンド apply・
  TTL で揮発）。本体の長期秘密は Secret/OCI Vault で本体 namespace に閉じる。
