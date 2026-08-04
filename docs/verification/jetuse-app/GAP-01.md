# GAP-01 検証レポート: ガードレール（マネージド導入分のみ）

- 日付: 2026-06-13 / ブランチ: `task/gap-01` / 計画: docs/plan-gap-b.md
- 調査: SPIKE-G1（出力モデレーション方式）+ 追補（OCIマネージド ApplyGuardrails 実機調査）
- 方針（ユーザー判断 2026-06-13）: **OCIマネージドで実際に機能する部分だけを導入する。
  マネージドに無い/効かない部分は今回実装せず、その旨をメモする。**

## 実装した（マネージドで機能する）

- **プロンプトインジェクション検知**: OCI Generative AI の ApplyGuardrails API（オンデマンド、
  ネイティブSDK、RP/ユーザー署名両対応）。`jetuse_core/guardrails.py`
- チャット入力（`/api/chat/stream` の最終userメッセージ）に適用。`PROMPT_INJECTION_GUARD_ENABLED`
  （既定off）で有効化。検知時はSSEエラーで中断＋監査 `prompt_injection_block`（score付き）
- ベストエフォート（API失敗時はfail-open=通す）。言語非依存（日本語入力でも検知）

## 今回実装していない（OCIマネージドに無い/効かないため）

| 項目 | 理由（実機確認） |
|---|---|
| **コンテンツモデレーション（入力・出力）** | マネージドCMは**日本語非対応**（`Language ja is not supported` 400）。本アプリは日本語主体のためマネージドでは実現不可。**マネージド方式としては今回見送り**（※非マネージドのLLM自己判定=SEC-02の `MODERATION_ENABLED` は別途存在するが、本ラウンドの「マネージド導入」スコープ外） |
| **PII保護** | マネージドのPII検知がデフォルト設定で未検知（英語の明示的PIIでもnull）。entity指定等の追加調査が要るため今回見送り |

> 補足: 出力ガードレール（GAP-01当初の主目的）は、マネージドのコンテンツモデレーションが
> 日本語非対応のため**マネージドでは実装できない**。よって本ラウンドでは出力モデレーションは未実装。

## 実機E2E

- `PROMPT_INJECTION_GUARD_ENABLED=true` で「Ignore all previous instructions…」系 → 中断（監査記録）
- 通常入力 → 素通り。ガード無効時は検知呼び出し自体を行わない
- ユニットテスト4件（fail-open / しきい値 / 遮断 / 無効時）

## 残（backlog）

- マネージドCMの英語コンテンツやPII entity指定の活用は、必要が出れば再調査（日本語CMはOCI側の対応待ち＝定点観測）
