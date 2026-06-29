# ADR-0020: コネクタ invoke 経路の解放（BE-03）— Slack コア限定・呼出権スコープ・秘密束縛

- ステータス: **承認済（2026-06-29 施主承認）**。方式A（消費デモ manifest が `platform:connector.invoke`
  を宣言・実行はコア builtin Slack 限定・秘密は tenant+plugin+connector 束縛）を採用し、下記 R3 を解消した。
- 日付: 2026-06-29
- ADR 番号: 当初 ADR-0019 として起草したが ADR-0019 は BE-04 が使用済みのため ADR-0020 に採番し直した
  （ADR-0018=mcp-auth, ADR-0019=BE-04）。
- 関連: specs/16-platform.md §12.6 / §13.7、ADR-0014（Platform API 認可）、CON-02（invoke 層・Slack コア）、
  BE-03（`/api/platform/connector/invoke` の 501 解除）。review: `runs/2026-06-29T0755_BE-03/reviews/review-1.json`。

## 背景

BE-03 は `connector_runtime.http_caller` の実 HTTP 化・`/api/platform/connector/invoke` の 501 解除・
secret の Vault 解決を行う。Codex レビュー（review-1）が、501 を解いて**実 invoke を開放**するにあたり
spec §12.6/§13.7 のままでは塞げない安全/到達性ギャップを指摘した。本 ADR はその解消のための
**spec からの逸脱を伴う決定**を提案する（spec-driven 原則によりコード先行ではなく承認を仰ぐ）。

## 決定（提案）

### D1. コネクタ manifest は `platform:connector.invoke` を宣言する（spec §12.6 の更新提案）
spec §12.6 はコア Slack manifest の `permissions` を空とし「呼ぶ権利は invoke 層が常に強制」とした。
しかし承認フロー（`platform_grants.approve_scopes`）は **manifest.permissions ∩ PLATFORM_SCOPES** に
閉じるため、`permissions=[]` だと `platform:connector.invoke` を**承認・発行できず**、正規の
`issue_token` 経由でルートに提示できるトークンを発行できない（=実行経路が不達。review BLK-003）。
→ **コネクタ manifest は `platform:connector.invoke` を top-level `permissions` に宣言する**。
action 固有の Platform データスコープは引き続き action.permissions で宣言（Slack は空）。
合成バリデーション（`validate_connector_composition`）は `connector.invoke` を **unused 扱いしない**
（コネクタの呼出権として正当な宣言）。

> **訂正（review-6 BLK-001）**: コネクタ manifest が `connector.invoke` を宣言しても、それは
> **コネクタ所有 plugin** のグラント語彙に入るだけで、実際に invoke を**呼ぶ消費側 L3 デモ**
> （別 plugin_id）が invoke スコープを得られるわけではない（grant は `(tenant, plugin_id)` 単位、
> `issue_token(tenant, caller)` は caller のグラントを読む）。**消費側の到達性は D6** が担う。コネクタ
> manifest の宣言は、デモの connector binding 由来 `required_scopes` 算出（`connector_invoke_scopes`）と
> 整合させるための宣言として残す。

### D2. BE-03 の実行対象は「コア同梱 builtin Slack コネクタ」に限定する（fail-closed）
ルートはコア plugin（`jetuse/slack-connector`）所属かつ `transport=builtin` のコネクタだけを実行し、
それ以外（MCP transport / サードパーティ builtin）は **501** に倒す。理由:
- MCP の実接続は spec §12.1/§12.6 で「実行時 SSRF ガード（DNS 解決後の公開判定・再バインド対策）は
  CON-03」と明記された非ゴール。実行時ガード無しで MCP を同時開放しない（review MAJ-001）。
- BE-03 の対象は「Slack コアの実体化」であり、Slack 以外網羅は非ゴール（tasks/BE-03）。

### D3. 秘密解決をテナント＋プラグイン＋コネクタ instance に束縛する（confused-deputy / 越境 / 取り違え防止）
`secretRef`→Vault OCID の対応表（`settings.connector_secret_ocids`）の鍵を
**`<tenant>/<plugin_id>/<connector_id>/<secretRef>`** の合成キーにする（review-2/3 BLK-001）。これにより:
- 別プラグインが同名 `secretRef`（例 `slack-bot-token`）を宣言しても他人の秘密を解決できない。
- 別テナントが同一コア plugin のトークンで他テナントの SaaS 資格情報を共有/越境できない（テナント別 secret）。
- **同一テナント内に同一 plugin の Slack 接続が複数あっても、`connector_id` ごとに別 secret を解決**し、
  別ワークスペースへの誤送信/取り違えを防ぐ。
未マップ鍵は fail-closed（`SecretResolutionError`→503）。`connector_id` は invoke 要求の instance id。
鍵の `<plugin_id>` は **invoke を呼ぶ L3 デモ自身**（`ctx.plugin_id`＝トークン sub）であり、コネクタ
所有 plugin ではない（D4 参照）。よって秘密は (tenant, 呼出デモ, connector) 単位で別管理され、別デモ・
別テナントは互いの Bot トークンを解決できない。復号値は Bearer 不正（内部空白/制御文字/非 ASCII）も
fail-closed（503）に倒す（httpx で 502/400 へ化けるのを防ぐ。review-5 MIN-001）。

### D4. 認可は invoke 層に一元化し、ルートはバイパスを持たない
`invoke_connector_action` は **常に** broker 認可（検証→scope→tenant→監査）を行い、認可をスキップする
バイパス引数を持たない（偽造 BrokerContext で外部副作用を起こせない。review MAJ-002）。ルートは
取得前 authorize（検証→scope→tenant→監査）を行い、委譲先でも再認可する（多層防御。同一スコープの
ALLOW 監査は route+invoke で各1回出るが許容）。

**呼出主体の所有一致は要求しない（review-6 BLK-001 で訂正）**: 当初ルートは
「connector 所有 plugin == トークン sub」を要求していたが、コア Slack は単一プラグイン
（`jetuse/slack-connector`）が所有する**共有 capability**で、実際の呼出主体は connector.invoke を
承認された**別の L3 デモ**（`build_runtime_injection` が発行するトークン sub=デモ自身の plugin_id
≠ Slack）である。所有一致を強制すると正規デモ呼出が必ず 403 になり主要経路が到達不能になるため、
この所有チェックは撤去した。**呼出ごとの境界は (a) tenant 認可、(b) コア限定（D2）、(c) secret 束縛
（D3。鍵 `{tenant}/{呼出 plugin}/{connector}/{ref}`）が担う**。未プロビジョンの
(tenant, 呼出 plugin, connector) は secret 解決不能で 503 となり Slack へ到達しない（fail-closed）。
connector 行は秘密を持たない（`secret_ref` のみ）ため、所有チェック撤去で秘密露出面は増えない。

### D5. エラー分類（HTTP 写像）
secret 解決不能・Vault/IAM/設定障害 → **503**（サーバー/依存側）、外部 SaaS 到達/応答障害 → **502**、
**未登録 connector / 未知 action → 404**（リソース不在の意味論。ルートが取得・存在検証で返す。既存 PAPI-03
契約と後方互換）、版固定スナップショット不整合（旧 source_version / provider・transport 不一致）→ **409**
（再インストール要求。MAJ-001）、payload 不正・SaaS 論理エラー（channel_not_found 等）→ **400**、
認可拒否 → 401/403。副作用不確定なサーバー障害を恒久的 400 に潰さない（監視・再試行判断を誤らせない。
review MAJ-004）。**補足**: 低層 `invoke_connector_action` を直接呼んだ場合の未知 action は構成不備
`ConnectorInvokeError`（直接呼出時の既定 400 相当）だが、HTTP ルートは取得段で存在検証して 404 を返す
（公開 HTTP 契約は 404。review-2 MIN-002 整合）。

### D6. invoke スコープは「呼ぶ消費側 plugin の manifest」で宣言・承認・発行する（到達性モデル）
`platform:connector.invoke` は `PlatformScope` 語彙の一員で **kind 制約を持たない**（manifest schema 上
任意の kind が `permissions` に宣言できる）。コネクタを束ねる消費側 L3 デモは、binding 由来の
`required_scopes`（`connector_invoke_scopes` が `connector.invoke` を含める）と整合するよう、**自身の
manifest.permissions に `platform:connector.invoke` を宣言する**。これにより:
- `approve_scopes`/`validate_grant_scopes`（`manifest.permissions` に閉じる）で **デモ plugin に対して**
  invoke を承認でき、
- `platform_grants.issue_token(tenant, デモ plugin)` がデモ sub の短期トークンを発行でき、
- そのトークンで `/connector/invoke` に到達できる（route は所有一致を要求しない＝D4）。

宣言しない消費 plugin は invoke を承認されず（迂回不可）、`build_runtime_injection` でのトークン発行も
`scope_not_granted` で失敗する（fail-closed）。検証は E2E scenario-2（消費デモ manifest 宣言→
`validate_grant_scopes`→`issue_token`→200、宣言なしは承認拒否の否定対照）＋単体
`test_platform_grants.py`（非 connector plugin への invoke 承認可否）で実証済み。

## 残課題（人間ゲート / 後続）

- **R1. connector_instances のテナント束縛（review BLK-002）**: `connector_instances` は tenant 列を
  持たず、install 経路は Project OCID ではなく `owner`（インストール者）で取込む。同一 plugin_id が
  複数テナントで使われると別テナントの instance を invoke し得る。本 ADR は「実行対象を**コア Slack
  （Platform データに触れない SaaS ブリッジ）に限定**」することで、テナントデータ漏洩の実害面を当面
  塞ぐ（Slack コネクタが解決する秘密は plugin 自身の Bot トークンで、テナントデータではない）。
  tenant→resource の物理束縛（connector_instances への tenant 列追加 + install への Project OCID 伝播）は
  **INFRA / CON-03 の範囲**（spec §13.7 の「tenant 単位の物理隔離は INFRA で上乗せ」と整合）として
  人間承認のうえ別タスクで実装する。tenant データに触れるコネクタの一般開放はそれまで行わない。
- **R2. 実 Slack / 実 Vault / IAM**: 実 Bot トークン投入・Vault secret 化・`secrets:read` IAM 付与は
  人間ゲート（`runs/2026-06-29T0916_BE-03/e2e/SKIPPED.md`）。
- **R3.（解消済 / 2026-06-29 施主承認 → D7 へ昇格）消費デモの invoke 到達性の実装方式**:
  当初は「消費デモ自身の manifest が `platform:connector.invoke` を宣言する」（D6）を到達性モデルとしたが、
  出荷サンプルアプリ（SBA-A/B/C）がこれを宣言していなかったため synth→deploy→`issue_token` が
  `scope_not_granted` で実 invoke に到達しなかった（review-3 BLK-001）。**施主は方式A を採用**した（下記 D7）。
  方式B（synth 時の自動注入＝fail-closed 原則に反する）・方式C（承認オーケストレーション層の新設＝統治設計が要る）
  は不採用。governance 強制（connector を束ねるデモは invoke 宣言を必須とする）は方式A の担保として D7 に含める。

### D7. Slack を束ねる出荷サンプルアプリの manifest が `platform:connector.invoke` を宣言する（方式A・採用）
施主承認（2026-06-29）に基づき、**Slack コネクタを binding する出荷消費デモの manifest.permissions に
`platform:connector.invoke` を宣言する**。これにより `approve_scopes`/`validate_grant_scopes`
（`manifest.permissions` に閉じる）が当該デモへ invoke を承認でき、`issue_token(tenant, デモ plugin)` が
デモ sub の短期トークンを発行でき、`/connector/invoke`（route は所有一致を要求しない＝D4）に到達する。
- **どのデモに付与するか**は製品判断であり、最小権限を保つ（Slack を実際に束ねるデモにのみ宣言する）。
- **governance 担保**: connector を binding しながら invoke を宣言しないデモは composition バリデーションで
  欠落として検出する（binding 由来 `required_scopes`（`connector_invoke_scopes`）に対し manifest.permissions が
  invoke を欠く＝宣言漏れ）。宣言しないデモは承認・発行されず実 invoke に到達しない（fail-closed・迂回不可）。
- **検証**: 実際の出荷サンプルアプリ manifest を使った synth→approve→issue_token→route の統合テストで
  到達性を実証する（review-3 BLK-001 の指摘に対応。架空 manifest による迂回を排除）。

## 影響

- 変更: `slack_connector_builtin._MANIFEST.permissions`、`connector.validate_connector_composition`、
  `service/routes/platform.py`（invoke ルート）、`connector_runtime`（live_http_caller / Vault resolver /
  エラー型）、`settings.connector_secret_ocids`。
- 方式A（D7）の変更: Slack を束ねる出荷消費デモ manifest の `permissions` に `platform:connector.invoke`
  を宣言（該当 sample-app builder）。governance に「connector を binding しながら invoke を未宣言のデモは
  違反」とする検査を追加（方式A の担保。binding 由来 `connector_invoke_scopes` ⊄ manifest.permissions ＝欠落）。
- 後方互換: invoke を宣言しない既存コネクタの合成は不変（unused 計算から invoke を除くだけ）。
  invoke_connector_action の公開シグネチャは CON-02 と同一（バイパス引数は追加しない）。Slack を束ねない
  既存 sample-app の manifest.permissions は不変（invoke を宣言する必要がない＝最小権限を保つ）。
