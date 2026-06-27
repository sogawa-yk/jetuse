# ADR-0015: 生成デモの L3 ホスト型コンテナ配備(実行基盤・SSO・データ注入)

日付: 2026-06-27
状態: **採用**(2026-06-27 施主承認。レビューで2点を反映 — §7・§8。DEP-01 で起票)。

> `docs/enhance/202607-demo-platform-plan.md` §11 が予告した「ADR-0015(S4): L3 ホスト型/既存資産
> オンボード(実行基盤・SSO・データ注入)」のうち、**L3 ホスト型デモの配備**部分を DEP-01 の実装範囲で
> 確定する。既存資産オンボード(伝ぴょん/No.1-RAG/SQL-Assist)は ASSET-01 へ分離する(別 ADR 追補)。

## 背景

ステージ1〜3 で「ヒアリング → 推薦 → 合成 → ガバナンス → プレビュー」までが宣言定義として成立した
(`synth.DemoComposition` / `governance.validate_governance`)。S4 の D8(デプロイ上限=コンテナ)に従い、
**生成デモを L3 ホスト型コンテナとして配備可能にする**段が必要になった。ここで決めるべきは「新規に
実行基盤や認可経路を作るのか、既存資産を再利用するのか」「秘密(DB 資格情報・トークン)をどう扱うか」
「Platform API への到達経路」の 3 点である。

確定済みの再利用可能資産:
- **ADR-0009**: 3 SDK 別ホスト型 ReAct コンテナ(`agent_*_app_ocid`)。SDK→Application OCID の解決規則。
- **ADR-0011**: 配布イメージは OCIR(ap-osaka-1, public)。Container Instance / Functions は認証なし pull。
- **ADR-0014 / platform_broker**: スコープ付き短期トークン。L3 は秘密鍵を持たずトークンのみ提示(D5)。
- **infra/terraform/modules/container-instance**: Container Instance を作る既存 Terraform モジュール。

## 決定(案)

1. **新規インフラを作らない(D8)**。生成デモの配備は、既存 container-instance モジュールへ渡す
   **宣言的な配備仕様(tfvars)を生成する**ことに閉じる。実行基盤・ネットワーク・認可経路は
   固定リファレンス基盤を再利用し、デモ側はアプリ層成果物(イメージ+env+スコープ)のみを差し込む。
2. **配備仕様の生成は決定的・fail-closed**(`jetuse_core/deploy.py`)。`composition.ok=False` や
   ガバナンス未通過の構成は配備仕様を作らない。出力は container-instance モジュール変数へ 1:1 写像。
3. **DEP-01 は秘密(実値も Vault OCID 参照も)を一切持たない**。配備仕様・tfvars・Terraform state に
   Vault secret OCID を残さない(機微な参照先の永続化を避ける)。代わりに配備仕様は
   **「要求する秘密の論理名」だけを宣言**(`required_secrets`)し、具体的な Vault OCID 解決と
   コンテナへの注入は **DEP-02(Platform API 注入)** が担う。秘密の表面は二重に絞る:
   (a) **required_secrets は allowlist**(`HOSTED_AGENT_CLIENT_SECRET` のみ)＝コンテナ自身のトークン
       取得用資格のみ許可し、broker 署名鍵・DB 資格情報など他の秘密名は全拒否(deploy.py＋TF 双方)。
   (b) **コンテナ env はキー名前空間 allowlist**(`OCI_REGION` か `JETUSE_*`)＋資格情報名ヒント拒否
       (PASS/KEY/TOKEN/AUTH/CERT/SECRET 等)＋Vault OCID 値の拒否で、env を秘密の運搬路にしない。
   **既知の残存リスク(受容)**: `environment_variables` は自由形式の文字列 map のため、
   `JETUSE_DEMO_NOTE="...<平文秘密>..."` のように**資格情報名でない非秘密キーに秘密の実値**を手書きで
   入れる経路は、キー名検証では原理的に塞げない。これは (i) 配備仕様は deploy.py が生成し手書きを前提と
   しない、(ii) D5 によりコンテナは DB ネットワーク経路も DB アカウントも持たず、仮に資格情報が漏れても
   テナントデータへ到達できない(broker 経由の短期トークンのみが経路)、で緩和する。env は「非秘密の
   宣言値」という契約であり、秘密はすべて DEP-02 の注入経路で扱う。
4. **データ注入は Platform API ブローカー経由(D5)**。デモコンテナへ DB 資格情報を配らない。配備仕様には
   付与予定スコープ(`required_scopes` = active コネクタ束縛由来)を記録するに留め、実トークン発行・
   実注入の配管は **DEP-02** に委ねる(本 ADR は「経路はブローカー一本」を確定し直接 DB 接続を禁ずる)。
5. **SSO/実行 ID**: ホスト型実行 ID(IDCS OAuth=`jetuse-agent`)は ADR-0009 の 3 コンテナ共用方式を踏襲し、
   デモ専用 ID は増やさない。デモ固有の認可はブローカーのスコープで表現する(ID ではなくスコープで絞る)。
6. **apply は人間ゲート**。**配備構成(HCL/変数/モジュール結線)の妥当性**を `terraform plan` で静的検証し
   (実在性検証や実起動の保証ではない)、実 apply(課金・実コンテナ作成)・
   OCIR への実イメージ push・実 Vault secret 作成はすべて人間承認後に行う。
7. **デモ配備のライフサイクル(更新/破棄)は実 apply 前に DEP-02 で確定する**(施主レビュー 2026-06-27)。
   実コンテナを起こす前に「更新(再配備)・破棄(teardown)・命名/タグ規約」を決め、デモコンテナが増え続けない
   ことを担保する(CLAUDE.md「むやみにリソースを増やさない」)。実 apply はこの規約確定が前提。
8. **既存資産オンボード(伝ぴょん/No.1-RAG/SQL-Assist)は本 ADR の対象外**。ASSET-01 着手時に
   **追補 ADR を起票**して実行基盤・SSO・データ注入を別途確定する(施主レビュー 2026-06-27。plan §11 が
   予告した ADR-0015 の既存資産部分はこの追補で扱う)。

## 理由

- 固定リファレンス基盤(D8)を崩さないことが「外れないデモ」の前提。新規実行基盤を作ると基盤保証が効かない。
- fail-closed＋秘密の OCID 参照化で、ガバナンス未通過構成やリポジトリ秘密混入を構造的に防ぐ(最小権限・越境防止)。
- ブローカー一本化(D5)で L3 が DB 資格情報を直接持たない構成を維持できる(ADR-0014 と整合)。

## 却下した代替案

- **A. デモごとに専用 VCN/サブネット/認可基盤を Terraform 生成**: D8 違反。基盤が可変になり保証が崩れる。
- **B. 秘密を tfvars/環境変数に実値で注入**: リポジトリ・state への秘密漏えいリスク。OCID 参照で回避。
- **C. デモコンテナへ DB 資格情報を直接配布**: D5(越境・最小権限)違反。ブローカー経由に一本化。

## 影響範囲 / 未決事項

- 影響: `jetuse_core/deploy.py`(新規)、`infra/terraform/environments/hosted-demo/`(新規, 既存モジュール consume)、
  `settings.hosted_demo_image_url`(新規・空既定)。既存公開シグネチャは不変(追加のみ)。
- 未決(DEP-02 以降): 実トークン発行のタイミング/失効、デモ単位の Vault secret 命名規約、マルチテナンシでの
  OCIR ミラー(ADR-0011 既知制約)、デモ配備のライフサイクル(更新/破棄)管理。**S4 着手時に対話で確定**。
