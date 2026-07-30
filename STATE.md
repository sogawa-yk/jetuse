# STATE — RAGM-01（マネージド Vector Store に属性付与・構造化出典・版フィルタ）

- task: RAGM-01
- run_id: 2026-07-30T0025_RAGM-01
- branch: feat/RAGM-01（base: main。共有物のため main 起点）
- area: api
- review_verdict: **PASS**（review-2。blocker 0 / major 3 / minor 1・E2E adequacy=**sufficient**）
- last_review_ref: runs/2026-07-30T0025_RAGM-01/reviews/review-2.json
- updated_at: 2026-07-30

## やったこと

ADR-0020 §1 の `vector_store` バックエンド（既定）に、属性付き取り込み・構造化出典・版フィルタを実装した。
SPIKE-M1 で「OCI 側は対応済み・不足はアプリ側」と確定していた穴を塞ぐ作業。

- `jetuse_core/rag_metadata.py`（新規・唯一の門番）: 許可キー 8 種（`file` / `version` / `sheet` /
  `cells` / `sha256` / `kind` / `current_version` / `chunk_id`）を定数化。`normalize_attributes()` は
  **値の無いメタをキーごと省き**（`0`/`False` は残す）、未知キー・512 文字超・入れ子・非有限数を拒否。
  `validate_filters()` は `eq/ne/gt/gte/lt/lte` と `and/or` を許可し、**未知キーを深い位置でも拒否**、
  `in`/`nin` は上流未対応として拒否、既知フィールドだけに正規化する。
- `jetuse_core/rag.py`: `add_file(..., attributes=)` → `vector_stores.files.create(attributes=)`。
  `file` / `sha256` は未指定なら補完。検証は **OCI 呼び出しより前**（不正なら Files API を汚さない）。
- `jetuse_core/chat.py`: `_extract_citations()` に `source`（attributes）/ `text`（500 字）/ `chunk_id` を追加。
  既存 `{file_id, filename, score}` は温存。代表チャンクの選択は**丸め前スコア**で比較する。
  `file_search_tool()` で `tools[].filters` を載せる。
- ルート: `POST /api/rag/files` に `attributes`（JSON 文字列・省略可）、`/api/chat/stream` に `rag_filters`。
  未知キー等は 422（属性の自動補完値が長すぎる場合も 422 に正規化）。`vector_store` 以外の
  バックエンドおよびエージェントモードとの併用は 400（渡す口が無い経路で黙って無視しない）。
- ドキュメント: `specs/09-rag.md`（API 契約）/ `docs/verification/RAGM-01.md`。

### 実装判断（タスクの「切り詰めるか拒否するか」）

**拒否（422）を選んだ。** 切り詰めると `sha256`/`version` が別物になり、フィルタが静かに外れる。
それは SPIKE-M1 ①-b の「未知キーで 0 件」と同じ失敗の形（利用者が気付けない）なので入口で弾く。

## E2E（実 ADB + 実 OCI / 共有 loop ADB のタスク専用スキーマ `JETUSE_RAGM01` で隔離）

アプリ（`service.main`）を実 ADB・実 OCI につないだまま in-process で起動し、全経路を実 API で通した
（モックなし）。検証用 Vector Store は run 固有名 `jetuse-spike-ragm01-<tag>`。ADB は増やしていない。

| # | シナリオ | 結果 | 証跡 |
|---|---|---|---|
| 1 | 版違いの架空文書 2 件を属性付きで取り込み → 版フィルタ有無の対照 | PASS | `e2e/scenario-1.md` |
| 2 | 回答の citations にセル範囲まで載る（実レスポンス） | PASS | `e2e/scenario-2.md` |
| 否定 | 未知フィルタキー / `in` / 未知属性キー / 512 文字超 / バックエンド不一致 / エージェントモード / 子が null | PASS | `e2e/guard.md` |

- 対照の実測: フィルタ無し → 引用 v1.0 + v2.0（回答も両版を混ぜる）／`current_version='Y'` → v2.0 のみ。
- 引用の実値: `source = {file, version:"2.0", sheet:"API一覧", cells:"B18:F18", kind, current_version, sha256}`。
- 片付け: ファイル・Vector Store・登録簿行・検証スキーマ（`JETUSE_RAGM01`/`_Q`）を削除し不在を再照会で確認
  （`e2e/teardown.md`。DROP は `--receipt` の `USER_ID` 一致時のみ）。証跡は `redact_evidence.py` で伏字化。
- 実施しなかった範囲と理由: `e2e/SKIPPED.md`。

## テスト / lint

- `.venv/bin/pytest packages/api/tests` 442 passed（新規 `test_rag_metadata.py` 19 件 + 既存テストへ 16 件追加）
- `.venv/bin/ruff check packages/api` クリーン

## 人間ゲート（未実施）

- コミット / PR / push（未承認のため未実施）
- 環境の記録事項: `.env` の `COMPARTMENT_OCID` は親（`jetuse`）を指すが実リソースは子の `dev` にあり、
  `PROJECT_OCID` 未設定だと `ProjectResolutionError` になる。E2E は両者を明示して実行した。
  恒久対応（既定値の変更 or `GENAI_COMPARTMENT_OCID` の追加）は仕様判断のため未実施（`docs/verification/RAGM-01.md`）。

## review-1（PASS / blocker 0・major 3・minor 3。E2E adequacy=insufficient）への対応

判定は PASS だったが、Codex が E2E 証跡を insufficient とし、完了主張を支える部分に指摘が出たため
修正した（磨き込みではなく、主張と実装の食い違いの是正）。修正後に E2E を**最初から通しで再実行**した。

| 指摘 | 対応 |
|---|---|
| F-001(major) エージェント経路に `rag_filters` を渡す口が公開 API に無く、`agent_id` 経路では黙って無視される | 投機的だった `_build_agent_tools` へのフィルタ受け渡しを**削除**し、`agent`/`agent_id` との併用を **400 で拒否**。実 API の否定側（`guard.md`）に追加 |
| F-002(major) 補完する `file`（ファイル名）が 512 文字超だと `MetadataError` が 500 として漏れる | `_rag_call` で `MetadataError` を **422 に正規化**＋ルートテスト |
| F-006(major) teardown が全例外を「削除済み」と扱い、OCI Files の不在を確認していない | 不在判定を **`NotFoundError` 限定**にし、ファイル 1 件ずつ Files API で再照会。確認できなければ台帳を残して**非ゼロ終了** |
| F-003(minor) 複合フィルタの子の `null` が検証を素通りする | 最上位以外の `None` を拒否＋単体/実 API の否定テスト |
| F-004(minor) 巨大 int で `math.isfinite` が OverflowError（500） | `int` はそのまま通し、有限性判定は `float` のみ |
| F-005(minor) 丸め後スコアで比較し、最上位でないチャンクを引用しうる | 丸め前スコアで比較（0.8504 vs 0.8501 の回帰テスト） |

## review-2: PASS（完了ゲート到達）

blocker 0 / E2E adequacy=sufficient。停止規律に従い、PASS の下に残る非 blocker は**修正せず
residual として列挙**する（磨き込みの反復に入らない）。

| sev | file:line | 指摘 | 扱い |
|---|---|---|---|
| major | `jetuse_core/rag_metadata.py:49` | 任意精度 int を無制限に許可（`10**400` も通す）。SDK の attributes 型は `str/float/bool` で、上流が拒否する | residual（数値の上下限は仕様判断。実運用の属性で起きない入力） |
| major | `jetuse_core/rag.py:276` | `vector_stores.files.create` が NotFound 以外で失敗すると OCI File と原本が孤児になる（DB 行が無く辿れない） | residual（本タスク以前からの経路。属性追加は失敗理由が1つ増えただけ） |
| major | `service/routes/chat.py:188` | `rag_filters` の組み合わせ検証がモデル可用性判定・guard より後ろで、400 が保証されない経路がある | residual（どちらの経路でも検索は実行されない＝黙って効かないことは起きない） |
| minor | `jetuse_core/rag_metadata.py:129` | 比較フィルタの値に空文字を許す（取り込み側は空を保存しないので必ず 0 件） | residual |

## 完了ゲート

- review_verdict = PASS（review-2・Codex 判定）
- `.venv/bin/pytest packages/api/tests` 442 passed / `.venv/bin/ruff check packages/api` クリーン
- 実環境 E2E: 正常系 2 本 + 否定系 7 本すべて PASS・証跡込みレビューで adequacy=sufficient
- タスクパケット: `runs/2026-07-30T0025_RAGM-01/report/RAGM-01.html`（report パイプで配置）
