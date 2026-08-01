# 完了ゲートの実環境 E2E は未実施（理由あり）

`tasks/MEM-01.md` の「E2E シナリオ」（JetUse の API を通した会話 A→B の想起・subject 分離・
会話削除）は **まだ実施していない**。実施できない理由は 1 つだけである:

**アプリ側の実装がまだ 1 行も無い。** MEM-01 は着手前の実機調査で当初の想定が覆り
（`docs/verification/MEM-01.md`）、方式が変わるため ADR-0022 を Proposed で起票して
**人間の承認ゲートで停止している**。承認前に実装を進めないという指示に従った結果であり、
技術的な障害ではない。

## いま実施済みなのは何か

方式判断に必要な範囲の**実機検証**は済んでいる（アプリ経由ではなく SDK 直叩き）。
実行ログ: `research-run.log` / 構造化結果: `../research/probe3-sdk-e2e.json`
再現: `spikes/mem01/probe_sdk.py`（期待値を assert し、外れたら非ゼロ終了する）

| 確認 | 結果 |
|---|---|
| 抽出（会話 A の 4 発話） | `MEM01_MEMORY` 3 行 / `MEM01_RECORD_CHUNKS` 7 行 |
| **会話をまたいだ想起** | 会話 B が存在する状態で user スコープ検索 → `User prefers reports in bullet points` が返り、その `thread_id` が **会話 A** であることまで確認 |
| **subject 分離** | 別 user・`exact_user_match=True` → **0 件** |
| **削除の伝播** | スレッド削除で `MEM01_MEMORY` 4→0 / `MEM01_MESSAGE` 5→0 / `MEM01_RECORD_CHUNKS` 9→0、削除後の検索も 0 件 |

検証環境: 共有 loop ADB（`jetuse-loop-adb` / Oracle AI Database 26ai 23.26.3.1.0）の
run 固有スキーマ `JETUSE_MEM01_EFC2EA`。ADB は増やしていない。認証は `config_file`。

## 承認後に残る E2E（このファイルを置き換える）

1. ADB バックエンドを選んだ状態で、JetUse の会話 A の情報が会話 B で想起される。
2. 別 subject では想起されない。
3. 会話削除（`DELETE /api/conversations/{id}`）後に該当記憶が想起されない。
