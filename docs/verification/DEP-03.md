# DEP-03 検証レポート — L3 実行基盤の OKE 移行

- 日付: 2026-06-27
- ブランチ: feat/DEP-03（base: feat/stage-4 系列）
- run_id: `2026-06-27T1509_DEP-03`
- 関連: ADR-0017（OKE 基盤・本タスクで起票）、ADR-0015/0016（supersede 対象）、tasks/DEP-03.md

## 1. 目的

JetUse 本体と生成デモ／ユースケースアプリ（L3）を **OKE（Kubernetes）** へ移行する。デモは K8s ワークロード
（Deployment／namespace）として deploy/delete が trivial になる。L3 実行基盤を Container Instances
（DEP-01/02）から OKE へ置換（ADR-0017 が L3 基盤選択を supersede）。

## 2. 成果物

| 区分 | 成果物 |
|------|--------|
| ADR | `docs/decisions/ADR-0017-oke-l3-platform.md`（提案／施主承認は人間ゲート） |
| OKE IaC | `infra/terraform/modules/oke/`（cluster + node_pool）、`infra/terraform/environments/oke/`（**専用 VCN** + 3 サブネット + NSG + module 結線。plan/validate まで） |
| 配備の K8s 化 | `packages/api/jetuse_core/deploy.py`（container-instance tfvars → K8s manifest 描画）、`deploy_inject.py`（K8s Secret/ConfigMap 注入描画） |
| 本体ホスティング（**雛形＋移行手順まで**。下記注意） | `infra/k8s/jetuse-api/`（Deployment/Service/ConfigMap/SA/Secret 雛形。kind で server-side dry-run 済）、`infra/k8s/README.md`（移行手順＋デモ runbook） |
| 運用ツール | `tools/render_injection.py`（注入マニフェストを `kubectl apply` に流す CLI） |
| テスト | `tests/test_deploy.py` / `tests/test_deploy_inject.py`（K8s 描画・注入の単体テスト追加・既存更新） |

## 3. 設計の核（基盤非依存・ADR-0017 で維持）

- **秘密非注入**: 注入物は base_url(非秘密)＋短期トークン(秘密)の 2 つのみ（キー allowlist）。DB 資格情報は
  L3 に配らない（D5）。デモ ConfigMap は非秘密 env のみ、短期トークンは **K8s Secret**。
- **二重閉包**: トークンのスコープ = 配備仕様 `required_scopes` ∩ 承認グラント。発行直後に自己検証。
  注入 CLI は発行前に再構築 spec の `required_scopes` **と発行プラグイン** を **実デプロイ Deployment の
  注釈（`jetuse.dev/required-scopes` / `jetuse.dev/plugin-id`＝ground truth）** と照合し、deploy 時と違う
  answers での宣言外スコープ上書き・**別プラグインのグラントへのすり替え**（運用迂回）を fail-closed で塞ぐ。
- **TTL/refresh**: 短期 TTL（既定 300s、broker 上限 900s）。更新は Secret 再 apply ＋ rolling restart。
- **秘密を IaC state に残さない**: 短期トークン Secret はコミット/Terraform/state を通さず、オーケストレータが
  アウトオブバンドで `kubectl apply`（TTL で揮発）。
- **1 デモ = 1 namespace**: 決定的命名（`jetuse-demo-<sample_app>`）＋ラベル規約で deploy/delete・棚卸しが trivial。
  ServiceAccount はトークン自動マウント無効（最小権限）＋ ResourceQuota（越境・暴走抑止）。

## 4. 検証結果

### 4.1 静的（lint / test）

- `ruff check .` … **All checks passed**（packages/api 全体）。
- `pytest`（packages/api）… **910 passed**, coverage 72%（gate 45%）。
  - 既存 deploy/deploy_inject テストを K8s 契約へ更新＋新規追加（manifest 描画・envFrom・Secret 注入・refresh・revoke）。
  - 注: `test_central_registry.py` / `test_plugin_publisher.py` は **sibling パッケージ `jetuse_registry` 未インストール**
    によるコレクションエラー（本タスクの変更とは無関係の環境要因。本ループ venv の既知事項）。除外して全緑。
  - 注: 型チェッカ（mypy/pyright）は本リポジトリに未設定（pyproject/CI に無し）。lint=ruff で代替。

### 4.2 OKE terraform（plan/validate）— 実 API に対して

証跡: `runs/2026-06-27T1509_DEP-03/e2e/terraform-oke-plan.log`（OCID redact 済）

- `terraform validate` … **Success**（modules/oke ＋ environments/oke）。
- `terraform plan`（実 jetuse-dev コンパートメント / ap-osaka-1）… **Plan: 25 to add**（既定=全プライベート）。
  - node image はクラスタ k8s 版に対応する OKE イメージを実 API から自動解決（緩い regex を版照合へ修正）。
  - service LB NSG は本体 Service の平文 HTTP に合わせ ingress 80/443 を許可（`lb_in_app` for_each。review F-002）。
  - **Internet Gateway は `need_public_rt`（public opt-in）時のみ作成**。private 既定は IGW を作らず、
    egress は **NAT Gateway（VCN レベル・public subnet 不要）＋ Service Gateway** で賄う（未使用リソースを残さない）。
  - セキュリティ境界は **NSG を主**とし、各 subnet の Security List は **default を使う**（OCI CCM が LB の
    listener/health-check 規則を SL に自動管理する OKE 標準動作と整合させるため。SL の絞り込みは §4.5 follow-up）。
  - **`admin_api_allowed_cidrs` に validation**（`0.0.0.0/0`・`::/0` 拒否＋CIDR 形式）を追加。`0.0.0.0/0` を
    渡すと plan が `Error: Invalid value for variable` で停止することを実証（ログ参照）。
  - public opt-in（`is_public_api_endpoint=true` / `service_lb_is_public=true`）でも plan グリーン（27 add：IGW＋public RT 追加）。
- **apply はしていない**（恒常課金＝人間ゲート。ADR-0017 §7 / `SKIPPED.md`）。

### 4.3 ローカル K8s（kind, podman provider）実機 E2E — 2 シナリオ

証跡: `runs/2026-06-27T1509_DEP-03/e2e/e2e-k8s.log`, `scenario-1-result.txt`, `scenario-2-result.txt`,
`scenario-1-demo-manifests.yaml`, `e2e_k8s.py`（再現スクリプト）

| シナリオ | 内容 | 結果 |
|----------|------|------|
| 1 | デモを K8s に deploy（tenant 指定で namespace に tenant ハッシュ＋`jetuse.dev/tenant` label・Secret 注入は `kubectl apply --server-side`）→ 短期トークンが **K8s Secret 経由で実行中 Pod の env に注入**（`printenv` で確認）→ Pod から注入 base_url の Platform API（in-cluster mock, 自己署名 TLS）へトークンを載せて疎通 → **別テナントが別 namespace になり 2 テナント共存（衝突なし）を確認** → `kubectl delete namespace` で撤去 | **PASS**（reachability HTTP 200・token scoped 検証・**Secret annotation に平文トークン非漏えい**・**テナント分離（namespace/Secret 非衝突）**・namespace 削除確認） |
| 2 | トークン更新（Secret 再 apply ＋ `rollout restart` で新トークンが新 Pod に反映）／承認失効後の次回 refresh が `GrantDenied(grant_revoked)` で fail-closed | **PASS**（pod hash 55c9f988c6→74447b9d98・token 変化・revoke で発行停止） |

補足（scenario 1 に内包・実 kubectl）: 注入 CLI `render_injection.py` の **live-check** を実機検証
（再構築 spec の `required_scopes` ＋ 発行プラグインが live Deployment 注釈と一致 → 通過、**plugin すり替え**
→ fail-closed、Deployment 不在 → fail-closed）。deploy 時と違う answers での宣言外スコープ上書き・
別プラグインへのすり替え（運用迂回）が塞がれることを ground truth ベースで確認。

E2E の限定事項（honest scope。詳細は `SKIPPED.md`）:
- デモ Pod イメージは OCIR 実イメージを kind で pull できないため curl テストイメージで代替（image/command/
  resources **および ResourceQuota の hard 値**を kind ノードに収まるよう差し替え）。**生成マニフェストの
  securityContext（runAsNonRoot＋runAsUser:10001 等）はパッチせず as-is で適用**し、Pod が起動・疎通する
  ことを確認（生成物そのものの起動性を検証）。なお `deploy.py` が生成する ResourceQuota の **値そのもの**は
  E2E では未適用（hard 値を縮小して apply）＝生成ロジックは unit（test_deploy）で担保。
  検証対象は **Secret/ConfigMap 注入・envFrom・命名・ライフサイクル・起動性**。アプリ本体ロジックではない。
- Platform API は in-cluster mock。承認グラントの DB 永続化は in-process スタブ（既存 unit が担保）。
  トークン署名/検証/二重閉包/TTL/K8s 注入/Pod 疎通/refresh/revoke は **実物**。

## 4.4 本体 OKE ホスティングの完了範囲（重要・honest scope）

JetUse 本体の OKE ホスティングは **manifest 雛形＋移行手順までを完了**とし、**OCI 認証 on OKE は未完**
（移行 follow-up・人間ゲート）。理由: 既存コードの OCI 認証は `AUTH_MODE=resource_principal`（Functions 用
env ベース signer）で **OKE Pod では成立しない**。OKE Workload Identity / instance principal への切替は
app 全体の signer 対応で、かつ GenAI 推論が third-party `oci_genai_auth` 依存のため **実 OKE でのみ検証可能**
（apply ゲート）。本タスクでは誤設定を避けるため configmap に `AUTH_MODE` を入れず、follow-up を ADR-0017 §影響 /
README §1 / SKIPPED.md に明記した。**本体を実 OKE に載せて OCI 呼び出しが通ることは本タスクでは未検証**。

## 4.5 残存事項（非ブロッカー・運用注意 / follow-up）

PASS 時点で残る指摘は実装の誤りではなく **運用前提の明示／実 OKE での検証待ち**：

- **Security List の NSG 一本化**: 現状は各 subnet に default Security List を使う（OCI CCM が LB の
  listener/health-check 規則を service_lb / worker subnet の SL に自動管理する OKE 標準動作と整合させるため）。
  SL を空にして **NSG を唯一の境界**にする厳格化は、OCI CCM の seclist 管理モードを無効化したうえで
  実 OKE で LB 疎通を確認する follow-up（kind には OCI CCM が無く検証不能）。
- **plugin binding の deploy 時強制（follow-up）**: 注入が必要（scoped）な spec に `plugin_id` が無いと、
  `build_runtime_injection` / `render_injection.py` が **fail-closed**（ground truth 無しの注入を拒否）する。
  実害（注入不能で Pod が起動待ち）は注入時の fail-closed と runbook（deploy で必ず `--plugin` を渡す）で
  構造的に防いでいる。さらに `build_deploy_spec` 自体が scoped かつ plugin 未指定の **生成を拒否**する
  deploy 時ガードは、既存テスト多数の改修を伴うため follow-up（多層防御の重複・非ブロッカー）。
- **デモ namespace の RBAC**: デモ Pod は K8s API を使わない（broker 経由）。専用 SA を
  `automountServiceAccountToken: false` で置き **Role/RoleBinding は作らない**（API 権限ゼロ＝最小権限）。
  ADR-0017 §4 を実装に整合させた。LimitRange は任意の追加ハードニング（Deployment の明示 requests/limits で代替）。

- **生成 Deployment の `runAsUser: 10001` と任意イメージの整合**: 生成物は非 root（UID 10001）を強制する。
  これは **JetUse がビルドするデモ／本体イメージ（Containerfile が `USER 10001`）前提**で、対応 UID を持たない
  サードパーティ任意イメージを `image_url` に渡すと起動に失敗しうる。MVP は配備対象を JetUse ビルド像に限定
  （命名・securityContext の決定性を優先）。任意イメージ対応は securityContext を spec で上書き可能にする
  follow-up（実 OKE で要検証）。
- **tenant_hash の衝突耐性**: 非秘密ハッシュを **48bit（sha256 先頭 12 hex。従来 32bit から拡張）** に。
  base prefix＋sample_app が同一のときのみテナント間衝突が問題になるが、48bit で実運用規模の衝突確率を実質無視
  できる水準にした（`TENANT_HASH_LEN`）。即時失効や厳密一意化が要るなら deployment_id 併用が follow-up。
- **本体 Service と svclb NSG の関連付け**: `infra/terraform/.../oke` は svclb NSG（ingress 80/443）を作るが、
  OCI CCM への関連付けは Service の `oci.oraclecloud.com/oci-network-security-groups` annotation 任意扱い
  （実 NSG OCID は apply 後に確定するためプレースホルダをコミットしない方針）。**実 OKE apply 後に当該
  annotation を付与**して LB↔worker の経路を NSG で固める（README §1・SKIPPED.md の手順）。既定の OCI LB は
  既定セキュリティで疎通するため疎通自体はブロックされないが、NSG 厳格運用では付与が必要。

## 5. 残る人間ゲート

1. **ADR-0017 の施主承認**（基盤選択の確定）。
2. **実 OKE クラスタ／ノードプール／VCN の `terraform apply`**（恒常課金）。
3. **IAM ポリシー／dynamic group**（OKE service / worker OCIR pull / CCM の LB 権限）の手動設定（`enable_iam=false` 維持）。
4. 実 OKE 上でのデモ deploy/疎通/refresh の再 E2E（クラスタ apply 後）。
5. コミット / PR / push。

実 apply 手順は `runs/2026-06-27T1509_DEP-03/e2e/SKIPPED.md` ＋ `infra/k8s/README.md` に記載。
