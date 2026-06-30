# ADR-0023: 契約 JSON Schema の配布と依存宣言（施主承認済み）

- 状態: **Accepted（施主承認 2026-06-30）**
- 日付: 2026-06-30
- 関連: [ADR-0022](./ADR-0022-experience-builder.md) / タスク EXB-01 / `specs/17-experience-builder/`
- 起票: stage-runner / stage-0 / EXB-01（Codex review で表面化した spec 逸脱を人間ゲートで提示し承認を得た）

## コンテキスト

EXB-01 の当初受け入れ条件は「`specs/` 配下の JSON Schema を spec-driven の正本とする」「新規依存を増やさない」
だった。実装の過程で次の2点が当初条件と食い違い、CLAUDE.md の spec-driven 原則により**人間レビューが必要**と判断、
ステージ報告で施主に提示した（2026-06-30）。施主は両方を**承認**した。

1. 契約スキーマは実行時に `jetuse_platform.contracts` のバリデータが読み、**配布 wheel / コンテナイメージへ
   同梱が必要**。生スキーマを `specs/`（パッケージ外）のみに置くと配布イメージで読めず壊れる（EXB-01 Codex
   review-1 の blocker）。
2. バリデータが使う `jsonschema` は既に推移依存として存在するが、推移依存頼みは脆く、直接依存として明示宣言する
   のが健全（EXB-01 Codex review-2 の指摘）。

## 決定（施主承認済み）

1. **機械可読スキーマの実体は `packages/api/jetuse_platform/contracts/schemas/*.json`** に置く（バリデータと同梱）。
   `importlib.resources` で読み、`[tool.setuptools.package-data]` で wheel に含め、`Containerfile(.fn)` は
   `jetuse_platform` を COPY する。dev / wheel / イメージのいずれでも同一スキーマで検証できる。
2. **`specs/17-experience-builder/` を当該契約の仕様文書（仕様の所在）**とする。spec-driven は「specs/ が仕様の所在」
   で満たす。生スキーマは一箇所（パッケージ同梱）のみに置きドリフトを防ぐ。
3. **`jsonschema` を直接依存として明示宣言**する（既存の推移依存を明示化するもので、環境への新規パッケージ追加では
   ない）。

## 帰結

- 配布イメージ・wheel・dev/test のすべてで契約検証が同一に動く（review-1 blocker の恒久解消）。`pip wheel` ビルドで
  schemas 同梱を実証するテストを持つ。
- spec-driven 運用注記: 「配布物へ同梱が必要な契約スキーマは、実体をパッケージに置き `specs/` に仕様文書を置く」を
  今後の慣行とする。
- 本決定は施主承認済みのため、EXB-01 を done として統合してよい。
