# 実 OCI への E2E は対象外（技術的理由と代替検証）

## なぜ OCI 上で実行しないか

この差分が変えるのは `ops/stalled.py` と `ops/er.py` — **ローカルで動くレポート生成器**だけ。

- 配備物に一切含まれない。`packages/api/Containerfile` は `packages/api/{jetuse_core,service,fn}`
  しか COPY せず、`ops/` はイメージに入らない
- `infra/terraform/` からも参照されない
- API エンドポイント・DB スキーマ・SPA のいずれも変更しない

したがって `jetuse:dev` へ配備しても**この差分が動く経路が存在しない**。
デプロイして疎通を確認しても、それはこの変更を検証したことにならない。

判定材料は `git rev-list` / `gh pr list` / ローカルの `origin/*` ref であり、
検証すべきは「**実際のリポジトリ状態から正しい結論を出すか**」である。

## 代わりに実施した検証

| | 内容 | 記録 |
|---|---|---|
| シナリオ1 | 単体 54 件（検出 43 + レポート表示 11）（使い捨て git リポジトリを作り、実リポジトリ状態に依存せず検査） | `01_pytest_release_lag.txt` |
| シナリオ2 | **実リポジトリ**で public / internal の両ペアを検出。`git rev-list --count` の実測値と一致 | `02_real_repo_scan.txt` |
| シナリオ3 | release PR 検出の3分岐（別 base の PR / 正しい release PR / `gh` 取得失敗） | `03_release_pr_detection.txt` |
| シナリオ4 | **オフライン / `gh` 未導入**でも例外を出さず `None` / `False` に落ちる | `04_offline_resilience.txt` |
| シナリオ5 | **判定できない状態を「問題なし」と言わない**（fetch 失敗・shallow・ref 欠落） | `05_cannot_tell.txt` |
| シナリオ6 | `git fetch` が**明示 refspec**で4本を更新（設定の refspec に影響されない） | `06_explicit_refspec.txt` |

シナリオ2で `stalled.py` が出した 16 / 10 commit は、同じファイル内に併記した
`git rev-list --count origin/main..origin/public-dev`（=16）と
`git rev-list --count origin/internal-stable..origin/internal-dev`（=10）に一致する。
**道具の出力を道具自身で確かめていない。**

シナリオ3の「別 base の PR」は **実在した #155**（`head=public-dev` / `base=internal-dev` の
同期 PR）の形をそのまま使っている。head だけで判定していた初版では、これがあるだけで
`main` への未リリースが要注意一覧から消えていた（review-1 major の指摘）。

## リポジトリ全体

- `make lint` OK（`[base]` / `[ocid]` とも通過）
- `make test` 976 passed / coverage 73.6%（`ops/er.py` の変更を含む全体）
