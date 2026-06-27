# ステージ4 タスク索引（コンテナデプロイ＋マーケット拡張＋既存資産オンボード）

親計画: [`../docs/enhance/202607-demo-platform-plan.md`](../docs/enhance/202607-demo-platform-plan.md) §3/§6/§7/§9/§10。
方式比較: [`../docs/comparison/marketplace-plugin.md`](../docs/comparison/marketplace-plugin.md) §2-B（μService）/ §3（既存資産オンボード）。
各タスクは `LOOP_TASK=<id> GOAL="..." claude` で1本ずつループ実行する（[`../LOOP.md`](../LOOP.md)）。進捗キュー: [`STAGE4-PROGRESS.md`](STAGE4-PROGRESS.md)。

## 目的（配備・流通・既存資産）
S1+S2 で「組める」、S3 で「データに繋がる」体験が成立した。S4 は ①生成デモを **L3 コンテナ**として配備し
Platform API でテナントデータに繋ぐ（DEP）、②素材（sample-app/connector）を**マーケット流通**させ本番レジストリ
（μService）へ昇格する（MKT）、③既存の高機能資産（伝ぴょん・No.1-RAG・SQL-Assist）を**オンボード**する（ASSET）。

## タスク
| ID | 内容 | 依存 | area |
|---|---|---|---|
| DEP-01 | 生成デモのコンテナ配備（L3, Phase 9 基盤再利用）＋IaC plan＋ADR-0015 | ステージ3 | api(+infra) |
| DEP-02 | Platform API 注入（D3 解。ベースURL＋短期トークンの実行時バインド） | DEP-01 | api(+infra) |
| MKT-01 | sample-app/connector のマーケット流通（PLG-04/05 を両 kind に拡張） | ステージ1・CON-01・SBA-01 | api |
| MKT-02 | 中央レジストリ μService（ADB・署名・版・評価, comparison §2-B） | MKT-01 | api |
| ASSET-01 | 既存資産オンボード（伝ぴょん=外部連携／No.1-RAG・SQL-Assist=MCP化） | DEP-01・CON-01・MKT-01 | api |

## 推奨実行順
（DEP-01 と MKT-01 を並行）→（DEP-02 と MKT-02 を並行）→ ASSET-01。

## ⚠️ apply/billing 依存が濃い
L3 実配備・実レジストリ流通・μService 実デプロイ・既存資産接続は **terraform apply / 課金 / 既存資産接続**を要し、
これらは hard_gate（自走中は越えない）。自走では **設計＋IaC plan＋コード＋mock/loop-ADB E2E＝PASS** まで進め、
**実 apply を要する E2E は SKIPPED に明記**してステージ報告で一括提示する。

## 人間ゲート（停止して承認を待つ）
コミット/PR/push（全タスク）／**ADR-0015 承認**（DEP-01）／**terraform apply・課金**（DEP-01/02・MKT-01/02）／
**既存資産接続・SSO 実設定（濃い）**（ASSET-01）。

## 起票予定 ADR
- **ADR-0015（DEP-01）**: L3 ホスト型/既存資産オンボード（実行基盤・SSO・データ注入）。計画 §11 で予約済。
