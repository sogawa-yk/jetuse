# ADR-0011: デプロイ用コンテナイメージはOCIR(ap-osaka-1)に置く

日付: 2026-06-24
状態: 承認済み（2026-06-24 ユーザーが選択肢Aを選択）
更新: 2026-07-06 — ADR-0017 により push 先を4リージョン（kix/nrt/iad/ord）へ拡張し、
`ocir_region_key` 入力を廃止（レジストリはデプロイリージョンから自動導出）。
repo 手動管理・public 公開・ネームスペースベース参照の方針は維持。

## 背景

ORMワンクリックスタックの初回デプロイ（`jetuse-dev` コンパートメント）が APPLY で失敗した。
RMジョブログ（`docs/verification/jetuse-app/ORM-OCIR-DEPLOY.md`）から2つの根本原因を特定:

1. **Container Instance (API)**: `image_url = ghcr.io/sogawa-yk/jetuse-api:latest` が private のため
   pull 認証エラー（`A container's image could not be pulled because ... requires authorization`）。
2. **OCI Functions (fn-router)**: `image = ghcr.io/sogawa-yk/jetuse-fn-router:latest` が
   `400-InvalidParameter, The image must be an OCIR image in this region's registry` で拒否。

`release.yml` は GHCR にしか push しておらず、スタックが作る OCIR リポジトリ（`module.ocir`）は
空のまま。特に **OCI Functions は同一リージョンの OCIR イメージしか受け付けない**ため、
GHCR を public 化しても Functions は原理的に通らない。

## 決定

**API・fn-router の両イメージを OCIR(ap-osaka-1, `kix.ocir.io`) に push し、スタックは OCIR を参照する。**

- レジストリ: `kix.ocir.io/<namespace>/jetuse-{api,fn-router}:latest`（namespace は tenancy 固有）。
  パスはネームスペースベースで**コンパートメント非依存**。
- `release.yml` の `images` ジョブで GHCR に加えて OCIR にも push（OCIR ログイン + タグ追加）。
- スタックの `api_image_url` / `fn_router_image` の既定を OCIR パスにする
  （`ocir_namespace` / `ocir_region_key` 変数から locals で合成。override 可）。
- **OCIR リポジトリはスタックでは作らず、人間が手動で作成・管理する**（2026-06-25 改訂）。
  本番用コンパートメント `genu-proto` に `jetuse-api` / `jetuse-fn-router` を **public** で作成済み。
  当初は `module.ocir` で `jetuse-dev` に作る設計だったが、OCIRのrepo名はネームスペース内で一意のため
  同名repoの二重作成が衝突する。→ `module.ocir` を ORM スタックから除外。
- **OCIR リポジトリは public**。Container Instance / Functions は認証なしで pull でき、
  実行時の pull ポリシー/シークレットが不要。
- **push 認可の要点**: OCIRはrepoが無いと push 時に**ルートコンパートメントへの自動作成**を試み、
  権限不足で `not authorized` になる。**repoを事前作成しておけば**既存repoへの push として通る
  （repoの存在するコンパートメントで `use/manage repos`）。IAMは人間が付与（CLAUDE.md: IAM変更は承認必須）。

## 理由

- **Functions の OCIR 必須制約は回避不可**。GHCR public 化では解決しないため、OCIR 化が唯一の整合解。
- Container Instance も同じ OCIR を参照すれば配布元が一本化できる。
- **repoを手動管理にした理由**: jetuse-dev は開発用、コンテナイメージは本番用 `genu-proto` に置く
  という運用方針（2026-06-25）。stackがIAMやrepoのようなテナンシ/本番リソースに踏み込まず、
  人間が管理する境界を明確化（[[agent-no-tenancy-perms]] と同方針）。
- **pull を public 化した理由**: private OCIR からの実行時 pull は、Functions に
  `service faas to read repos`、Container Instance に image_pull_secrets(Vault) が要り構成が複雑。
  public 化（元の GHCR public 設計と同じ発想）で pull 側を権限ゼロにし、確実性を優先（2026-06-25 選択）。
- スタックは既に OCIR リポジトリを作る作りになっており、設計意図（OCIRネイティブ）にも合致。

## 却下した代替案

- **B. ハイブリッド（API=GHCR public / fn-router=OCIR）**: 配布経路が2系統になり一貫性が低い。
  GHCR の public 露出も残る。
- **C. GHCR public化のみ**: Functions が通らないため不成立（切り分け確認用としてのみ有効）。

## 既知の制約・今後

- **マルチテナンシ**: namespace は tenancy 固有のため、別テナンシの顧客が「真のワンクリック」で
  使うには、各自の OCIR へイメージをミラーする手段が要る（プロトタイプ段階では `jetuse-dev` と
  同一テナンシの OCIR を使う前提）。productization で再検討。
- **CI シークレット**: `release.yml` の OCIR push には GitHub Secrets
  `OCIR_USERNAME`（`<namespace>/<user>` 形式、Identity Domain 利用時は
  `<namespace>/oracleidentitycloudservice/<user>`）と `OCIR_TOKEN`（OCI Auth Token）が必要。
- **ブートストラップ順序**: OCIR リポジトリは push 前に存在が必要。`module.ocir` を含むスタックを
  一度 apply してリポジトリを作成 → `release.yml` で push → 再 apply（または Functions/CI を作る
  apply）で pull、の順。`jetuse-api` は作成済み、`jetuse-fn-router` は本変更の apply で作成される。
