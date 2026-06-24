# ADR-0001: スパイク環境の確定（コンパートメント・モデル方針）

日付: 2026-06-10
状態: 承認済み（2026-06-10 人間チェックポイント①）

## 背景

`docs/plan.md` は検証用コンパートメント `jetuse-spike` の事前作成と、モデル候補に Grok 4.1 Fast / Llama 4 系を想定していた。実環境調査の結果、前提との差分が判明した。

## 決定

1. **コンパートメントは既存の `jetuse-proto` を使用する。** `jetuse-spike` は存在しない。スパイク用リソースは名前プレフィックス `jetuse-spike-` で識別し、誤削除を防ぐ。
2. **モデル候補から Grok 系・Llama 4 系を除外する（大阪リージョン）。** 公式の Models by Region によると、Grok 系は北米3リージョン（Ashburn/Chicago/Phoenix）限定、Llama 4 Maverick/Scout も大阪非提供。さらに Grok 4.1 Fast は 2026-05-15 非推奨化・2026-08-15 リタイア予定であり、計画書の記載は古い。
3. **大阪の第一候補モデル**: `openai.gpt-oss-120b` / `cohere.command-a-03-2025` / `google.gemini-2.5-flash` / `google.gemini-2.5-pro` / `meta.llama-3.3-70b-instruct`（SPIKE-01の実測で序列を確定）。
4. **認証は IAM 署名を採用**（`oci-genai-auth` で openai-python に署名注入）。コンソール発行の GenAI APIキー（sk- 形式）は人間作業が必要になるため初期開発では使わない。

## 影響

- Grok 前提の機能（高速・低価格帯のモデル切替候補）は gpt-oss-120b で代替する。
- 北米限定機能との比較が必要な場合のみ us-chicago-1 で追検証し、差分を `docs/verification/` に記録する（計画書のリージョン方針どおり）。

## 参照

- https://docs.oracle.com/en-us/iaas/Content/generative-ai/model-endpoint-regions.htm
- https://docs.oracle.com/en-us/iaas/Content/generative-ai/xai-grok-4-1-fast.htm
