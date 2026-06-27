# タスク: DEP-03 OKE 基盤への移行（JetUse 本体＋生成デモ/ユースケースアプリ）

## ゴール
JetUse 本体と、生成デモ／インストール済みユースケースアプリ（L3）を **OKE（Oracle Kubernetes Engine）**へ移行する。
デモ/アプリは K8s ワークロード（Deployment/Helm release・namespace）として **deploy/delete が trivial** になる。
L3 実行基盤を Container Instances（DEP-01/02）から OKE へ置き換える（**ADR-0017 で基盤選択を確定・supersede**）。

## 対象 area
api（＋ infra/terraform: OKE クラスタ＋ノードプール）

## 依存
ステージ4（DEP-01/02 = Container Instances ベースライン）。**ADR-0017（OKE 基盤）起票・承認**。

## 仕様参照
docs/enhance/202607-demo-platform-plan.md §3（固定基盤）/ §10、ADR-0015/0016（CI 前提を OKE へ置換）、
memory `oke-migration-decision`。既存: `deploy.py`/`deploy_inject.py`（配備仕様・注入の基盤非依存な核を再利用）。

## 受け入れ条件（検証可能な述語で書く）
- [ ] **ADR-0017 起票**（OKE 基盤: クラスタ構成・JetUse 本体ホスティング・デモ namespace/RBAC・Secret 注入・ライフサイクル。ADR-0015/0016 の L3 基盤を supersede）
- [ ] `infra/terraform` に OKE クラスタ＋ノードプール（**plan まで**。apply・恒常課金は人間ゲート）
- [ ] `deploy.py`/`deploy_inject.py` の配備ターゲットを container-instance から **K8s manifest/Helm ＋ K8s Secret** に置換（秘密非注入・二重閉包・TTL・ブローカー一本のデータ注入の核は維持）
- [ ] デモの **deploy/delete**（Deployment/Helm install/uninstall・namespace 撤去）と **トークン更新**（Secret 更新＋投影反映 or rolling update）を K8s ネイティブで実装
- [ ] JetUse 本体の OKE ホスティング構成（現 compute/コンテナからの移行手順）
- [ ] api lint/型・terraform plan・既存テスト後方互換がクリーン

## E2E シナリオ（実環境 / jetuse-dev・複数）
**OKE クラスタ provisioning（apply・恒常課金）は人間ゲート**。クラスタが用意できれば実機 deploy/delete/refresh を、
不能なら kind/minikube 等のローカル K8s で manifest/Secret 注入を検証し、実 OKE は apply 後に再 E2E。
- [ ] シナリオ1: デモを K8s に deploy → Platform API 短期トークンが Secret 経由で注入され疎通 → delete で撤去
- [ ] シナリオ2: トークン更新（Secret 更新→投影反映 or rolling update）が機能。承認失効が次回更新で反映
- [ ] 実 OKE apply は人間ゲート → SKIPPED.md 明記

## 成果物
`docs/decisions/ADR-0017-*.md` ／ OKE terraform（plan）／ `deploy*.py` の K8s 化 ／ `docs/verification/DEP-03.md`

## 非ゴール / 制約
- 実 OKE クラスタ apply・課金は人間ゲート（恒常コスト）。本タスクは設計＋IaC plan＋K8s 化＋ローカル/実 E2E まで。
- spec-driven: specs/ にない判断は ADR 案（ADR-0017）。既存リソースは参照のみ。コミット/PR/push は人間承認後。
- **人間ゲート: ADR-0017 承認 / OKE クラスタ apply・恒常課金。**
