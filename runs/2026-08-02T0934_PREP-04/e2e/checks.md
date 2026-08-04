# 完了条件のコマンド実行記録（テスト・lint）

## `.venv/bin/pytest packages/api/tests`
```
TOTAL                                           5873   1600    73%
Required test coverage of 45% reached. Total coverage: 72.76%
694 passed, 7 warnings in 22.40s
<sys>:0: DeprecationWarning: builtin type swigvarlink has no __module__ attribute
exit=0
```

## `.venv/bin/ruff check packages/api`
```
All checks passed!
exit=0
```

## `.venv/bin/ruff check spikes/prep04 --config spikes/ruff.toml`（E2E ハーネス）
```
All checks passed!
exit=0
```
