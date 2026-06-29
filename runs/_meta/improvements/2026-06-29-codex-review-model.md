# 2026-06-29 codex-review の既定モデルを gpt-5.6-Sol に固定

## 症状（人間指摘）
- Codex レビュー時のモデルを `gpt-5.6-Sol` にしたい。

## 証跡
- `.claude/skills/codex-review/scripts/run_codex_review.sh:66-67` — `CODEX_MODEL` 指定時のみ `--model` 付与。未指定時は Codex デフォルト任せで固定されていなかった。
- リポ全体に既定モデルの設定箇所なし（grep 済）。`loop-config.yml` にモデル項目なし。

## 注意（実機テストで判明）
- 表示名は `GPT-5.6-Sol` だが、Codex の **API スラッグは小文字 `gpt-5.6-sol`**。
  大文字 `gpt-5.6-Sol` を渡すと 400 `model is not supported` で失敗する（`~/.codex/models_cache.json` の slug が真）。

## 変更
- `run_codex_review.sh`: `CODEX_MODEL="${CODEX_MODEL:-gpt-5.6-sol}"` を追加し、既定モデルを `gpt-5.6-sol` に固定。

## 実機テスト（2026-06-29）
- `codex exec --model gpt-5.6-sol` 単体 → `turn.completed`（成功）。
- レビュースクリプトを最小 staged 差分で end-to-end 実行 → `review-1.json` の `model = "gpt-5.6-sol"` / `verdict = PASS`。デフォルト配線 OK。
  - `CODEX_MODEL` を明示指定した場合は従来どおり上書き可能。
  - 既定を入れたことでレポートの `model` フィールド（同 :96）も `gpt-5.6-Sol` を記録するようになる。

## 対象 run
- 次回以降の全 codex-review。

## 検証
- 次の run の `runs/<id>/reviews/review-*.json` の `model` が `gpt-5.6-Sol` になっていることを確認すること。
