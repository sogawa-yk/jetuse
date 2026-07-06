# ADR-0013: デモ生成プラットフォームのアーキテクチャ（3層プラグイン／中央レジストリ／スナップショット取込／Platform API／ガバナンス4制約）

日付: 2026-06-25
状態: Superseded by ADR-0015（2026-07-06。dev リセット後のフレッシュ再設計により置換）

## 背景

`docs/enhance/202607-demo-platform-plan.md` が「JetUse をデモ生成プラットフォーム化する」実装計画を
定義した（主ペルソナ＝フィールドSA、§1）。プリセールスが「リファレンスから外れない」業務デモを
ガイドに沿って短時間で組める体験を狙う。方式比較は `docs/comparison/marketplace-plugin.md` に既出。

ステージ1（MVP）の起点 PLG-01 は配布単位 manifest の確定であり、その前提となる全体アーキテクチャの
基本構造をここで固定する。本 ADR は実装そのものではなく、後続タスク（PLG-02..08 / SBA-01..05）が
従う設計の枠を定める。

## 決定

### 1. プラグインは 3 層（L1/L2/L3）

配布素材を能力の重さで 3 層に分ける。MVP は **L1 のみ**実装する。

- **L1 宣言型**（`kind: usecase | agent`）: コードを持たない宣言。既存エンジン（UC-01 / AGT）に流し込む。
- **L2 MCP コネクタ**（`kind: tool`=connector）: MCP で正規化した外部接続。コア=Slack 1 本＋マーケット拡張。
- **L3 ホスト型**（`kind: hosted-app`）: コンテナ配備アプリ。Platform API 経由でテナントデータへ到達。

`sample-app`（scaffold テンプレ）と `bundle` も種別として予約するが、MVP の manifest 検証は L1
サブセット（usecase/agent）に限定する（specs/16-platform.md）。

### 2. 中央レジストリ（D2）

ベンダー運用の **Object Storage ＋ `index.json` ＋ 発行者公開鍵**。読取は公開、publish は発行者認証＋
署名必須。レジストリ実体は PLG-04（`packages/registry`、Terraform は plan まで・人間ゲート）。

### 3. スナップショット取込（D6）

インストールは**版固定のスナップショット取込**とする。取込先に `installed_plugins` を記録し、取り込んだ
定義に `source_plugin_id/version` を刻む（PLG-02）。レジストリ側の更新が黙って反映されることはない
（再現性とガバナンスのため）。

### 4. 署名と検証（D7）

発行者 ID ＋ **ed25519 署名**。署名対象は manifest の正準バイト列（signature を除く、キーソート・
最小区切り）。取込時にレジストリ取得の公開鍵で検証し、**検証失敗・未署名は取込拒否**（PLG-03）。
manifest の構文 valid と署名 valid は別レイヤ（未署名でも構文上は valid、取込ポリシーで弾く）。

### 5. Platform API ブローカー（④ / D5・D3前提）

L2/L3／生成デモが **DB 認証情報を持たずに**テナントデータへ到達する唯一の正規経路。スコープ語彙は
`platform:rag.search` / `platform:db.query` / `platform:conversations.read` / `platform:files.read|write` /
`platform:connector.invoke`。manifest の `permissions` はこの集合の部分集合に限る。呼び出しごとに
短期 JWT（テナント＝Project OCID・プラグインID・付与スコープ内包）を発行し、全アクセスをブローカー
経由にして越境防止・レート制限・監査（`audit.py`/`guardrails.py`）へ接続する。実装は後続ステージ（S3）。

### 6. ガバナンス4制約 — IaC 検証（Galley的アプローチ）は採らない

「リファレンスから外さない」を、生成物の IaC をツールで検証する方式ではなく **4 つの構造的制約**で
担保する（§4）:

1. **固定リファレンス基盤（D8）**: JetUse 自体を機能カットのリファレンスアーキテクチャとし、これを
   そのまま固定基盤とする（新規構築しない）。SA はインフラを選べない＝変なアーキにできない。
2. **宣言型素材（L1）＋正規化コネクタ（L2 MCP）**: 自由記述コードではなく宣言と正規化境界に閉じる。
3. **Platform API ブローカー経由のデータアクセス**: DB 直結を許さず、付与スコープ内に閉じる。
4. **署名つき配布＋版固定スナップショット**: 出所と版を追跡可能にし、未署名・改ざんを取込で拒否。

→ **IaC を生成・検証する Galley 方式は不採用**。プラットフォームはインフラを生成せず、既存 JetUse
基盤の上でのみ動く。これが §1 の動機（リファレンスから外れない）への直接の解。

## 影響

- PLG-01: `jetuse_core/plugins/manifest.py`＋`specs/16-platform.md`（本 ADR と同時起票）。
- PLG-02 以降: `installed_plugins`・`source_plugin_id/version`・レジストリクライアント・Platform API は
  本 ADR の枠に従う。
- 既存エンジン（usecases / agents）は L1 contributes の受け皿となる（PLG-07 で合算返却）。

## 非ゴール / 留保

- L2/L3 の manifest 拡張、permissions 承認フロー、短期 JWT 実装は後続ステージ。
- レジストリの Terraform apply・課金は人間ゲート（PLG-04）。
- 本 ADR の採択自体が人間ゲート（タスク PLG-01 の承認条件）。

## 代替案

- **Galley 的 IaC 検証**: 生成 IaC を検証して逸脱を弾く。却下 — プラットフォームがインフラを生成しない
  方針（固定リファレンス基盤）と矛盾し、検証の網羅も困難。構造制約の方が確実かつ単純。
- **L1 で任意コード許可**: 表現力は上がるがガバナンスとサンドボックスのコストが跳ね上がる。MVP は
  宣言型に閉じ、コードが要る場合のみ L3（コンテナ＋Platform API）へ寄せる。
