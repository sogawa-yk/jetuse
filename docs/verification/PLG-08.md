# PLG-08 検証レポート — MVP E2E 実機検証（インスタンス間共有）

- タスク: PLG-08（`tasks/PLG-08.md`）
- ゴール: 「公開物を別インスタンスから参照・インストール・実行できる」を**実環境**で証明する（実機検証主義）。
- 実施日: 2026-06-26（UTC）
- run-id / 証跡: `runs/2026-06-26T0252_PLG-08/e2e/`
- 結論: **PASS**（受け入れ条件4項目すべて充足。happy path 2シナリオ + 信頼境界の負シナリオ1本を実機実行）。

## 1. 何を「実機」で使ったか

本検証はモック・インメモリ代替を使わず、以下の**実 OCI リソース**で実行した。

> 注: 本レポートと証跡では、CLAUDE.md「認証情報・テナンシ/コンパートメント OCID・エンドポイント実値をリポジトリにコミットしない」に従い、Object Storage namespace は `<os-namespace>`、推論エンドポイント実値は `${GENAI_BASE_URL}`（`.env` 管理。雛形は `.env.example`）としてマスクしている。実値は `.env`／実行時環境にのみ存在し、コミットしていない。

| 役割 | 実体 | 備考 |
|---|---|---|
| 中央レジストリ（保存層） | **実バケット `jetuse-registry`**（namespace `<os-namespace>` / ap-osaka-1） | 既存の空バケットを再利用。新規課金リソースの apply は行っていない（人間ゲート遵守）。`OciObjectStore`（`jetuse_registry.storage.build_from_env`）で読み書き。 |
| 中央レジストリ（サービス） | `jetuse_registry.create_app`（PLG-04 本番コード）を localhost で起動し**実バケットへ永続化** | 発行者認証 = ed25519 署名 + Bearer。コードは本番と同一。 |
| アプリ DB | **実 ADB `jetuseloop`**（jetuse-dev / 固定 loop 環境、`infra/terraform/environments/loop/`） | むやみに増やさず再利用。admin パスワード/ウォレットは都度リセット再生成（[[loop-e2e-adb-jetuse-dev]] 方針）。 |
| 推論（実行/SSE） | **実 OCI Generative AI**（`${GENAI_BASE_URL}`、`meta.llama-3.3-70b-instruct`） | IAM ユーザー署名。`/api/chat/stream` が `text/event-stream` を実際にストリームした。 |

## 2. インスタンスA/Bの分離方法（証跡に明記）

同一 compute インスタンス上で、A（発行側）とB（購読側）を**3層で物理分離**して模擬した。

1. **別 ADB スキーマ**: A=`JETUSE_PLG08_A` / B=`JETUSE_PLG08_B`（loop ADB 内に専用ユーザを作成し、各々へ全マイグレーション適用）。`loop-config.yml` の `db_schema_isolation: JETUSE_<task>` を A/B 用に展開したもの。
2. **別 OS プロセス・別作業ディレクトリ**: A は `workdir-A`、B は `workdir-B` で独立 Python プロセスとして起動（`runs/.../e2e/scripts/run_all.sh`）。
3. **共有経路は中央レジストリ（バケット）のみ**: B のインストール経路（`installer.install`）は**レジストリからしか読まない**。B は A のスキーマに一切接続しない。

この分離の実証は `db-state.txt`（後述 §4）で確認できる。A のスキーマには installed_plugins が 0 件・ローカル authored の UC のみ。B のスキーマには registry 由来（`source_plugin_id` 付き）の ingested UC と署名検証済み（`sig_verified=1`）の installed_plugins。両者は別スキーマであり、A の UC 行 id は B から参照不能（§5 (d)）。

> 実バケットは `NoPublicAccess` のため、購読側の読取だけは素の HTTP ではなく **OCI 署名付き transport** を `CentralRegistryClient` に注入して行った（`project_b.py` `_oci_client`）。本番は Object Storage 公開 URL を `plugin_registry_url` に設定する想定で、**署名検証・取込ロジックは無改変**。これは私設バケット再利用に伴う唯一の適応点。

## 3. 受け入れ条件の充足

| 受け入れ条件 | 結果 | 証跡 |
|---|---|---|
| A（Project A）で宣言型UCを公開→中央レジストリに掲載される | ✅ | `project-a/publish.log`（`POST /api/usecases/{id}/publish` → HTTP 200、registry へ 201 Created）、`bucket-objects.txt`（実バケットに `index.json` + `plugins/plg08-e2e/.../*.json`）、`registry-plugins.json` |
| B（Project B）でマーケットからインストールできる | ✅ | `project-b/run.log`（`GET /api/marketplace/plugins` に A の公開物2件、`POST /api/marketplace/install` → HTTP 200、署名検証済み ingest）、`db-state.txt`（B スキーマに ingested 行） |
| B で当該UCが実行でき **SSE 出力**まで動く | ✅ | `project-b/result.json` / `run.log`（`POST /api/chat/stream` → HTTP 200 `text/event-stream`、`delta` イベント列、`[DONE]`、実モデル出力テキスト） |
| 上記の実行ログを `docs/verification/PLG-08.md` に添付 | ✅ | 本ファイル + `runs/2026-06-26T0252_PLG-08/e2e/` 一式 |

## 4. シナリオ1・2（happy path × 2 宣言型UC）

A が2件の宣言型UC（`{{var}}` テンプレート + fields）を作成・公開し、B が両方をインストール・実行した。

**公開（A、実 API ルート → 実 registry → 実バケット）** — `project-a/publish.log`:
```
[A] POST /api/usecases/.../publish v1.0.<stamp> -> HTTP 200
[A]   published plugin_id=plg08-e2e/plg08-translator  version=1.0.<stamp> publisher=plg08-e2e
[A] POST /api/usecases/.../publish v1.0.<stamp+1> -> HTTP 200
[A]   published plugin_id=plg08-e2e/plg08-summarizer version=1.0.<stamp+1> publisher=plg08-e2e
```

**中央レジストリ（実バケット）の状態** — `bucket-objects.txt`:
```
index.json
plugins/plg08-e2e/plg08-summarizer/1.0.<stamp+1>/<sha256>.json
plugins/plg08-e2e/plg08-translator/1.0.<stamp>/<sha256>.json
```

**マーケット表示 → インストール（B、実 API ルート）** — `project-b/run.log`:
```
[B] GET /api/marketplace/plugins -> HTTP 200
[B]   marketplace shows 2 card(s); 2 from publisher plg08-e2e:
[B]     - plg08-e2e/plg08-translator  installed=False installable=True
[B]     - plg08-e2e/plg08-summarizer  installed=False installable=True
[B] POST /api/marketplace/install plg08-e2e/plg08-summarizer -> HTTP 200  (ingested usecases)
[B] POST /api/marketplace/install plg08-e2e/plg08-translator -> HTTP 200  (ingested usecases)
```

**実行 + SSE（B、`/api/chat/stream` → 実 GenAI）** — `project-b/result.json`:

| シナリオ | source plugin | HTTP / content-type | SSE frames / delta / [DONE] | モデル出力（実 GenAI） |
|---|---|---|---|---|
| 1. Translator | `plg08-e2e/plg08-translator@1.0.<stamp>` | 200 / `text/event-stream; charset=utf-8` | 9 / 6 / ✅ | `Thank you for coming today.` |
| 2. Summarizer | `plg08-e2e/plg08-summarizer@1.0.<stamp+1>` | 200 / `text/event-stream; charset=utf-8` | 数十 / 数十 / ✅ | （実 GenAI による日本語要約。例: 「OCI版のJetUseは…インスタンス間で共有でき、購読側は署名検証のうえで取り込む」。生出力は `result.json` 参照） |

SSE フレーム先頭サンプル（`result.json` `first_frames`）:
```
data: {"ka": 1}
data: {"delta": "Thank"}
data: {"delta": " you"}
```

**DB 永続化 + A/B 分離の実証** — `db-state.txt`:
```
==== schema JETUSE_PLG08_A ====               # 発行側
usecases (2):  PLG08 Summarizer / PLG08 Translator  (locally authored, not from registry)
installed_plugins (0):

==== schema JETUSE_PLG08_B ====               # 購読側
usecases (2):
  - PLG08 Summarizer  <- registry plg08-e2e/plg08-summarizer@1.0.<stamp+1>
  - PLG08 Translator  <- registry plg08-e2e/plg08-translator@1.0.<stamp>
installed_plugins (2):
  - plg08-e2e/plg08-summarizer@... sig_verified=1 by=dev-user from=oci://<os-namespace>/jetuse-registry/
  - plg08-e2e/plg08-translator@... sig_verified=1 by=dev-user from=oci://<os-namespace>/jetuse-registry/
```

## 5. シナリオ3（配布の信頼境界 / fail-closed の実機検証）

happy path だけでなく「壊れた配布を B が取り込まない」ことを実バケット・実 registry・実スキーマBで確認した（`project-b/negative.json` / `negative.log`、全 PASS）。

| チェック | 期待 | 実結果 |
|---|---|---|
| (a) 署名改ざん manifest の install | 拒否 | `SignatureRejected: 署名検証に失敗したため取込拒否`（ADB に書き込まれない） |
| (b) 無署名 manifest の install | 拒否 | `SignatureRejected: 未署名 manifest は取込拒否` |
| (c) 既公開 (id,version) の再 publish | 409 | `HTTP 409: …は既に publish 済み(版は不変)` |
| (d) A のローカル UC 行 id を B から参照 | 不可視 | `get_usecase(B, <A row id>) -> None`（スキーマ分離の確認） |

## 6. 再現方法

証跡と完全な実行スクリプトは `runs/2026-06-26T0252_PLG-08/e2e/scripts/` に保存（`registry_server.py` / `project_a.py` / `project_b.py` / `project_b_negative.py` / `run_all.sh` / `cleanup_plg08.py`）。環境依存値（compartment OCID・バケット名・ADB DSN・パスワード）と秘密値（署名鍵・トークン）はすべて環境変数 / `.env` から注入し（未設定なら `${VAR:?}` で停止）、リポジトリにコミットしていない。

実行手順の要約:
1. loop ADB（`jetuseloop`）の admin パスワードをリセット → ウォレット生成 → `JETUSE_PLG08_A` / `JETUSE_PLG08_B` を作成し各々マイグレーション適用。
2. 環境変数（compartment / registry bucket / publisher token / ed25519 署名鍵 / publish URL）を設定して `run_all.sh` を実行。
3. レジストリサービス（実バケット）起動 → A 公開 → バケット確認 → B 取込・実行（SSE）→ 負シナリオ。

## 7. 後始末・残課題

- 実バケット `jetuse-registry` には本検証の plugin（plg08-e2e/*）が残っている。ステージ1出口デモ後の後始末は `scripts/cleanup_plg08.py`（**スコープ限定**: `plugins/<publisher>/` 配下と index.json 内の当該 publisher エントリ／鍵のみ。他 publisher のデータは温存。既定 dry-run、`CONFIRM=1` で実削除）。バケット全削除はしない（既存リソースの破壊的変更は人間ゲート）。ADB スキーマ `JETUSE_PLG08_A/B` は loop ADB 上に残置（再実行容易のため。不要なら `DROP USER ... CASCADE`）。
- 残る人間ゲート: **ステージ1 出口判定（デモ承認）**（`tasks/PLG-08.md` 非ゴール）。本検証はその判断材料を提供する。
- 既知の適応点（§2 注記）: 私設バケット再利用のため購読側読取に OCI 署名 transport を注入。本番は Object Storage 公開 URL を `plugin_registry_url` に設定する（検証・取込ロジックは無改変）。
