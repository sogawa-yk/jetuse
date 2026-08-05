---
id: ER-0012
title: 公開リポジトリに実 OCID が残っている
status: done
size: S
source: 気づき
created: 2026-08-04
ticket:
pr: 134
---

## 【2026-08-04 完了】

`runs/` の中間生成物を追跡から外し、`docs/archive/` の実値をマスクした（PR #134 / ADR-0029）。
`ops/check-no-real-ocid.sh` で新規混入を機械的に止める仕組みも入れた。
`tenancy` / `compartment` は allowlist に書いても拒否する。

残った `ormjob` 2件は受容 residual として `ops/allowed-public-ocids.txt` に記録。

**確認できる証跡**:

| 主張 | どこで確認できるか |
|---|---|
| 追跡対象から実 OCID が消えた | `make lint` の `ops/check-no-real-ocid.sh --all` が
`[ocid] OK（検出はすべて ops/allowed-public-ocids.txt で受容済み）` を返す。CI では
専用ワークフロー `.github/workflows/no-real-ocid.yml` が全ブランチの push で実行 |
| `tenancy` / `compartment` は allowlist に書いても拒否 | `packages/api/tests/test_check_no_real_ocid.py` の
`test_tenancy_cannot_be_allowlisted` / `test_compartment_cannot_be_allowlisted` /
`test_commented_out_tenancy_in_allowlist_is_rejected`（計14ケース） |
| 追跡解除の実施 | PR #134（343ファイル・約50.7MB を `git rm --cached`） |
| 判断の記録 | ADR-0029（受容した residual と、正規表現が近似である旨も明記） |

**この ER の差分単体では検証できない。** 完了の裏づけは PR #134 とその証跡
（`runs/2026-08-04T1832_FINISH-4/`）にあり、本ファイルはそこへの索引にすぎない。
現在も成立していることは `make lint` と CI（`no-real-ocid.yml`）が毎回確かめている。

## ひとことで

公開リポジトリの過去の資産に、実際のリソース識別子が入ったままになっている。

## 何が起きているか

`main`（公開）に**実 OCID** が入っている。2026-08-04 の走査で確認:

- `docs/archive/spikes/` の検証用スクリプト
- 過去の実行記録（`runs/*/reviews/review-*.raw.txt`）4 件

## 根拠

PORT-03 の再開時に、証跡の秘匿値スキャンで検出した。
**その作業由来ではなく、以前から入っていたもの**。

OCID 単体で直ちに悪用できるわけではないが、
**どのテナンシのどのリソースが存在するかが読める**。
リポジトリの規約は「テナンシ / コンパートメント OCID をコミットしない」と定めている。

## どう直すか

- 現在のファイルから伏せる（種別は残し、値だけ `<ocid:project>` 等に）
- **履歴に残る点をどうするか**は判断が要る（書き換えるか、現状のまま前へ進むか）
- 再発防止として、コミット前の走査を仕組みに入れる
  （`ops/check-infra.sh` と同じ位置づけ。**顧客名の走査も一緒にできる**）

## やらない場合の代償

公開リポジトリを見た人が、テナンシの構成を推測できる。
実害は限定的だが、**規約と実態がずれたままになる**。

## 関連

ER-0013（ページ送りトークンの URL エンコード）と同じく、PORT-03 の再開で出たもの。
