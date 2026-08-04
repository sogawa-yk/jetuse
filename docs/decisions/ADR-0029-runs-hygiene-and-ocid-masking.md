# ADR-0029: runs/ の hygiene と実 OCID のマスク強制

- Status: Proposed（**承認は人間ゲート**）
- Date: 2026-08-04
- 関連: ADR-0018（報告パイプ）/ ADR-0028（4ブランチ体制）

## Context

このリポジトリは **public**。2026-08-04 に `runs/` を棚卸ししたところ、次の状態だった。

| 項目 | 実測 |
|---|---|
| `runs/` の総量 | 155.7 MB / 1,451 ファイル |
| うち追跡済み | 817 ファイル |
| `review-*.{raw.txt,payload.txt,input.diff}` | 123.2 MB（全体の 79%） |
| `.git` | 94 MB |

さらに、`main` と `dev` で **`.gitignore` の方針が真逆**だった。`main` は `runs/` をコミットし、`dev` は「環境依存の識別子を含みうる — コミットしない」として `runs/` を丸ごと ignore していた（ただし ignore 前に入った516ファイルは追跡されたまま）。`ops/sync-main-to-dev.sh` はこれを「既知の構造的乖離」として列挙し、同期のたびに手で解決する運用が固定化していた。

### 秘匿値のスキャン結果

| 種別 | 結果 |
|---|---|
| 秘密鍵（PRIVATE KEY） | 0件 |
| PAR（事前認証リクエスト）URL | 0件 |
| 実パスワード・トークン | 0件（すべて `change-me` / `<redacted>` 等のプレースホルダ） |
| 実顧客名 | 0件（`架空商事株式会社` のみ＝架空） |
| **実 OCID（完全長）** | 出現は約6,700回だが大半は `ocid1.tenancy.oc1..MASKED` にマスク済み。取りこぼしは**追跡対象で10個**（うち `tenancy` 1・`compartment` 3） |

手動のマスク規律はよく効いている。しかし取りこぼしは実際に起きており、しかも次の2点が分かった。

1. **除外ルールでは防げない。** 実 OCID は「除外する中間生成物」だけでなく、証跡として残す側（`runs/.../e2e/DONE.md` / `scenario1-apply.txt` / `scenario3-summary.txt`）にも入っていた。
2. **`runs/` の外にもあった。** `docs/archive/spikes/spike14b_aisdk.mjs` に **compartment の実値**が公開されていた。
3. **当初の見積もりが誤っていた。** `runs/` だけを対象にした最初のスキャンで「実 OCID は9個、`tenancy` / `compartment` は公開されていない」と結論したが、追跡対象全体を機械で洗うと **10個あり、`tenancy` 1件・`compartment` 3件が含まれていた**。手作業のスキャンは取りこぼす —— これ自体が検査を機械化すべき根拠になった。

## Decision

1. **codex レビューの中間生成物とターン差分を追跡対象から外す。** `.gitignore` に追加し、既追跡の343ファイル（約50.7 MB）は `git rm --cached` する。
   ```
   runs/**/reviews/*.raw.txt
   runs/**/reviews/*.payload.txt
   runs/**/reviews/*.input.diff
   runs/**/diffs/turn-*.diff
   ```
   判定と結論は `review-<n>.json` / `STATE.md` / `summary.md` に残るため、証跡の追跡性は落ちない。ファイル自体はローカルに残る。
2. **`ops/check-no-real-ocid.sh` で実 OCID の新規混入を止める。** 完全長 OCID を検出したら落とす。検査対象は index（ステージ済み）＋追跡ファイルの作業ツリー内容（`--all` で未追跡も）。`make lint` と、**専用ワークフロー `.github/workflows/no-real-ocid.yml`**（`ci.yml` と分離し全ブランチの push で走る）に載せる。`ci.yml` は `on.push.branches` を長期4ブランチに限定しているため、そこに同居させると feature branch への push が素通りする。
3. **`docs/archive/spikes/spike14b_aisdk.mjs` の compartment / generativeaiproject の実値をマスクする。** 退避済みスクリプトであり実値に価値が無い。環境変数から読む形にした。
4. **既に公開済みの `ormjob` OCID 2件を受容 residual とし、`ops/allowed-public-ocids.txt` に記録する。** `tenancy` / `compartment` を含むその他は中間生成物の追跡解除で追跡対象から消える。 認証情報ではなく、tenancy / compartment でもない。`git filter-repo` による履歴書き換えは public リポジトリの fork / clone を壊す代償に見合わない。
5. **`tenancy` / `compartment` の実値は受容しない。** 危険度が変わる（サポート詐称、cross-tenancy ポリシーの標的化）ため必ずマスクする。**この禁止はコードで強制する** —— allowlist に書かれていても（コメント行に紛れ込ませても）落とす。運用規律に頼らない。
6. **`runs/` の方針を4ブランチで統一する。** Internal 側の `runs/` 丸ごと除外は廃止し、上記の中間生成物除外に一本化する。

## Consequences

- `runs/` の増加要因のうち 123 MB 分が止まる。`.git`（94 MB）は縮まない —— 既に push 済みの内容は履歴に残る。これは**今後の増加を止める措置**である。
- 実 OCID の混入は規律ではなく機械が止める。`ops/check-no-real-ocid.sh` は `--all` で未追跡ファイルも検査できる。
- `.gitignore` の構造的乖離が解消し、sync の手動解決が1つ減る。

### 受容する residual

- **公開済みの `ormjob` OCID 2件。** `runs/2026-07-13T0730_PORT-01/e2e/` に残る。ORM ジョブの存在と実行時刻が分かるだけで、IAM 認証なしには参照できない。
- **プライベート IP（VCN CIDR `10.0.0.x` / `10.1.x`）の露出。** 到達性が無く実害は軽微。マスク検査の対象には含めていない。
- **allowlist への追加は人間レビューでしか止められない。** CI は「正当な受容」と「検査を通すための追記」を区別できない（同じコミットで OCID と受容行を足せば通る）。`tenancy` / `compartment` だけはコードで拒否しているが、それ以外は PR レビューが唯一の関門になる。追加行には受容できる理由を行末コメントで書く規約にした。
- **CI は検出であって防止ではない。** public リポジトリでは push した時点で内容が GitHub 上に出るため、workflow が落ちても漏えいは成立している。**防止するのはローカルの `make lint`**。pre-push hook は用意していない（別途検討）。
- **正規表現は近似であり全形式の網羅を保証しない。** OCID は `ocid1.<type>.<realm>[.<region>][.<future-use>].<unique-id>` で、region / future-use の有無も unique-id の長さもリソース依存。中間セグメントを緩く受け閾値を30文字にしたが、条件外の形式は見逃しうる。**この検査は最後の関門ではなく防御の一段**であり、マスクの規律そのものを置き換えるものではない。

## Alternatives considered

**`runs/` を丸ごと除外する（Internal 側の旧方針に統一）** — 混入経路は確実に断てるが、CLAUDE.md の「不変の実行履歴は `runs/<run-id>/`」がローカル限定になり、worktree 撤去で失われる。証跡主義と両立しない。

**中間生成物の除外のみ（検査なし）** — 肥大は止まるが、証跡側のファイル経由の混入は止まらない。実際 `e2e/DONE.md` に実 OCID が入っていた。

**`git filter-repo` で履歴から除去** — 厳密に消せる唯一の方法だが、public リポジトリの fork / clone と open PR を壊す。受容する OCID の危険度に見合わない。
