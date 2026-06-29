# specs/16: デモ生成プラットフォーム — プラグイン manifest 仕様（PLG-01）

> 出典: `docs/enhance/202607-demo-platform-plan.md` §4・§6・§10／`docs/comparison/marketplace-plugin.md`。
> 設計判断は `docs/decisions/ADR-0013`。本仕様は PLG-01 で **L1 宣言型サブセット**（`kind: usecase | agent`）を
> 確定し、以降 **`sample-app`（§10 / SBA-01）・`connector`（L2 MCP / §12 / CON-01）** を追加した。
> `hosted-app`(L3)・bundle は後続タスクで拡張する。
> 実装は `jetuse_core/plugins/manifest.py`（pydantic モデル＋JSON Schema＋検証。kind 別詳細は §10/§12 の専用モジュール）。

## 1. 位置づけ

プラグインは「配布可能な素材の最小単位」である。中央レジストリ（D2、ベンダー運用の Object Storage
＋`index.json`＋発行者公開鍵）に publish され、各インスタンスがスナップショット取込（D6、版固定）する。
公開・取込は発行者の ed25519 署名（D7）で真正性を担保する。

manifest はこの配布単位を記述する宣言であり、**コードではなく宣言**である（L1）。本仕様は manifest の
スキーマと検証規則のみを定める。レジストリ通信（PLG-03/04）・UI（PLG-06）・データモデル（PLG-02）は
別タスク。

## 2. 配布表現

manifest は camelCase の JSON として配布される（例 `schemaVersion`, `jetuse.minVersion`）。
pydantic モデルは alias で受理し、`model_dump(by_alias=True)` で同じ表現に戻す。JSON Schema は
`manifest_json_schema()` が camelCase 別名で出力する。

## 3. フィールド仕様

| フィールド | 型 | 必須 | 規則 |
|---|---|---|---|
| `schemaVersion` | string | ✓ | 現行は `"1"` 固定。後方非互換変更で繰り上げ。 |
| `id` | string | ✓ | `namespace/name`。各セグメントは `[a-z0-9]` とハイフン（端はハイフン不可）。最大 255 文字。 |
| `version` | string | ✓ | semver.org 準拠（MAJOR.MINOR.PATCH[-prerelease][+build]）。最大 64 文字。 |
| `kind` | string | ✓ | `usecase` \| `agent` \| `sample-app`(§10) \| `connector`(§12) \| `external-app`(§14)。 |
| `name` | string | ✓ | 非空（表示名）。 |
| `description` | string | – | 既定 `""`。 |
| `publisher` | string | ✓ | 発行者 ID（非空）。レジストリの発行者認証と対応。 |
| `jetuse.minVersion` | string | ✓ | ホスト JetUse の最低バージョン（semver）。 |
| `requires.models` | string[] | – | 必要モデル ID（取込時に解決可否を確認するための宣言）。 |
| `requires.datasources` | string[] | – | 必要データソース。 |
| `requires.tools` | string[] | – | 必要ツール。 |
| `permissions` | string[] | – | Platform API スコープの部分集合（§4）。重複・未知スコープは不可。 |
| `contributes` | object | ✓ | `kind` と同名のキーを **ちょうど1つ** 持つ宣言型ペイロード（§5）。 |
| `icon` | string\|null | – | 表示アイコン。 |
| `tags` | string[] | – | 検索・分類用。 |
| `license` | string\|null | – | ライセンス識別子。 |
| `signature` | object\|null | – | 発行者署名（§6）。未署名も構文上は valid だが取込時に拒否されうる。 |

未知のトップレベルキーは拒否する（`extra="forbid"`）。

`id`/`version` の長さ上限は永続化層（`installed_plugins` および取込定義の
`source_plugin_id`/`source_version`。ADR-0013 / PLG-02）の VARCHAR2 カラム幅と一致させ、
検証を通った manifest が必ず保存できる（保存時に桁超過しない）ことを保証する。

## 4. permissions（Platform API スコープ）

manifest が要求できるスコープは Platform API ブローカー（§7）の語彙に限る。許可集合
（`PLATFORM_SCOPES`）:

- `platform:rag.search` — RAG 検索（File Search）
- `platform:db.query` — DB 読取照会（NL2SQL）
- `platform:conversations.read` — 会話履歴の読取
- `platform:files.read` / `platform:files.write` — ファイル入出力
- `platform:connector.invoke` — L2 コネクタ呼び出し

集合外のスコープ、または重複を含む `permissions` は検証エラー。承認はインストール／合成時に行う
（本タスクの非ゴール）。

## 5. contributes（kind 別ペイロード）

`contributes` は `kind` と同名のキーを **ちょうど1つ** 持つ。

- `kind: usecase` → `contributes.usecase`（UC-01 の definition: fields/template 等）
- `kind: agent` → `contributes.agent`（instructions/tools 等）
- `kind: sample-app` → `contributes["sample-app"]`（screens/datasets/aiSlots。§10）
- `kind: connector` → `contributes.connector`（provider/transport/actions/auth。§12）

`kind` に一致しないキー、または複数キーは検証エラー。`usecase`/`agent` の内部構造の詳細スキーマは
各エンジン（usecases / agents）側に委ね、manifest.py では「kind とキーの対応」までを強制する。
`sample-app`/`connector` の詳細スキーマは専用モジュール（§10/§12）が担う。

## 6. signature（発行者署名 / D7）

| サブフィールド | 規則 |
|---|---|
| `algorithm` | `ed25519` のみ。 |
| `publicKeyId` | 非空。レジストリ `index.json` の発行者公開鍵と突き合わせる識別子。 |
| `value` | base64 エンコードした **64 バイト** の ed25519 署名。 |

署名対象は `canonical_signing_payload()` が返す **正準バイト列**＝ 検証後の manifest から `signature`
**のみ**を除いた全フィールド（既定値は注入済み、任意フィールドの未指定は `null` として保持。`exclude_none`
しない）を `sort_keys=True` ・区切り `(",", ":")` で JSON 直列化したもの。署名の有無で正準ペイロードは
不変であり、発行側・検証側（別実装含む）で同一バイト列を再現できる。

**前方互換の契約**: 正準ペイロードの形（含まれるフィールド集合）は `schemaVersion` に紐づく。
`schemaVersion=1` の間はフィールド集合を変更しない（任意フィールドの追加・除去・既定値変更を含む）。
変更が必要になったら `schemaVersion` を繰り上げる。これにより、既存の署名済み manifest の検証が
本体のフィールド追加で壊れることを防ぐ。

**正準化規則（schemaVersion=1）**: 別実装の publisher が同一バイト列を再現できるよう、以下を固定する。

- 文字符号化: UTF-8。文字列は **エスケープせず素の UTF-8**（`ensure_ascii=False`）。
- 構造: `signature` を除く全フィールドを含むオブジェクト。**キーは再帰的に辞書順ソート**（`sort_keys`）。
- 空白: キー・値・要素間に空白を入れない（区切りは `","` と `":"`）。
- 数値: 整数はそのまま。**非有限数（NaN/Infinity）は不許可**（`contributes` の値も含め検証時に拒否）。
  浮動小数点の文字列表現（例 `1e-06` と `0.000001`）の差は、単一実装（Python）の MVP では発行・検証が
  同一実装のため問題にならない。第三者 publisher を伴う相互運用（PLG-04）では JCS/RFC 8785 等の数値
  正準化を ADR で確定する（それまで浮動小数を含む payload の他実装署名は非対象）。
- 取込時に検証を通った manifest は必ずこの正準化が成功する（非 JSON 値を validator が排除するため）。

> 第三者 publisher を伴う中央レジストリ（PLG-04）導入時は、より厳密な相互運用標準（JCS / RFC 8785）の
> 採用を ADR で再評価する。現行 MVP は上記規則で発行側・検証側を一致させる。

`verify_signature(manifest, public_key_bytes)` は 32 バイト raw 公開鍵で検証し、署名なし・鍵不正・
検証失敗・改ざんのいずれでも `False` を返す（例外を投げない）。取込（PLG-03）はこれが `False` の
manifest を拒否する。**manifest の構文 valid と署名の valid は別**であり、未署名 manifest は構文上は
受理されるが取込ポリシーで弾く。

## 7. 公開 API（`jetuse_core/plugins/manifest.py`）

| シンボル | 役割 |
|---|---|
| `PluginManifest` | pydantic ルートモデル。 |
| `validate_manifest(data) -> PluginManifest` | dict を検証。不正なら `ManifestError`。 |
| `manifest_json_schema() -> dict` | 配布・ドキュメント用 JSON Schema（camelCase）。 |
| `canonical_signing_payload(manifest) -> bytes` | 署名対象の正準バイト列。 |
| `verify_signature(manifest, public_key: bytes) -> bool` | ed25519 署名検証。 |
| `SCHEMA_VERSION` / `PLUGIN_KINDS` / `PLATFORM_SCOPES` | 仕様定数。 |

`manifest_json_schema()` は `schemaVersion` const・`kind`/`permissions` enum・`id`/`version` pattern・
`contributes` の `maxProperties:1`・`signature.value` の base64 注記までを構造的に表現する。ただし
**cross-field 制約**（`contributes` のキーが `kind` と一致すること、`permissions` の重複禁止、
`signature.value` が厳密に 64 バイトであること）は JSON Schema で簡潔に表せないため
**`validate_manifest()` が正本**。外部ツールは JSON Schema を一次フィルタとし、最終判定は
`validate_manifest()` に委ねること。

## 8. 検証（受け入れ）

`packages/api/tests/test_plugin_manifest.py` が正常系（usecase / agent / 署名つき往復）＋不正 manifest
拒否（schemaVersion/id/version/kind/permissions/contributes/署名フィールド/必須欠落/改ざん）を網羅する。

## 9. 非ゴール

- レジストリ通信・UI・インストール処理（PLG-03..08）。
- `kind` の L3 拡張（hosted-app/bundle）。
- permissions の承認フロー・短期 JWT 発行（§7 Platform API、後続ステージ）。

> 注: PLG-01 当初の非ゴールだった `kind: sample-app` は SBA-01 で追加した（§10）。
> `kind: connector`（L2 MCP）は CON-01 で追加した（§12）。permissions の承認フロー・短期 JWT 発行は
> PAPI-01（認可コア）／PAPI-02（承認＋発行フロー）で実装した（§13）。

## 10. kind: sample-app（scaffold テンプレ / SBA-01）

`kind: sample-app` は §6 D9 の「サンプル業務アプリ」を表す配布種別。`contributes["sample-app"]`
ペイロードは **UI テンプレ（screens）＋データモデル/シード（datasets）＋AI 組込スロット（aiSlots）**
を宣言する。実装は `jetuse_core/plugins/sample_app.py`（定義スキーマ＋合成バリデーション土台）と
`jetuse_core/plugins/scaffold.py`（インスタンスへの展開）。

### 10.1 contributes["sample-app"] スキーマ

| キー | 型 | 必須 | 規則 |
|---|---|---|---|
| `screens` | object[] | ✓ | 1..50。`key`(小文字英数-)・`title`・`type`(list/detail/form/dashboard/board)。`dataset`(任意, datasets を参照)・`slots`(aiSlots のキー配列)。 |
| `datasets` | object[] | – | 0..30。`name`(小文字 snake)・`fields`(1..60, name/type/required)・`seed`(行配列, dataset 毎 ≤1000・全体 ≤5000)。 |
| `aiSlots` | object[] | – | 0..50。`key`・`title`・`capability`(下記語彙)・`permissions`(Platform スコープ部分集合)。 |
| `summary` | string | – | 表示用説明（≤2000）。 |

cross-field 規則: screen が参照する `dataset`/`slots` は実在必須。キー（screen/dataset/aiSlot）は一意。
seed 行は宣言フィールドのみを持ち、必須フィールド非空、**値は宣言した型に整合**（number/boolean/
date/datetime の形式を検証）。検証済み定義は正準 JSON 化可能（manifest 署名往復の前提）。

### 10.2 capability 語彙（AI 組込スロット）

aiSlot が要求できる JetUse コア能力（§6 のサンプルアプリ表「使う JetUse 能力」に対応）:
`rag.search` / `summarize` / `classify` / `nl2sql` / `chart` / `agent` / `minutes` / `draft` /
`vlm.ocr`。集合外は検証エラー。

### 10.3 合成バリデーション土台（HBD-04 の前段）

`validate_composition(manifest, available_capabilities=...)` が以下を判定する:
- `missing_capabilities`: aiSlots が要求する能力のうちホストが備えないもの（致命）。
- `undeclared_permissions`: aiSlot が要求するが manifest.permissions に宣言されていないスコープ（致命）。
- `unused_permissions`: 宣言されたがどの aiSlot も使わないスコープ（警告）。

`ok` は致命がいずれも無いとき True。`required_capabilities`/`required_permissions` は aiSlots から導出。
許可組合せ・テナント境界等の本格的な合成検証はステージ2 HBD-04。

### 10.4 scaffold 取込（インスタンスへの展開）

`scaffold_sample_app(manifest, created_by=..., available_capabilities=...)` は合成バリデーションを
通したうえで、定義を `sample_app_instances`（definition CLOB ＝ 配布表現のまま）、各 dataset の
seed を `sample_app_seed_rows`（payload CLOB / row_index 順 / instance に ON DELETE CASCADE）へ
展開する。`plugin_id`/`source_version` で出所追跡（installed_plugins と対応。幅は manifest の
`MAX_ID_LEN`/`MAX_VERSION_LEN` と一致）。**合成バリデーションが致命的不足を検出した場合は DB に
何も書かず `CompositionError` を送出**（fail-closed）。migration は `016_sample_app_instances.sql`。

## 11. ヒアリングフロー＆推薦（HBD-01）

スタンダードモード（§5）の中核 = 「顧客ヒアリング → ダイアログ Q&A → 素材（サンプルアプリ＋AI部品＋
コネクタ）の推薦 → SA が確定 → 合成」。本章は `docs/enhance/202607-hearing-flow.md` を昇格したもの
（質問セット・回答→素材の決定的写像・GenAI 補助の境界・データモデル）。実装は
`jetuse_core/hearing_schema.py`（質問スキーマ）/ `jetuse_core/recommend.py`（決定的推薦エンジン）/
`jetuse_core/hearing.py`（永続）/ `service/routes/hearing.py`（API）。migration は `017_hearing.sql`。

### 11.1 質問スキーマ（Q1..Q6 ＋ Auto）

| ID | 型 | 目的 | 選択肢 id | 素材写像 |
|---|---|---|---|---|
| **Q1** | single | 主サンプルアプリ決定 | support / sales / inventory / accounting / other | support→SBA-A・sales→SBA-C・inventory→SBA-B・accounting→SBA-D・other→GenAI 最近傍 |
| **Q2** | multi | AI部品の素地 | docs / business_db / audio / image / saas | docs→{rag.search,summarize,classify}・business_db→{nl2sql}・audio→{minutes}・image→{vlm.ocr}・saas→コネクタ側 |
| **Q3** | single | 主役 AI 強調 | rag_qa / nl2sql / agent / ocr_extract / summarize_draft | 主役 capability を highlight（先頭）に。SBA 組込点へ優先配置 |
| **Q4** | single | コネクタ選定 | slack / other_connector / none | slack→コア・other_connector→後段マーケット・none→無し |
| **Q5** | single | UI/出力テンプレ | chat_form / notify / report | chat / notify / report |
| **Q6** | single | シード戦略 | sample / industry_generated / replace_later | sample / genai_generated / replace_later |
| **Auto** | auto | 合成バリデーション | —（SA 回答なし） | 能力/警告を点検（不足は警告し外させない） |

選択肢 `id` は安定キー（表示文言と分離）。必須 multi（Q2）は require 時に最低 `min_selections`（=1）件。
回答は `validate_answer`/`validate_answers` で検証（未知 id・型不一致・必須欠落・空必須 multi を拒否）。

### 11.2 推薦エンジン（決定的・監査可能）

`recommend(answers)` は**副作用の無い決定的関数**で、3 要素＋UI/シード＋監査トレースを返す:
1. **主 SBA**: Q1 を基点に、分岐「Q2 に business_db ＋ Q3=nl2sql → SBA-B へ格上げ」で補正（§3 分岐例）。
   Q1=other は `sample_app=None`＋`needs_genai_nearest=True`（最近傍は GenAI 補助に委ねるが推薦自体は成立）。
2. **AI 部品**: Q2（データ素地）∪ Q3（主役）。capability 語彙は §10.2 と一致。`highlight`=Q3 の主役。
   並びは `PART_ORDER` で決定的（同じ回答→同じ出力）。
3. **コネクタ**（Q4）＋ **UI**（Q5）＋ **シード戦略**（Q6）。
代表例（§4）: support＋docs＋rag_qa → SBA-A ＋ {rag.search, summarize, classify} ＋ slack ＋ chat ＋ sample。

`validation`（Auto）は要求 capability がホスト既定能力に収まるかを点検し、`vlm.ocr` は MM-01 依存を
警告する（**部品は外さない**＝§3 の原則「不足は警告＋代替提案」）。最終選定は必ず画面で SA に提示する
（ブラックボックス化しない）。

### 11.3 GenAI 補助の境界（§6）

決定（何を選ぶか）はルール＋SA 確認。GenAI は「埋める/書く/寄せる」に限定: ①ヒアリングメモの要点抽出
→各質問のデフォルト提案（`source=genai_suggested` で保存）、②Q1=other 時の最近傍 SBA 提案、
③シードデータ生成方針、④構成サマリの文章化。**GenAI 不在/失敗でも決定ルールだけで推薦が成立**
（フォールバック）。

### 11.4 データモデル（§7）／API

- `hearing_session`: id / owner_sub / status(draft|ready|confirmed|archived) / input_notes(CLOB) /
  created_at / updated_at。
- `hearing_answer`: (session_id, question_id) 一意（upsert）/ value(CLOB JSON) /
  source(sa|genai_suggested)。
- `recommendation`: session_id 一意 / sample_app / ai_parts(JSON) / connectors(JSON) / ui /
  seed_strategy / validation(JSON) / detail(JSON 全文) / confirmed_at。**内容差し替え時は confirmed_at を
  NULL に戻す**（古い確定状態を引き継がない）。

API（`/api/hearing`）: `GET questions` / セッション CRUD / `PUT sessions/{sid}/answers/{qid}`（upsert）/
`POST sessions/{sid}/recommend`（決定的推薦を生成・保存）/ `POST .../recommend/confirm`（SA 確定）。
所有権は SQL（owner_sub）で強制し、他人のセッションは 404。CLOB 列は明示 CLOB バインドで長文に耐える。

### 11.5 非ゴール

ダイアログ UI は HBD-02、合成（実構成生成）は HBD-03、本格的な合成バリデーションは HBD-04。
推薦の「複合（主＋従 SBA）」は MVP では単一 SBA に絞り、`secondary_sample_apps` は将来拡張余地として
空で保持（§8 未決）。

## 12. kind: connector（L2 MCP コネクタ / CON-01）

`kind: connector` は §6 D9 の「SaaS コネクタ」を表す配布種別（plan §10 で `tool`=`connector`＝L2 MCP）。
コネクタは「**DB 認証情報を持たずにテナントデータ／外部 SaaS へ到達する唯一の正規経路**」（plan §4-3）の
L2 を担い、Slack 等の SaaS を JetUse から呼び出すための**正規化された MCP 接続**を宣言する。
`contributes["connector"]` は **接続方法（transport）＋公開操作（actions）＋必要な認証方式（auth）** を
宣言する。実装は `jetuse_core/plugins/connector.py`（定義スキーマ＋合成バリデーション土台）と
`jetuse_core/plugins/connector_store.py`（インスタンスへの登録）。migration は `019_connector_instances.sql`。
**Slack 等の実コネクタ本体は CON-02、合成（sample-app × AI 部品 × connector）への組込＋E2E は CON-03。**

### 12.1 contributes["connector"] スキーマ

| キー | 型 | 必須 | 規則 |
|---|---|---|---|
| `provider` | string | ✓ | 接続先 SaaS の安定キー（`slack`/`teams`/`jira` 等。小文字英数とハイフン/アンダースコア、≤64）。 |
| `transport` | string | ✓ | `mcp`（外部 HTTPS MCP サーバー）\| `builtin`（コア同梱・インプロセス実行）。 |
| `endpoint` | string\|null | – | `transport=mcp` のとき必須（https・公開ホスト literal）。`builtin` のとき禁止。 |
| `auth` | object | ✓ | 認証方式の宣言（§12.2）。**実シークレット値は持たない**。 |
| `actions` | object[] | ✓ | 1..100。`name`（小文字 snake）・`title`・`description`・`permissions`（Platform スコープ部分集合）。 |
| `summary` | string | – | 表示用説明（≤2000）。 |

cross-field 規則: `transport=mcp` は `endpoint` 必須・`builtin` は `endpoint` 禁止。`action.name` は一意。
`endpoint` 検証は**オフライン・決定的**（DNS 解決しない）で、https スキーム・ホスト名あり・明白な
private/loopback/link-local の IP literal 拒否までを行う（完全な SSRF ガード＝DNS 解決を伴う公開判定は
invoke 時＝CON-03）。検証済み定義は正準 JSON 化可能（manifest 署名往復の前提）。

### 12.2 auth（認証方式 / 実値を持たない）

| サブキー | 規則 |
|---|---|
| `kind` | `none` \| `api_token` \| `oauth2`。 |
| `scopes` | 外部 SaaS 側のスコープ（例 Slack `chat:write`）。**Platform スコープではない**自由文字列。`oauth2` のときのみ非空可（重複不可）。 |
| `secretRef` | ホストが install 時に Vault へ束ねる秘密の**論理参照名**（小文字英数とハイフン/アンダースコア、≤64）。`kind!=none` のとき必須・`none` のとき禁止。 |

**認証実値の非保持と `secretRef` の機密区分（設計判断）**:

- 非保持の対象は**トークン/パスワード等の実シークレット値**である。manifest・`connector_instances`・
  証跡のいずれにも**実シークレット値は保存しない**。実シークレットは install 時に Vault（OCID 参照）へ
  束ねる（CON-02/03）。`019_connector_instances.sql` は秘密値の列を持たない。
- `secretRef` は**実シークレットではなく、宣言の一部である論理参照名**（例 `slack-bot-token`）。
  「このコネクタは名前 X の秘密を要求する」という宣言であり、それ自体は資格情報ではない（既存の
  `mcp_servers.auth_secret_ocid` が OCID 参照を保持するのと同じ区分）。
- **`secretRef` は登録定義に保持してよい**（むしろ保持すべき）。`connector_instances.definition` は
  発行された manifest の `contributes["connector"]` ペイロードを**そのまま往復保存**する（署名往復の前提・
  install 時にどの Vault 秘密を束ねるべきか復元するため）。`secretRef` を除去すると定義が manifest と
  一致しなくなり round-trip 契約が壊れる。機密区分は「テナント内部の論理名（非機密）」とし、テナント境界は
  登録者（`registered_by`）と plugin 出所（`plugin_id`/`source_version`）で追跡する。
- 整理: **保存する** = 配布定義（`secretRef` 名を含む）。**保存しない** = 実シークレット値・OCID 実値・
  認証トークン。CLAUDE.md「認証実値をコミットしない」とも整合する。

### 12.3 合成バリデーション土台（CON-03 / HBD-04 の前段）

`validate_connector_composition(manifest)` が以下を判定する:
- `undeclared_permissions`: action が要求するが manifest.permissions に宣言されていないスコープ（致命）。
- `unused_permissions`: 宣言されたがどの action も使わないスコープ（警告）。
- `requires_secret`/`secret_ref`: 認証が必要か（`kind!=none`）＋束ねるべき参照名。

`ok` は `undeclared_permissions` が空のとき True。`required_permissions` は actions から導出。
許可組合せ・テナント境界等の本格的な合成検証は CON-03／ステージ2 HBD-04。

### 12.4 登録（インスタンスへの取込）

`register_connector(manifest, *, registered_by, name=None)` は合成バリデーションを通したうえで、
定義（provider/transport/actions/auth、配布表現のまま）を `connector_instances`（definition CLOB）へ
登録する。`plugin_id`/`source_version` で出所追跡（installed_plugins と対応。幅は manifest の
`MAX_ID_LEN`/`MAX_VERSION_LEN` と一致）。**合成バリデーションが致命的不整合を検出した場合は DB に
何も書かず `ConnectorCompositionError` を送出**（fail-closed）。`get_connector`/`list_connectors`
（plugin_id/provider 絞り込み）/`remove_connector` を提供する。定義 CLOB は配布表現のまま往復保存し、
**実シークレット値は含まない**（含むのは `secretRef` 論理参照名のみ。§12.2 の機密区分）。実シークレットの
Vault 束ねは本タスクの非ゴール（CON-02/03）。

### 12.5 公開 API（`jetuse_core/plugins/connector.py`）

| シンボル | 役割 |
|---|---|
| `ConnectorDefinition` | `contributes["connector"]` の pydantic ルートモデル。 |
| `validate_connector(source) -> ConnectorDefinition` | manifest か dict を検証。不正なら `ConnectorError`。 |
| `validate_connector_composition(manifest) -> ConnectorCompositionReport` | 合成バリデーション土台。 |
| `connector_json_schema() -> dict` | 定義の JSON Schema（camelCase）。 |
| `CONNECTOR_TRANSPORTS` / `CONNECTOR_AUTH_KINDS` | 仕様定数。 |

### 12.6 コネクタ実行（invoke）／ Slack コア（CON-02）

CON-01 が確定した配布表現（定義・合成・登録）の上に、**登録済みコネクタの action を実際に呼び出す
実行経路**（`jetuse_core/plugins/connector_runtime.py`）と、その最初の実体である**コア同梱 Slack
コネクタ**（`jetuse_core/plugins/slack_connector_builtin.py`）を実装する。コネクタは「DB 認証情報を
持たずに外部 SaaS／テナントデータへ到達する唯一の正規経路」（plan §4-3）の L2 であり、invoke は
必ず **Platform API ブローカー**（§ ADR-0014 / `platform_broker`）の認可を通す。

**`invoke_connector_action(definition, action, payload, *, broker_token, tenant, resource="",
secret_resolver=None, http_caller=None, mcp_caller=None)`** の手順（順序が安全契約）:

1. **action 解決**: 未知 action は `ConnectorInvokeError`（外部に触れる前）。
2. **ブローカー認可（fail-closed）**: 必須 `platform:connector.invoke` ＋ action が宣言する Platform
   スコープ（`action.permissions`）を `platform_broker.authorize` で順に強制し、許可/拒否を
   `platform_broker_audit` に記録する。未付与スコープ・テナント越境・期限切れ・改竄・鍵未設定は
   `ConnectorInvokeDenied`（broker の DENY を引き継ぐ）に倒す。**認可は外部呼び出しより前**に行い、
   拒否時は Slack/MCP へ一切到達しない（外部副作用ゼロ）。
3. **秘密解決**: `auth.kind!=none` のとき `secret_resolver(secretRef)` で実トークンを取得する
   （未設定なら fail-closed）。解決したトークンは **戻り値・例外・監査・ログのいずれにも出さない**
   （Authorization ヘッダ／MCP ヘッダにのみ載せる）。実 Vault 束ね・install フローは CON-03。
4. **transport 別ディスパッチ**:
   - `builtin`: `(provider, action)` で引く**インプロセスハンドラ**（`register_builtin_action`）。
     実 HTTP は差し替え可能な `http_caller` 経由（既定は実ネットワーク禁止の fail-closed。テスト/E2E は
     mock を注入）。
   - `mcp`: Responses API **`type:"mcp"`** ツール仕様（`server_label=provider`／`server_url=endpoint`／
     `Bearer=解決トークン`／`require_approval=never`）を組み立てて `responses.create` を呼ぶ配管
     （`mcp_caller` 差し替え可能。単体は mock、実 MCP 接続は CON-03）。

戻り値 `ConnectorInvokeResult`（provider/action/transport/ok/output/jti）に**実シークレットを含めない**。
新規 migration は作らず、invoke の認可監査は既存 `platform_broker_audit`（020）を再利用する。

**コア Slack コネクタ**: `provider=slack` / `transport=builtin` / `auth=oauth2`・
`secretRef="slack-bot-token"`（参照名のみ）。actions = `post_message`（`chat.postMessage`）/
`list_channels`（`conversations.list`）。いずれも SaaS ブリッジで Platform データに触れないため
`permissions` は空（呼ぶ権利 `platform:connector.invoke` は invoke 層が常に強制）。`slack_connector_manifest()`
は `kind: connector` として `validate_manifest`／合成バリデーション（ok・requires_secret）を満たす。
**実 Slack 認証は本タスクでは投入しない**（実 OAuth トークン未投入のため `http_caller` を mock して投稿
フローを検証。実 SaaS／実 MCP 接続は CON-03）。

| シンボル（CON-02） | 役割 |
|---|---|
| `invoke_connector_action(...) -> ConnectorInvokeResult` | コネクタ action 実行（認可→秘密解決→ディスパッチ）。 |
| `register_builtin_action(provider, action)` | builtin ハンドラ登録デコレータ。 |
| `ConnectorInvokeError` / `ConnectorInvokeDenied` | 構成不備／認可拒否（fail-closed）。 |
| `slack_connector_definition()` / `slack_connector_manifest()` | コア Slack コネクタ定義／manifest。 |

### 12.7 合成への組込／connector 束縛・invoke 経路（CON-03）

CON-01/02 が確定したコネクタ（配布表現・invoke 層・Slack コア）を、**合成エンジン（HBD-03 / `synth.py`）**
と**デプロイ前ガバナンス（HBD-04 / `governance.py`）**へ正式に組込む。これまで synth は推薦の
`connectors` を文字列のまま転記していたが、本節で AI 部品と同じく**束縛**する（plan §4-3「組み立て結果
（sample-app × AI部品 × connector）を、許可された組合せ・必要ケイパビリティ・権限スコープでチェックして
からデプロイ」の connector 軸）。

**コアコネクタ・レジストリ**（`jetuse_core/plugins/core_connectors.py`）: provider → 検証済み定義/manifest の
単一の引き当て。コアパレットは Slack 1本（§6 D9）。`core_connector(provider)` /
`core_connector_providers()`（governance の許可パレットの正本＝二重定義しない）/
`connector_invoke_scopes(definition)`（= `platform:connector.invoke` ＋ action 宣言スコープの和集合・順序固定）/
`resolve_active_connector(composition, provider)`（**active** な定義を返し、無ければ None＝fail-closed）。
**実シークレットを持たない**（secretRef = 参照名のみ）。

**コネクタ束縛（synth）**: `synthesize()` が推薦 `connectors` を `ConnectorBinding`（provider/status/transport/
actions/required_scopes/requires_secret/secret_ref/reason）へ束縛する。`status`:
- `active`: コアパレットに在り、かつコネクタ合成バリデーション（`validate_connector_composition`・宣言整合）が ok。
  `required_scopes` の短期トークンを broker から得れば invoke 経路に載せられる。
- `excluded`: パレット外（後段マーケット）または合成不整合。**黙って消さず** `reason` と `warnings` に残す（§4）。

`DemoComposition` に `connector_bindings` と `active_connectors` を追加する（**既存 `connectors: list[str]` は
後方互換で維持**＝summary.py 等の参照を壊さない）。副作用なし・決定的。

**connector_scope ゲート（governance）**: コアパレット判定を `core_connector_providers()` から導出し、
新たに `connector_scope` チェックを設ける。active コネクタの `required_scopes` が既知 Platform 語彙
（`PLATFORM_SCOPES`）の部分集合で、かつ `platform:connector.invoke` を含むこと、コアコネクタなのに合成不整合で
excluded になっていないことを検証する（違反は `connector_scope_unknown` / `connector_scope_undeclared`、各々
**代替提案つき**）。invoke 経路が成立する構成だけをデプロイ可とする。

**実呼び出しは Platform API ブローカー経由**: 合成された active コネクタは CON-02 の
`invoke_connector_action`（`platform:connector.invoke` ＋ action スコープを broker 強制・`platform_broker_audit`
記録・fail-closed）で呼ぶ。新しい invoke 経路は作らない（再利用）。**実 Slack 認証・実 Vault 束ね・実 SaaS／
実 MCP 接続は本タスクでも投入しない**（mock で検証。実接続は後段）。

| シンボル（CON-03） | 役割 |
|---|---|
| `core_connector(provider)` / `core_connector_providers()` | コアコネクタ引き当て／パレット（正本）。 |
| `connector_invoke_scopes(definition)` | コネクタ invoke に要る Platform スコープ（順序固定）。 |
| `resolve_active_connector(composition, provider)` | 合成構成の active コネクタ定義（無ければ None）。 |
| `synth.ConnectorBinding` / `DemoComposition.connector_bindings` / `.active_connectors` | コネクタ束縛結果。 |
| `governance` `connector_scope` チェック | active コネクタの invoke スコープ経路を検証（デプロイ前ゲート）。 |

## 13. Platform API ブローカー（plan §7 昇格 / PAPI-01・02）

> plan §7 を本仕様へ昇格したもの。認可モデルの正本は `docs/decisions/ADR-0014`（採用済）。
> 実装は `jetuse_core/platform_broker.py`（認可コア＝発行/検証/スコープ強制/テナント境界/監査。PAPI-01）と
> `jetuse_core/platform_grants.py`（スコープ承認＋発行フロー。PAPI-02）。migration は
> `020_platform_broker_audit.sql`（監査）/`021_platform_scope_grants.sql`（承認）。
> 実 Platform API ルート本体（rag.search/db.query 等）は **PAPI-03**。

L2 コネクタ・L3 ホスト型アプリ・生成デモが、**DB 認証情報を持たずに**テナントデータへ到達する
**唯一の正規経路**。プラグインはブローカーが発行する**スコープ付き短期トークン**を提示し、ブローカーが
スコープ・テナント境界・監査を一元的に強制する（plan §12「データはインスタンス所有・アクセスは仲介経由」）。

### 13.1 スコープ語彙（§4 と同一集合）

ブローカーが扱うスコープは manifest 検証の `PLATFORM_SCOPES`（§4）と**同一集合を正本**とする
（manifest の `permissions` と発行トークンの `scope` が必ず突き合う）。付与スコープは常に
`PLATFORM_SCOPES` の部分集合でなければならない（未知スコープは発行・検証で拒否）。

### 13.2 短期トークン（ADR-0014 §2）

呼び出しごとに JetUse（ブローカー）が短期 JWT を発行する。発行＝検証が JetUse 内で閉じるため
**対称鍵 HS256**。鍵 `platform_broker_secret` は .env / Vault 注入で**コミットしない**。claims は
`iss`=`jetuse-platform-broker` / `aud`=`jetuse-platform-api` / `sub`=プラグイン ID /
`tenant`=テナント境界（Project OCID）/ `scope`（付与スコープ・スペース区切り）/ `jti`（監査・失効の継ぎ目）/
`iat`/`nbf`/`exp`（TTL 既定 300 秒・上限 900 秒）。**DB 認証情報はトークンに載せない**。

### 13.3 スコープ承認（PAPI-02 / `approve_scopes`）

スコープは manifest `permissions` 由来で、**インストール／合成時に人間=SA が承認**した範囲だけを載せる。
承認は (tenant=Project OCID, plugin_id) ごとに `platform_scope_grants` へ永続化する（upsert＝再承認で更新、
失効＝`revoke_grant` で status=REVOKED）。承認可能なのは **manifest.permissions ∩ PLATFORM_SCOPES** のみで、
プラグインが要求していないスコープ・未知スコープ・空は拒否する（fail-closed＝最小権限。manifest が正本）。
グラント行・トークンに**署名鍵・DB 認証情報・実シークレット値を保存しない**。

### 13.4 発行フロー＋粒度の確定（PAPI-02 / `issue_token`）

`issue_token(tenant, plugin_id, scopes=None)` は承認済みグラントを読み、**承認スコープに厳密に閉じた**
短期トークンを認可コア（`issue_broker_token`）経由で発行する。グラント無し（`no_grant`）・失効
（`grant_revoked`）・承認超過要求（`scope_not_granted`）は**トークンを発行せず**拒否する（fail-closed）。
manifest が宣言していても**未承認スコープはトークンに載らない**。

**発行粒度の確定**（ADR-0014 §2 が PAPI-02 へ委任した決定）: **呼び出しごと**に発行する
（セッション単位で使い回さない）。TTL 内の単回使用強制（`jti` 消費）を持たない MVP では、粒度を
細かくするほどリプレイ露出窓が小さくなるため、最短粒度＝呼び出しごとを採る。リプレイリスト／単回 `jti`
消費の本格導入は PAPI-03 で再判断する（ADR-0014 §2・§5）。

### 13.5 テナント境界・監査・fail-closed（ADR-0014 §3〜5）

実 API ルート（PAPI-03）は各エンドポイントの冒頭で `authorize(token, required_scope, tenant=...)` を
呼び、**トークンの `tenant` と要求リソースのテナントの一致**を必須にする（不一致は `tenant_mismatch`）。
全アクセス（ALLOW/DENY）を `platform_broker_audit` にベストエフォートで記録し、越境試行（DENY）が必ず
監査に残るようにする。署名不正・期限切れ・`nbf` 未到来・`iss`/`aud` 不一致・未知スコープ・`tenant` 欠落・
鍵未設定など**あらゆる失敗を「不可」に倒す**（fail-closed）。L3 コンテナは検証鍵を持たず短期トークンのみ
提示し、検証は常にブローカー側で行う。レート制限は PAPI-03（ブローカーが一元的に絞れる位置を要件として固定）。

### 13.6 公開 API（`platform_broker.py` / `platform_grants.py`）

| シンボル | 役割 |
|---|---|
| `issue_broker_token(plugin_id, tenant, scopes)` | 認可コア: 署名付き短期 JWT を発行（PAPI-01）。 |
| `verify_broker_token(token) -> BrokerContext` | fail-closed 検証（PAPI-01）。 |
| `authorize(token, required_scope, *, tenant)` | 検証＋スコープ強制＋テナント一致＋監査（PAPI-01。PAPI-03 が各ルートで使う）。 |
| `approve_scopes(manifest, *, tenant, scopes, approved_by)` | スコープ承認を永続化（PAPI-02）。 |
| `issue_token(tenant, plugin_id, *, scopes=None)` | 承認に閉じた発行フロー（PAPI-02）。 |
| `get_grant` / `list_grants` / `revoke_grant` | 承認グラントの参照・失効（PAPI-02）。 |
| `validate_grant_scopes` / `select_issuable_scopes` | 承認・発行スコープ選択の純粋ポリシー（DB 非依存・PAPI-02）。 |

### 13.7 実 Platform API ルート（`service/routes/platform.py` / PAPI-03）

L2/L3/生成デモは、`Authorization: Bearer <broker-jwt>` に **broker が発行した短期トークン** を載せて
`/platform/*` を呼ぶ（OIDC のユーザトークンとは**別系統**。検証鍵は常にブローカー側）。各ルートは
冒頭で `authorize(token, required_scope, tenant=<要求リソースのテナント>)` を呼び、**JWT 検証 →
scope 強制 → テナント一致 → 監査（ALLOW/DENY）** を通した範囲でだけ既存エンジンへ委譲する。

| path / method | 要求 scope | 委譲先 / 配管 | 備考 |
|---|---|---|---|
| `POST /platform/db/query` | `platform:db.query` | `nl2sql.execute_readonly`（**読取限定**） | 非 SELECT は `SqlRejectedError`→**400**（書込は到達しない）。 |
| `POST /platform/connector/invoke` | `platform:connector.invoke` | 配管まで（CON-02/03） | 認可＋コネクタ/action 存在検証まで→実 MCP は **501**。未登録は 404。 |
| `POST /platform/rag/search` | `platform:rag.search` | 配管まで（後続） | 認可まで→本格ベクトル検索（OCI Responses）委譲は **501**。 |

**拒否マッピング（fail-closed）**: `scope_denied` / `tenant_mismatch` → **403**、改竄・期限切れ・未署名・
必須 claim 欠落（`invalid_token` 他）→ **401**、トークン欠如 → **401**、ブローカー鍵未設定
（`BrokerConfigError`）→ **503**。テナント越境は scope 不足より先に `tenant_mismatch` として監査に残す
（§13.5 の契約）。`conversations.read` / `files.*` のルート、rag.search の本格検索、connector.invoke の
実 MCP 呼び出し、レート制限、OIDC 発行主体認証（INFRA-02）は後続。

## 14. kind: external-app（外部アプリ連携 / SSO ブリッジ / ASSET-01）

`kind: external-app` は **外部アプリの UI そのものを JetUse へ埋め込み（iframe / link）＋ OIDC SSO で
シングルサインオン**するオンボード種別（§6 D9 の「外部連携」）。コネクタ（§12 / L2 MCP）が「外部 SaaS の
**API** を JetUse から呼び出す」のに対し、external-app は「外部アプリの **画面**を JetUse 内に出し、利用者の
身元を SSO で引き渡す」。独自フロントを持つ既存資産（例: 伝ぴょん）の最短オンボード経路。実装は
`jetuse_core/plugins/external_app.py`（定義スキーマ＋OIDC SSO ブリッジ最小実装）と、資産別 builder
（`denpyon_external_app.py`）。**実コネクタ化（MCP 化）が過剰な、UI 完結型の既存資産に用いる**
（方式選択は docs/verification/ASSET-01.md）。

### 14.1 contributes["external-app"] スキーマ

| キー | 型 | 必須 | 規則 |
|---|---|---|---|
| `app` | string | ✓ | 連携先アプリの安定キー（`denpyon` 等。小文字英数とハイフン/アンダースコア、≤64）。 |
| `embed` | string | ✓ | `iframe`（画面内枠埋め込み）\| `link`（別タブ導線）。 |
| `url` | string | ✓ | 外部アプリの HTTPS エンドポイント。**オフライン・決定的検証**（https・公開ホスト・private/loopback 拒否・userinfo/query/fragment 禁止＝認証値埋め込み防止）。url は**利用者ブラウザが iframe/link の src として読む**（サーバ側 fetch しない）ため DNS 解決を伴う SSRF ガードは持たず、内部 FQDN 抑止は配備時のネットワーク境界・iframe CSP/sandbox・IdP の redirect_uri 許可リストで担保する。 |
| `title` | string | ✓ | 表示名（≤200）。 |
| `sso` | object\|null | – | OIDC SSO ブリッジ宣言（§14.2）。None なら埋め込みのみ（SSO なし）。 |
| `summary` | string | – | 表示用説明（≤2000）。 |

`claimMapping` は写像元（subject クレーム名）に資格情報系の名前（token/secret/jwt/sid 等）を使えず、
宛先クレーム名の重複も禁止（後勝ちで身元属性を黙って上書きしない）。SSO 宣言時は最低1つの写像が必須。
external-app は UI 埋め込み＋SSO であり **Platform API スコープを要求しない**（builder は manifest の
top-level `permissions` を空にする。external-app は broker invoke 経路を持たないため、仮に宣言されても
inert）。

### 14.2 sso（OIDC SSO ブリッジ / 実値を持たない）

| サブキー | 規則 |
|---|---|
| `mode` | `oidc`（現状 OIDC のみ。SAML 等は後段）。 |
| `issuer` | OIDC IdP の HTTPS URL（公開ホスト・オフライン検証）。token/authorize の基底。 |
| `clientIdRef` | OIDC client_id の **論理参照名**（install 時に解決。値ではない）。 |
| `secretRef` | client_secret の **論理参照名**（Vault 束ね対象。値ではない）。 |
| `audience` | token-exchange の audience（連携先アプリの HTTPS URL）。 |
| `scopes` | OIDC スコープ。**openid を含む**・重複不可（≤50）。 |
| `claimMapping` | JetUse subject クレーム名 → 連携先アプリのクレーム名 への写像（SSO で渡す身元属性。≤50）。 |

**実シークレットの非保持**: コネクタ（§12.2）と同じ契約。manifest・配布定義・証跡のいずれにも
**実 client_secret 値・実トークンは保存しない**。保持するのは論理参照名（`clientIdRef`/`secretRef`）のみ。
実値は install 時に Vault(OCID) へ束ねる（人間ゲート）。url/issuer/audience は認証値を埋め込めない
公開 HTTPS URL に限定する（userinfo/query/fragment 禁止）。

### 14.3 SSO ブリッジ最小実装（`build_sso_handoff`）

`build_sso_handoff(definition, subject, *, state, nonce, subject_token_ref=...)` は **決定的・オフライン**
（IdP へ実通信しない）で OIDC ハンドオフ要求を組み立てる:
- `sso` 未宣言のアプリは **fail-closed**（`SsoHandoffError`）。`state`/`nonce` 欠落も fail-closed。
- `claimMapping` を `subject`（認証済み利用者のクレーム）へ適用し受け渡しクレームを作る。写像元クレームの
  欠落・空は **fail-closed**（SSO は正しい身元の確実な引き渡しが要）。
- **RFC 8693 token-exchange 要求の shape**（grant_type / audience / scope）を返す。token endpoint は IdP
  ごとに異なり issuer から機械的に導出できないため**固定パスを生成せず**、OIDC discovery URL
  （issuer + `/.well-known/openid-configuration`）を返す（`sso.tokenEndpoint` 明示時はそれを使う）。
  client_id / client_secret / subject_token は **参照名**のみ（実値・実トークンを含めない）。
- 返り値・例外の **JetUse 構築部分**（token_exchange_request の client_id/client_secret/subject_token は
  参照名のみ）に**実シークレット値・実トークンを注入しない**（`contains_secret_values=False` の意味）。
  `mapped_claims` は**呼び出し側が与える subject の身元属性**であり、秘密を入れない責務は呼び出し側。
  多層防御として、claimMapping の写像元に資格情報系クレーム名（token/secret/jwt/sid 等）を使うことは
  定義検証で禁止する（実トークンの転写経路を塞ぐ）。

実 IdP 接続・実 client_secret 投入・実 id_token 発行・実 token-exchange の実行は **人間ゲート**（SSO 実設定）。
合成（sample-app × connector × external-app）への組込・実埋め込みレンダリングは後段。

### 14.4 オンボード経路とマーケット流通（ASSET-01 → BE-06 / ADR-0021）

ASSET-01 では external-app の**オンボードは builder（`denpyon_external_app_manifest` 等）による配布表現の
生成と、in-process の決定的 SSO ブリッジ（`build_sso_handoff`）**で完結し、DB の instance store も
marketplace install も持たない「後段」扱いとしていた。**BE-06（ADR-0021）でこの後段を実装した**:

- **store＋migration**: `external_app_instances`（migration 026。BE-04 が 025 を使用するため繰り下げ）＋ `external_app_store`
  （register/get/list/remove/delete_by_source）。**実シークレット値は保存せず**参照名（clientIdRef/
  secretRef）のみ（§14.2・§12.2 の機密区分）。
- **install 対応**: `marketplace.SUPPORTED_KINDS` に `external-app` を追加。`installer._ingest_contributes`
  に external-app 分岐を足し、署名検証・版固定・出所追跡・補償削除の **kind 非依存の枠組みをそのまま流用**
  する（迂回構築 manifest は `IngestError` へ正規化）。
- **実 token-exchange 配線＋起動ルート**: `external_app.exchange_sso_token`（実 RFC 8693 token-exchange。
  継ぎ目は `secret_resolver`/`subject_token`/`token_exchange_caller`。既定 fail-closed・本番 caller
  `http_token_exchange_caller` 提供）と routes `/api/external-apps`（一覧 / `sso-launch`=shape /
  `sso-exchange`=実 exchange）。install 済み instance も起動ルートが surface する。

**実 IdP 接続・実 client_secret 投入・実 MCP 配備・Vault 束ね・Identity Domain 設定は人間ゲート**（実接続）。
`sso-exchange` は tokenEndpoint・Vault secret・利用者の実 id_token（Bearer）が揃わなければ fail-closed
（503/401）＝未構成では実 IdP に到達しない。実接続の実設計と未解決事項は ADR-0021 を参照。

### 14.5 公開 API（`jetuse_core/plugins/external_app.py`）

| シンボル | 役割 |
|---|---|
| `ExternalAppDefinition` | `contributes["external-app"]` の pydantic ルートモデル。 |
| `OidcSsoBridge` | OIDC SSO ブリッジのサブモデル。 |
| `validate_external_app(source) -> ExternalAppDefinition` | manifest か dict を検証。不正なら `ExternalAppError`。 |
| `build_sso_handoff(definition, subject, *, state, nonce) -> dict` | SSO ハンドオフ組み立て（fail-closed）。 |
| `external_app_json_schema() -> dict` | 定義の JSON Schema（camelCase）。 |
| `EMBED_MODES` / `SSO_MODES` | 仕様定数。 |
