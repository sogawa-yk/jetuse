# specs/16: デモ生成プラットフォーム — プラグイン manifest 仕様（PLG-01）

> 出典: `docs/enhance/202607-demo-platform-plan.md` §4・§6・§10／`docs/comparison/marketplace-plugin.md`。
> 設計判断は `docs/decisions/ADR-0013`。本仕様は **L1 宣言型サブセット**（`kind: usecase | agent`）を
> 確定する。tool(L2 MCP)・sample-app・hosted-app(L3)・bundle は後続タスクで拡張する。
> 実装は `jetuse_core/plugins/manifest.py`（pydantic モデル＋JSON Schema＋検証）。

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
| `kind` | string | ✓ | L1 サブセット = `usecase` \| `agent`。 |
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

`kind` に一致しないキー、または複数キーは検証エラー。内部構造の詳細スキーマは各エンジン
（usecases / agents）側に委ね、本タスクでは「kind とキーの対応」までを強制する。

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
- `kind` の L2/L3 拡張（tool/hosted-app/bundle）。
- permissions の承認フロー・短期 JWT 発行（§7 Platform API、後続ステージ）。

> 注: PLG-01 当初の非ゴールだった `kind: sample-app` は SBA-01 で追加した（§10）。

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
