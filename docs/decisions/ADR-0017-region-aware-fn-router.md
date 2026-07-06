# ADR-0017: 非Osakaリージョンでは Functions ルーターを自動無効化する

日付: 2026-07-06
状態: 提案中（Issue #55 対応。PR merge をもって承認とする）

## 背景

Issue #55: `ap-tokyo-1` で ORM スタックを apply すると
`module.functions.oci_functions_function.router[0]` の作成が
`400-InvalidParameter, The image must be an OCIR image in this region` で失敗する。

原因は ADR-0011 の既知制約: **OCI Functions は関数と同一リージョンの OCIR イメージしか
受け付けない**（プラットフォーム制約・回避不可）が、既定イメージが Osaka
（`kix.ocir.io/<namespace>/jetuse-fn-router:latest`）固定のため、Osaka 以外では必ず衝突する。

## 決定

**デプロイリージョンのキーが `ocir_region_key` と一致しない場合、fn-router の既定イメージを
空にして router を作成しない。**

- リージョンキーは既存の `data.oci_identity_region_subscriptions`（providers.tf）から導出
  （デプロイ先は必ずサブスクライブ済みリージョンのため常に解決可能。ハードコード表不要）。
- router 不在時、対象ルート（`presets` / `dbchat` / `tts`）は API Gateway の
  catch-all `/api/{p*}` → Container Instance にフォールバックする（router のオプショナル化は
  specs/02-infra §api-gateway「空ならルート生成しない」で既定義の挙動）。
  機能は全て動作するが、フォールバック経路は catch-all の read timeout 60 秒の制約を受ける
  （チャット SSE `/api/chat/{p*}` は専用ルートで従来どおり 300 秒・本変更の影響なし）。
- 他リージョンで router を使う場合は、イメージを自リージョン OCIR へミラーして
  `ocir_region_key` + `ocir_namespace`（または `fn_router_image`）を明示指定する。
  明示指定は自動無効化より常に優先。

## 理由

- **失敗するより機能縮退**: 既定値のまま任意リージョンで apply が通る。ワンクリック配布の
  目的（ADR-0014）に対し、Osaka 限定の apply 失敗は最悪の UX。
- 追加インフラ不要・差分最小。マルチリージョン publish（下記）を採用しても本フォールバックは
  「イメージ未公開リージョン」の安全網として残る。

## 却下した代替案

- **マルチリージョン publish**（release.yml が nrt 等へも push + リージョンキー自動導出）:
  真のワンクリックに最も近いが、各リージョンでの OCIR repo 手動作成（ADR-0011: repo は人間管理）、
  テナンシのリージョンサブスクリプション、別テナンシからの cross-tenancy pull を Functions が
  許すかの実機検証が必要。需要を見て将来対応（本 ADR のフォールバックと排他ではない）。
- **ドキュメントのみ**（ミラー手順の案内だけ）: 既定値での apply が失敗したままで解決にならない。

## 検証

`docs/verification/issue-55-region-aware-fn-router.md` 参照。リージョンキー導出と
無効化判定は実テナンシの `oci_identity_region_subscriptions` に対して両リージョン分を評価し確認
（Osaka=`kix`一致で既定イメージ合成、Tokyo=`nrt`不一致で空）。空→router 0→ルート無しの経路は
既存実装（modules/functions の count、main.tf の fn_routes）。ap-tokyo-1 での実 apply は
Resource Manager 経由のため人間ゲート。
