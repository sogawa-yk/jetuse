# CHAT-04b 検証レポート: 生成パラメータ拡張（top_p / max_tokens / reasoning effort）

日付: 2026-06-11
仕様: specs/07-chat.md [CHAT-04b]
状態: **実機E2E完了**（イメージ 0.6.1→0.7.1、SPA同時デプロイ）

## 実装

- API: `ChatRequest` に `top_p`(0<x≦1) / `max_tokens`(1〜32768) / `reasoning_effort`(low/medium/high) を追加。未指定はAPIに渡さない（=モデル既定）
- 系統マッピング: Responses系= `top_p`/`max_output_tokens`/`reasoning.effort`（effortは `reasoning=True` のモデルのみ）。Chat系= `top_p`/`max_tokens`（effortは黙って無視）
- `GET /api/chat/models` が `api` / `reasoning` / `min_max_tokens` を返し、UIが出し分け（effortセレクタはgpt-oss選択時のみ表示）
- UI: top_pスライダー（1.0=モデル既定で送信しない）、最大出力トークン数値入力（空=制限なし）、推論の深さセレクタ

## 実装中に発見した問題と対処（実測）

1. **Gemini系は小さいmax_tokensで本文が空になる/ストリームが返らない**: 思考トークンがバジェットを消費するため。max_tokens=100でストリーム90秒無応答、512でも `finish_reason=max_tokens` で本文空、2000で正常。`reasoning_effort=minimal` は受理されるが思考は止まらない（none/lowは400）→ **モデル定義に実用下限 `min_max_tokens=2048` を持たせサーバー側でクランプ**。UIにも下限と注記を表示
2. **潜在バグ修正**: `stream_chat()` 呼び出しがtry外にあり、同期例外時にSSEがkeepaliveのまま永久ハングする構造だった → try内移動+except時にerrorイベント送出（テストのfake署名漏れで発覚）

## 実機E2E（API GW経由、イメージ0.7.1）

| ケース | 結果 |
|---|---|
| gpt-oss `reasoning_effort=low` / `high`（同一プロンプト・temp0） | 出力 **110 tok / 310 tok** — effortが推論量を制御 |
| gpt-oss `max_tokens=50` + `top_p=0.5` | 出力ちょうど50 tokで打ち切り |
| gemini-flash `top_p=0.5` + `max_tokens=100` | クランプ(→2048)で**本文付き正常完走**（修正前は90秒無応答） |
| `top_p=1.5` | 422（バリデーション） |
| `GET /api/chat/models` | api/reasoning/min_max_tokens 返却 |

- pytest 24件パス / ruff / web lint+build クリーン

## 備考

- stop / frequency・presence penalty / seed はChat系のみ対応のため見送り（要望が出たら追加。対応マトリクスは docs/tips.md）
- ディスク逼迫（96%）で旧イメージ0.2.x〜0.6.0を削除し6.4GB確保（0.6.1はロールバック用に保持）
