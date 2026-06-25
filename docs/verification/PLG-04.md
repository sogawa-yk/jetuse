# PLG-04 検証レポート: 中央レジストリ Service（MVP）

日付: 2026-06-25
仕様: docs/enhance/202607-demo-platform-plan.md §6 / docs/comparison/marketplace-plugin.md §2 / specs/16-platform.md / ADR-0013
状態: **plan＋統合テストまで完了（実バケット作成＝apply は人間ゲートのため未実施）**
run-id: `runs/2026-06-25T1545_PLG-04`

## 概要

ベンダー運用の中央プラグインレジストリ（D2）本体を `packages/registry`（`jetuse_registry`）として実装した。
保存層は Object Storage（`index.json` ＋ 発行者公開鍵 ＋ プラグイン成果物）。読取（list/search/get/
download）は公開、publish は発行者認証＋ed25519 署名検証（PLG-01 の `manifest.py` を再利用）。実バケットの
作成は Terraform `infra/terraform/modules/plugin-registry`（**plan まで**。apply は課金・人間ゲート）。

## 受け入れ条件と結果

| 受け入れ条件 | 結果 | 根拠 |
|---|---|---|
| list / search / get / download / publish API を提供 | ✅ | `jetuse_registry/app.py`（FastAPI）＋ `service.py` |
| Object Storage を保存層とし、publish 時に index.json を更新 | ✅ | `storage.py`（`ObjectStore`）/ `index.py` / `service.publish()` |
| 発行者認証＋公開鍵登録＋publish 時の署名検証 | ✅ | `publishers.py`（Bearer→publisher）/ `register_public_key` / `verify_signature` |
| 無署名 publish を拒否 | ✅ | `service.publish` step4（422）／test `test_publish_rejects_unsigned`・`test_http_publish_rejects_unsigned` |
| publish→index更新→list/get/download の統合テスト | ✅ | `test_publish_then_index_then_list_get_download` / `test_full_http_publish_index_list_get_download` |
| `infra/terraform/modules/plugin-registry` が plan クリーン | ✅ | 下記 plan 結果（Plan: 3 to add, 0 change, 0 destroy） |
| docs/verification/PLG-04.md に実行ログ | ✅ | 本ファイル |

## 実行結果

| チェック | コマンド | 結果 |
|---|---|---|
| 単体＋統合テスト | `.venv/bin/pytest packages/registry/tests` | **76 passed**（並行制御・鍵不変性・公開鍵取得・成果物 sha 整合・OCI アダプタ fake のテスト含む） |
| 既存 api 回帰 | `.venv/bin/pytest packages/api/tests` | **220 passed**（PLG-01 manifest 再利用に副作用なし） |
| lint | `.venv/bin/ruff check packages/registry` | All checks passed |
| terraform fmt | `terraform fmt -check -recursive infra/terraform/modules/plugin-registry` | クリーン（exit 0） |
| terraform validate | （plan-check root） | Success! The configuration is valid. |
| terraform plan（実 OCI / ap-osaka-1 / jetuse-dev） | `terraform plan`（apply せず） | **Plan: 3 to add, 0 to change, 0 to destroy**（バケット＋読取PAR＋time_offset） |

証跡:
- `runs/2026-06-25T1545_PLG-04/e2e/terraform-plan.log`（OCID・namespace は sensitive で自動 redaction）
- `runs/2026-06-25T1545_PLG-04/e2e/integration-tests.log`（registry 76 tests の詳細）
- `runs/2026-06-25T1545_PLG-04/e2e/checks.log`（ruff・registry tests・**api 回帰 220**・terraform fmt の一括証跡）
- `runs/2026-06-25T1545_PLG-04/e2e/SKIPPED.md`（実バケット E2E を apply 人間ゲートで SKIP した理由）
- `runs/2026-06-25T1545_PLG-04/reviews/`（Codex レビュー判定）

## アーキテクチャ（MVP）

```
packages/registry/jetuse_registry/
  storage.py    ObjectStore プロトコル + InMemoryObjectStore（テスト）+ OciObjectStore（本番）
  index.py      index.json（RegistryIndex / IndexEntry / PublisherKey）
  publishers.py 発行者認証（Bearer→publisher、トークンは sha256 で保持・定数時間比較）
  semver.py     最新版選択（version 省略時の get/download）
  service.py    RegistryService（list/search/get/download/publish/register_public_key/get_publisher_keys）
  app.py        FastAPI（読取公開 / publish・鍵登録は Bearer 認証）
                GET /registry/publishers/keys?publisher= = 取込側(PLG-03)の署名検証用に公開鍵を公開
infra/terraform/modules/plugin-registry/   バケット＋読取PAR（plan まで）
  examples/plan-check/                      plan 検証用ルート（apply しない）
```

publish フロー（無署名拒否を含む）: 発行者認証 → manifest 検証（PLG-01） → publisher 一致（なりすまし
防止 403） → **署名存在チェック（無署名は 422 拒否）** → 登録公開鍵 lookup → ed25519 署名検証 →
版の不変性（既存版は 409） → 成果物保存（sha256 付与）→ index.json 更新。

## セキュリティ／運用上の確定事項

- 認証情報・OCID・namespace・PAR の access_uri はコミットしない（`.env` / `TF_VAR_` / output は sensitive）。
  `compartment_ocid`（variable `sensitive=true`）と namespace（`sensitive()` でラップ）は plan 出力で
  `(sensitive value)` に自動 redaction される（手動 REDACTED に依存しない）。
- 読取 PAR 失効は `time_offset`（apply 時刻 + `read_par_expiry_days`、既定 365 日）で確定し、固定日付の
  劣化（既定のまま期限切れ→読取経路停止）を回避。明示 `read_par_expiry` 指定で固定も可。
- 発行者トークンは平文保持せず sha256 ハッシュで突き合わせ、比較は `hmac.compare_digest`（定数時間）。
- 公開済みプラグイン版は不変（(id,version) 一意で 409）。保存層もバケットの versioning=Enabled で保全。
- 読取配布は PAR（AnyObjectRead、リスト不可）。index.json を入口にする。
- **並行制御**: index.json は楽観的並行制御（etag / `if_match` / `if_none_match`）で read-modify-write し、
  衝突時はリトライ。成果物は `if_none_match=*` で書き、同一 (id,version) で内容の異なる並行 publish が
  成果物を上書き（sha と download の不整合＝版不変性破り）するのを防ぐ。`InMemoryObjectStore` も etag を
  模し、競合シナリオ（更新消失なし／重複 409／成果物上書きなし）をテストで決定的に検証（Codex review-2 blocker 対応）。
- **発行者公開鍵は不変**: 同一 `(publisher, publicKeyId)` の鍵差し替えは 409 で拒否（過去 publish 済み
  manifest の検証可能性を保護）。同一鍵の再登録は冪等（鍵の等価判定はデコード後のバイト列で行う）。
- **成果物の完全性**: 成果物パスは sha256 を含む content-addressed。get/download 時に読んだバイト列の
  sha256 を index の値と照合し、不一致（破損/改ざん）は取込側へ渡さず 500 で拒否。欠落も 500 で表面化。
- **CI 配線**: `.github/workflows/ci.yml` に `registry` ジョブ（install/ruff/pytest）と plugin-registry
  モジュールの `terraform validate` を追加（PR で回帰検知）。

## 実バケット E2E（apply 承認後・2026-06-25 実施）

ユーザーが本セッションで Terraform apply・課金・デプロイ・実環境E2E を直接承認。**apply 実行 → 実
Object Storage バケット `jetuse-registry`（jetuse-dev / ap-osaka-1）作成 → 本番アダプタ `OciObjectStore`
での実バケット E2E を実施し 8/8 PASS**。証跡: `runs/2026-06-25T1545_PLG-04/e2e/RESOLVED.md` /
`real_bucket_e2e.py` / `real_bucket_results.json` / `real_bucket_e2e.log`。

- apply 作成リソース: bucket `jetuse-registry`（NoPublicAccess / versioning=Enabled / 規定tags）＋
  読取PAR（AnyObjectRead / 失効 apply時刻+365d）＋ time_offset。`Plan: 3 to add` をそのまま適用。
- 実 E2E: 公開鍵登録 / 署名publish→index更新 / list・search / get・download往復 / 無署名拒否 /
  改ざん署名拒否 / 版不変409 / 実バケット内オブジェクト実在（SDK list で独立確認）= 全 PASS。
- 後始末: テスト発行物は削除（バケットは空）。バケット＋PARは動作確認のため残置（破棄は `terraform destroy`）。
- namespace/OCID/PAR は秘匿（実値は証跡・ログ・本書に残さない）。

## 残課題（後続・人間ゲート）

- [x] Terraform apply（実バケット作成）→ 実バケットでの E2E … **2026-06-25 実施・8/8 PASS**（上記）
- [ ] コミット / PR / push（人間承認）
- [ ] 第三者 publisher を伴う相互運用での数値正準化（JCS/RFC 8785）の ADR 化（specs/16 §6 注記。本 MVP は単一実装署名で範囲外）
- [ ] 発行者認証の IAM/Identity Domain 統合・μService 高度化（評価・DL数・レビュー）はステージ4
- [ ] orphan 成果物の GC: index 更新が並行衝突でリトライ上限超過した稀ケースで、成果物だけが残る
  （content-addressed なので後続 publish を汚染せず・index から参照されない無害なゴミ）。将来の
  スイーパー（index 非参照かつ一定時間経過の成果物を削除）で回収する。本 MVP は許容（plan/エミュレート
  統合テストで検証範囲）。

> 本番 OCI アダプタ（`OciObjectStore`）の結線は、OCI セマンティクスのフェイク transport を用いた
> `tests/test_oci_integration.py` で publish→index→get/download/list・`if_none_match='*'` 新規 index 作成・
> `if_match` 条件付き更新・412→PreconditionFailed・重複 409 まで検証済み（実バケット疎通のみ apply 人間ゲート）。
