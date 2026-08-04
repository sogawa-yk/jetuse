# 未実施と、その理由（この時点のスナップショット）

goal は4件すべての E2E 合格。**この run は4件を順に進める**。各 PR が到達した時点の状態を下表に保つ。

| # | 作業 | 状態 | 証跡 |
|---|---|---|---|
| 1 | `feat/TOOL-01` の取りこぼし回収 | **完了**（PR #136 マージ済み・E2E 合格） | `scenario-1-tool01-guard.md` |
| 2 | branch protection（4ブランチ・bypass なし） | **ruleset 適用済み**。2a(direct push 拒否)・2b(dist ガード3経路) 合格。**2c(PR 経由で通ること) は本 PR のマージで確定**するため、この時点では未確定 | `scenario-2-branch-protection.md` |
| 3 | Public リリース `public-v0.1.0` | **リリース完了・E2E 10項目合格**。タグ付けはこの後 | `scenario-3-public-release.md` |
| 4 | Internal リリース `internal-v0.1.0` | **進行中**。配備が `podman: command not found` で止まったため、先にコンテナエンジンのフォールバックを入れた（E2E 合格）。リリース点の統合 E2E はこの後 | `scenario-4-container-engine.md` |

## なぜ分けて出すか

2〜4 は順序依存がある。protection を先に入れることで、以後の release PR が
「protection 下で実際に通るか」を実運用で確かめられる。1 はそれらと独立で、
`3caef09` が `public-dev` にも `main` にも入っていない取りこぼし状態を早く解消したかった。

**進捗は上表が正**（PR ごとに更新する）。この段落は履歴として残すが、状態の判定には使わない。

## 証跡から外したもの

`reviews/review-4.json` はコミットしない（ローカルには残る）。指摘文が
`https://postman-echo.com/post` を引用しており、証跡としてリポジトリへ新規に持ち込むのを避けた。

なおこの値自体は既に `packages/api/jetuse_core/http_tools.py` を含む7ファイルで追跡済みの
**公開テスト用エンドポイント**であり、環境依存の秘匿値ではない。それでも「新しく足さない」側に倒した。
review-4 の指摘内容（SKIPPED.md の自己矛盾・dist ガードの失敗経路未検証）は本 PR で対応済み。
