# SQL-02 検証レポート: NL2SQLチャットフロー

日付: 2026-06-11
仕様: specs/10-dbchat.md [SQL-02]
状態: **実機E2E完了**（イメージ 0.10.1→0.11.0、SPA同時デプロイ）。UIの操作感レビューはユーザー確認待ち

## 実装

- `POST /api/chat/nl2sql`（SSE+keepalive。生成は実測30秒前後のため/api/chat配下=300sルートに配置）: SemanticStore `generateSqlFromNl` をリソースプリンシパル署名で呼び出し（同期/非同期ジョブ両対応）
- `POST /api/dbchat/execute`: **多層ガード** — ①JETUSE_QUERY接続（CREATE SESSIONのみの読取専用ユーザー） ②コメント/セミコロン除去後にSELECT/WITH限定判定+更新系キーワード拒否 ③200行上限（201行目で打ち切り検出） ④call_timeout 30秒 ⑤セル300字切り詰め。SQL構文エラーは400で本文返却（DB停止系DPY-は503ハンドラへ）
- UI `/dbchat`: 質問 → 生成中プログレス（経過秒+目安表示）→ **生成SQLをエディタ表示（ユーザーが確認・編集してから実行）** → 結果テーブル（固定ヘッダ・打ち切りバッジ・コピー）
- CI環境変数追加: `SEMSTORE_OCID` / `ADB_QUERY_PASSWORD`

## 実機E2E（API GW経由、イメージ0.11.0）

| ケース | 結果 |
|---|---|
| 「2001年の販売チャネル別の売上合計」→ 生成 | JOIN付きの正しいSQL（SALES×TIMES×CHANNELS） |
| 生成SQLを実行 | **実データ正答**: Direct Sales 13,388,435.36 / Partners 8,038,529.96 / Internet 6,709,496.66（3行） |
| `DROP TABLE sh.sales` | 400拒否 |
| `UPDATE sh.sales SET ...` | 400「SELECT文のみ実行できます」 |

- pytest 43件（ガード網羅: コメント偽装DROP・複文・CTE後の更新文など）/ ruff / web lint+build クリーン

## 残作業（Phase 5）

- SQL-03: 結果の自動グラフ化
- SQL-04: Select AI直接実行モード + 比較ドキュメント（docs/comparison/nl2sql-backends.md）
- チェックポイント③: 顧客デモ品質のユーザーレビュー（10問正答率の定点指標はSQL-01で再確立済み: 10/10）

## 追補（SQL-02b、ユーザーフィードバック対応）

「テーブル内容が事前にわからないと質問できない」→ `/dbchat` 上部に**「質問できるデータ」パネル**を追加: `GET /api/dbchat/schema`（ディクショナリから実取得+日本語説明のキュレーション補完、行数付き、クリックで列展開）+ **サンプル質問チップ**（クリックで入力欄へ）。実機確認: SH 9テーブル・行数・日本語説明返却OK（イメージ0.11.1）。
