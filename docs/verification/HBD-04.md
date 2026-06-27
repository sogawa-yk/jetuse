# HBD-04 合成バリデーション（ガバナンス4制約）検証レポート

- タスク: HBD-04 合成バリデーション（許可組合せ・必要ケイパ・権限スコープ・モデル可用性）
- area: api（バリデーション中核）/ web は検証結果の提示
- base: feat/stage-2（HBD-03 合成エンジン統合済）
- run: `runs/2026-06-26T1901_HBD-04/`
- 仕様参照: `docs/enhance/202607-demo-platform-plan.md` §4 / §4-3 / §10「HBD-04」、
  `docs/enhance/202607-hearing-flow.md` §3 Auto

## 1. 何を作ったか

HBD-03 の合成済みデモ構成（`synth.DemoComposition`）を**デプロイ前ゲート**として §4 ガバナンス
4制約で機械判定する合成バリデータを追加した。外れた構成は弾き、各違反に**代替提案（外させない）**を添える。

- `jetuse_core/governance.py`（新規）
  - `validate_governance(composition, *, available_models=MODELS, allowed_connectors=CORE_CONNECTORS)`
    → `GovernanceReport`（`ok` / `violations[]` / `checks{}`）。副作用なし・決定的（DB/GenAI 非依存）。
  - `GovernanceViolation`: `kind` / `element` / `element_type` / `detail` / `alternative`（機械可読）。
  - `available_model_features(models=MODELS)`: 実行リージョン（既定 ap-osaka-1）の可用モデル能力集合。
- `service/routes/hearing.py`: `POST /api/hearing/sessions/{sid}/validate`（デプロイ前ゲート。
  構成＋ガバナンス判定を返す）。
- テスト: `tests/test_governance.py`（15 ケース）/ `tests/test_hearing_route.py`（/validate 4 ケース）。

### 判定する4制約と実装の対応（二重定義しない）

| 制約（§4-3） | 実装 | 出典 |
|---|---|---|
| (a) 許可組合せ（sample-app × AI部品） | synth の `no_slot` 束縛を `disallowed_combination` に翻訳 | bindings[].status |
| (a) 許可組合せ（connector パレット） | `connectors ⊆ CORE_CONNECTORS`（コア=Slack） | §6 D9 |
| (b) 必要ケイパビリティ束縛済み | synth の `unbound` 束縛を `unbound_capability` に翻訳 | ai_runtime.bound_capabilities |
| (c) 権限スコープが manifest 内 | SBA-01 `composition_report.undeclared_permissions` を**再利用** | sample_app.validate_composition |
| (d) モデル可用性（ap-osaka-1） | capability→必要モデル能力（vlm.ocr=vision）を可用集合と照合 | models.py / hearing-flow §3 Auto |

> 設計: 「記述は synth、強制は governance」。synth が束縛状態を**記述**（warning）し、governance が
> デプロイ前ゲートとして**強制**（hard fail）する。capability/permission 集合の再計算はせず、SBA-01 の
> `composition_report` をそのまま使う（受け入れ条件「二重定義しない」）。

## 2. 受け入れ条件の充足

- [x] (a)許可組合せ (b)必要 capability 束縛 (c)権限スコープ (d)モデル可用性 を判定
- [x] 違反は機械可読（違反種別・該当要素・代替提案）で返し、**代替提案**を含む
- [x] 既存の合成バリデーション土台（SBA-01 / `validate_composition`）と整合・二重定義しない
- [x] 正常 PASS／各違反種別が個別 FAIL する単体テストを網羅（15 ケース）
- [x] api lint（ruff）クリーン・既存テスト後方互換（596 passed）

## 3. 単体テスト・lint

```
.venv/bin/python -m pytest packages/api/tests   → 596 passed
.venv/bin/ruff check packages/api               → All checks passed!
```

各違反種別の個別 FAIL: `disallowed_combination`(capability/connector) / `unbound_capability` /
`scope_out_of_manifest` / `missing_host_capability` / `model_unavailable` / `unresolved_composition`。

## 4. 実環境 E2E（jetuse-dev / loop ADB・専用スキーマ JETUSE_HBD04 隔離）

証跡: `runs/2026-06-26T1901_HBD-04/e2e/`（`deploy.log` / `e2e-run.log` / `scenario-1..3.json` / `SKIPPED.md`）。

- デプロイ: loop ADB `jetuse-loop-adb` 再利用 → 専用スキーマ `JETUSE_HBD04` 隔離作成 → migrate（全17適用＋冪等再適用クリーン）。
- シナリオ1（正常）: SBA-A＋RAG-QA＋Slack を実 DB に永続 → `/validate` で `governance.ok=true`・全制約 PASS。
- シナリオ2（違反→代替）: SBA-A＋業務DB で nl2sql が許可外組合せ → `ok=false`・代替提案（主アプリを SBA-B/SBA-C へ）。
- シナリオ3（能力前提）: vlm.ocr を組合せ違反で弾く＋VLM 不可レジストリで `model_unavailable` を実証。既定 ap-osaka-1 は vision 可用。
- 結果: **ALL PASS**。GenAI による代替提案の言い換え（best effort）は不実施（ゲートはルールベースで完結。理由は `SKIPPED.md`）。

## 5. 残る人間ゲート

- コミット / PR / push（feat/HBD-04 → base）。
- 本タスクで作成・変更した実リソースは無し（loop ADB は参照・再利用、専用スキーマは隔離・使い捨て）。
- web の検証結果表示の本実装は別ターン（提示レイヤ。本ゲート API はバックエンド完結）。
