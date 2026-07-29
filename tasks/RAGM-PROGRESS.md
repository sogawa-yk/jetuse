# RAG メタデータ / Agent Memory 進捗キュー（ADR-0019 承認により起票）

`docs/decisions/ADR-0019-rag-metadata-backend.md`（**Accepted** 2026-07-29）の実装キュー。
決定は「**性格の違う 2 バックエンドを選べるようにし、能力差をアプリから見えるようにする**」。
**base=`main`**（共有物）。push / PR / apply / IAM は人間ゲート。

status: `todo` | `in_progress` | `blocked` | `done`

| 順 | タスク | 依存 | 人間ゲート | status |
|---|---|---|---|---|
| 1 | [RAGM-01 マネージド Vector Store に属性・構造化出典・版フィルタ](RAGM-01.md) | — | コミット | todo |
| 2 | [RAGM-02 Oracle AI Database バックエンド（`adb`）](RAGM-02.md) | — | コミット | todo |
| 3 | [RAGM-03 能力差を API とフロントから見えるように](RAGM-03.md) | RAGM-01, RAGM-02 | コミット | todo |
| 4 | [MEM-01 Oracle AI Database を Agent Memory バックエンドに](MEM-01.md) | RAGM-02 | コミット | todo |

> 第1波 = RAGM-01 ∥ RAGM-02（相互独立・並列可）。第2波 = RAGM-03 → MEM-01。
> RAGM-03 を先に入れない（画面に「使える」と出ているのに動かない状態を作らないため）。

## SPIKE-M1 から引き継いだ宿題（起票済み扱い・優先度順）

| # | 内容 | どこで効くか |
|---|---|---|
| 1 | `ops/setup-select-ai.py` / `ops/setup-dev-schema.py` の `~/.oci/config` パーサ修正 | 複数プロファイル環境で credential が別テナンシになり DBMS_CLOUD の全呼び出しが `ORA-20404`。**踏むと原因究明に時間を溶かす**（SPIKE-M1 で実際に溶かした） |
| 2 | `adb` バックエンドのスケール検証（数万〜数十万チャンク） | RAGM-02 の前提。10 行ではオプティマイザが索引を使わず、速度も再現率も測れていない |
| 3 | VPD / Data Redaction がベクタ検索にも効くことの実証 | 「行レベル制御ができる」と顧客に言う前に必要。SPIKE-M1 では未実証 |
| 4 | Chicago（us-chicago-1）での ①③ 再確認 | JetUse 本体の提供リージョン。SPIKE-M1 は大阪のみ |

## 参照

- 実測レポート: `docs/verification/SPIKE-M1.md`
- 比較資料（プリセールス転用可）: `docs/comparison/rag-metadata-backends.md`
- 検証スクリプト: `spikes/spike_m1/`（片付け込み。`teardown.py --yes`）
