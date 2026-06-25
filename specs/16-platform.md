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

- レジストリ通信・UI・データモデル・インストール処理（PLG-02..08）。
- `kind` の L2/L3 拡張（tool/sample-app/hosted-app/bundle）。
- permissions の承認フロー・短期 JWT 発行（§7 Platform API、後続ステージ）。
