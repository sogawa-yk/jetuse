# Codex レビューの記録

判定の正本は `review-<n>.json`（verdict / severity_counts / findings）。
`review-<n>.input.diff` / `.payload.txt` / `.raw.txt` は生成物で、レビュー回数が増えると
**それ自体が次回のレビュー入力に入って再帰的に肥大する**ため git には入れない
（本 run は 11 回まわしており、全部入れると入力が codex の 1MB 上限を超える）。

## 経緯（要約）
- review-1: FAIL（blocker 2 / major 5 / minor 1）— 証跡への実パスワード混入、E2E不足ほか
- review-2: FAIL（blocker 1）— 既存Stackのアップグレードで apply が失敗する経路
- review-3〜10: PASS（blocker 0）。各回の major/minor を潰しながら再実行
