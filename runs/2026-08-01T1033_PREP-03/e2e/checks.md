# 完了条件のコマンド実行記録（テスト・lint・ビルド）

タスクの完了条件に挙がっているコマンドを、**この差分の状態で**実行した記録。
コマンド・終了コード・集計行をそのまま貼る（回帰していないことの証跡）。

## `.venv/bin/pytest packages/api/tests`（api 全件）

```
packages/api/service/validators.py                38      4    89%
------------------------------------------------------------------
TOTAL                                           5494   1626    70%
Required test coverage of 45% reached. Total coverage: 70.40%
606 passed, 7 warnings in 12.12s
<sys>:0: DeprecationWarning: builtin type swigvarlink has no __module__ attribute
exit=0
```

## `.venv/bin/ruff check packages/api`

```
All checks passed!
exit=0
```

## `npm --prefix packages/web run test` / `run lint`（web も触ったため）

```
 Test Files  11 passed (11)
      Tests  76 passed (76)
test exit=0
> eslint .

lint exit=0
```

## `.venv/bin/ruff check spikes/prep03 --config spikes/ruff.toml`（E2E ハーネス）

```
All checks passed!
exit=0
```

> 既存形式（pdf / txt / md / xlsx）の取り込みテストは api 全件の中に含まれる
> (`test_rag.py` / `test_rag_adb.py` / `test_extract_xlsx.py`)。新規追加ぶんだけでなく
> **全件が緑**であることがこの記録の要点。

---

## 最終差分での再実行（2026-08-01 12:11）

```
608 passed, 7 warnings in 12.26s
<sys>:0: DeprecationWarning: builtin type swigvarlink has no __module__ attribute
All checks passed!
All checks passed!
      Tests  76 passed (76)
```

## 最終差分での再実行（2026-08-01 12:21・これが最終）

```
610 passed, 7 warnings in 13.26s
All checks passed!
All checks passed!
      Tests  77 passed (77)

```
