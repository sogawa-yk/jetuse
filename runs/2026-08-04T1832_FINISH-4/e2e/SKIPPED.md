# 未実施と、その理由（この時点のスナップショット）

goal は4件すべての E2E 合格。**この PR は 1件目のみ**を含む。残り3件は同じ run で継続する。

| # | 作業 | 状態 | 証跡 |
|---|---|---|---|
| 1 | `feat/TOOL-01` の取りこぼし回収 | **完了・E2E 合格** | `scenario-1-tool01-guard.md` |
| 2 | branch protection（4ブランチ・bot bypass） | 未着手 | — |
| 3 | Public リリース `public-v0.1.0` | 未着手 | — |
| 4 | Internal リリース `internal-v0.1.0` | 未着手 | — |

## なぜ分けて出すか

2〜4 は互いに順序依存がある（protection を入れてから release PR を通すことで、
protection 自体が機能するかを実運用で確かめられる）。一方 1 はそれらと独立で、
`3caef09` が `public-dev` にも `main` にも入っていない取りこぼし状態が続くのは避けたい。

**1 単体の完了条件は満たしている**: 実 OCI で3分岐（自身が dev / 親 / dev でない）を確認済み。
