# TOOL-01 単体ゲートの実行ログ

$ .venv/bin/pytest packages/api/tests -q
Required test coverage of 45% reached. Total coverage: 71.71%
688 passed, 7 warnings in 15.21s
<sys>:0: DeprecationWarning: builtin type swigvarlink has no __module__ attribute

$ .venv/bin/ruff check packages/api spikes ops
All checks passed!
