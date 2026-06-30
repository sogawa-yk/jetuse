# ステージ0 進捗キュー（stage-runner の単一の真実源）

Experience Builder の第一ステージ＝**契約とベースライン**。`stage-runner` が依存順に消化し、PASS したタスクを
ステージ専用ローカルブランチ `feat/stage-0`（base=`dev`）へ自動 commit+merge する。push / dev への PR /
apply / ADR 承認は自走中も停止（人間ゲート）。詳細は各 `tasks/EXB-0X.md`、索引は
[`README-experience-builder.md`](README-experience-builder.md)。

status: `todo` | `in_progress` | `blocked` | `done`

| 順 | タスク | 依存 | 人間ゲート | status |
|---|---|---|---|---|
| 1 | EXB-00 ベースライン確定＋ADR-0022 | — | ADR-0022 承認 | done |
| 2 | EXB-01 MVP契約スキーマ(JSON Schema) | EXB-00 | コミット(spec逸脱 施主承認済) | done |
| 3 | EXB-02 RAG Reference Descriptor(静的)＋Catalogローダー | EXB-01 | コミット | in_progress |

> 依存が直列なので1波1タスクで進む（EXB-00 → EXB-01 → EXB-02）。EXB-00 の ADR-0022 は方針確定の
> 真の決定を含むため **人間ゲート**（stage-runner はドラフトまで進め、承認はステージ報告でまとめて仰ぐ）。
> EXB-00 が blocked（ADR 未承認）でも EXB-01/02 は EXB-00 の成果物（baseline 確認）に依存するため待機する。

## 完了条件（ステージ報告で人間が確認）
- 3タスクすべて Codex review PASS・test/lint クリーン・実環境 E2E（または理由付き SKIPPED）通過。
- `dev` 上で main 由来の既存テストが回帰なし。
- MVP の主要契約（Experience / DemoBundle / answer.with-citations@1 / Run イベント語彙）と RAG Descriptor が
  レビュー可能な形（specs/ の JSON Schema ＋静的 descriptor）で存在する。
- ADR-0022 がドラフトされ、人間承認を待つ状態。

## 実行ログ（stage-runner が追記）
- 2026-06-30 ステージ開始: `feat/stage-0`（base=dev）で直接進行（直列ステージ＝per-task worktree 不要・環境再利用）。
- 2026-06-30 EXB-00 done: ベースライン回帰なし（api 220 passed / web build 成功）。ADR-0022 ドラフト＋README リンク。
  Codex review **PASS**（review-3: blocker0/major0/minor0。途中 README 確定誤認・既存Marketplace矛盾・正本誤記の
  major/minor を修正）。実環境E2E は SKIPPED（理由明記）。3a5f9f3 で feat/stage-0 へ commit。
  **残ハードゲート: ADR-0022 承認**（ステージ報告で提示）。→ 次: EXB-01。
- 2026-06-30 EXB-01 = blocked（spec逸脱 ratification 待ち）。コードは完成・全緑（contract 74 / api 294 passed・
  ruff クリーン・実 wheel ビルドで schemas 同梱を証明）。fa842b7 で feat/stage-0 へ commit。
  Codex review **7 ラウンド**: import時配布破損／@cache 汚染／共有validator汚染／不正TZ／空knowledge 等の
  **実害を段階的に解消**（合成ループが実装前に止めた）。残るのは**ガバナンス2点**（自走では越えない）:
  ①スキーマ正本を specs/ でなく実装パッケージに同梱（配布のため。EXB-01「specs/が正本」からの逸脱＝spec-driven
  人間レビュー事項）②jsonschema を直接依存に明示宣言（既存推移依存・新規パッケージ増ではないが文言と衝突）。
  + leap second 非対応（既知の狭め・実害なし）。→ EXB-02 は EXB-01 ratification 待ちで停止。**ステージ報告へ**。
