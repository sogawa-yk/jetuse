# ADR-0017: 対応リージョンを4つに固定し、OCIRレジストリをデプロイリージョンから自動導出する

日付: 2026-07-06
状態: 承認済み（Issue #55 対応。方式は 2026-07-06 ユーザー決定。PR merge をもって確定）

## 背景

Issue #55: `ap-tokyo-1` で ORM スタックを apply すると
`module.functions.oci_functions_function.router[0]` の作成が
`400-InvalidParameter, The image must be an OCIR image in this region` で失敗する。

原因は ADR-0011 の既知制約: **OCI Functions は関数と同一リージョンの OCIR イメージしか
受け付けない**（プラットフォーム制約・回避不可）が、既定イメージが Osaka
（`kix.ocir.io/<namespace>/jetuse-fn-router:latest`）固定のため、Osaka 以外では必ず衝突する。

## 決定

**対応リージョンを 大阪（kix）/ 東京（nrt）/ アシュバーン（iad）/ シカゴ（ord）の4つに固定し、
イメージを4リージョンの OCIR へ事前 push、レジストリはデプロイリージョンから自動導出する。**

- `release.yml` が API / fn-router イメージを GHCR に加えて **4リージョンの OCIR すべて**へ push。
- ORM スタックはリージョンキーを**ユーザー入力にしない**（`ocir_region_key` 変数と schema 入力を
  削除）。デプロイリージョンのキーは既存の `data.oci_identity_region_subscriptions`
  （providers.tf でホームリージョン導出に使用済み）から導出し、レジストリ
  `<region-key>.ocir.io` を自動選択する。ハードコードのリージョン→キー対応表は持たない。
- 対応外リージョンでは `terraform_data.region_guard` の precondition が **plan 時に明示エラー**で
  停止する（apply 途中の不可解な pull / CreateFunction 失敗を防ぐ）。イメージを自リージョン OCIR へ
  ミラーして `api_image_url` と `fn_router_image` の両方を明示指定した場合のみガードを通過する。
- ADR-0011 の「イメージは OCIR(ap-osaka-1)」を**4リージョンへ拡張**する更新。repo の手動管理・
  public 公開・ネームスペースベース参照の方針は維持。

## 理由

- **どのリージョンでも同一機能**: fn-router が常に有効で、リージョンによる機能差
  （SSE タイムアウト等）が生じない。プリセールス用途でデモ品質が揃う。
- **入力ミスの根絶**: リージョンキーの手入力を廃し、デプロイリージョンから機械的に導出。
- 4リージョン（日本2 + 米国2）は想定利用地域をカバーし、repo 管理コスト（2 repo × 4 リージョン）は
  許容範囲。

## 却下した代替案

- **非対応リージョンで fn-router を自動無効化（機能縮退でデプロイ続行）**: 本 ADR の初案。
  追加インフラ不要だが、対象ルート（presets/dbchat/tts）が catch-all の read timeout 60 秒に
  黙って縮退し、リージョンで挙動が変わる。ユーザーレビューで却下（2026-07-06）。
- **全リージョン publish**: repo 事前作成と push 時間が線形に増える。需要のない地域まで
  維持するコストに見合わない。需要が出たリージョンを4つの集合へ追加する運用とする。
- **ドキュメントのみ**（ミラー手順の案内だけ）: 既定値での apply が失敗したままで解決にならない。

## 前提となる人間側の作業（merge 前）

1. **OCIR リポジトリの事前作成**（ADR-0011: repo は人間管理）: `jetuse-api` / `jetuse-fn-router` を
   nrt / iad / ord の3リージョンに **public** で作成（kix は作成済み。テナンシは4リージョンとも
   サブスクライブ済みを確認済み）。無いと release.yml の push がルートコンパートメントへの
   自動作成を試み権限不足で失敗する。
2. merge 後に release.yml が4リージョンへ push（以後 main への push ごとに自動）。

## 既知の未検証点

- **別テナンシからの cross-tenancy pull を Functions が許すか**は未検証（本リポジトリの検証は
  同一テナンシのみ。Issue #55 の報告者はリージョンチェックで先に失敗しており切り分け不能）。
  不可だった場合、別テナンシ利用者は従来どおりイメージミラー + 明示指定が必要。
- 東京 / アシュバーン / シカゴでの実 apply（Resource Manager 経由・人間ゲート）。

## 検証

`docs/verification/issue-55-region-aware-fn-router.md` 参照。
