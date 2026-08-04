# E2E-1: TOOL-01 の片付けガード（`resolve_dev_compartment`）

対象: `spikes/ragm02/common.py`（cherry-pick `3caef09` → `291acf8`）
環境: 実 OCI / `jetuse:dev` コンパートメント / `AUTH_MODE=config_file` / ap-osaka-1
モック不使用。実際に `oci.identity.IdentityClient` でコンパートメントを引いている。

## なぜ必要か

TOOL-01 の片付けが「一意に定まらない」で中止した実害の修正。リポジトリ内に
`COMPARTMENT_OCID` の解釈が2通りあり、`ops/_adb.assert_target()` は「そのもの」を、
`resolve_dev_compartment()` は「親」を前提にしていた。両対応にしつつ
**名前が dev であることの要求（fail-closed）は維持**したので、3分岐すべてを実機で確認する。

## 結果

| ケース | 与えた `COMPARTMENT_OCID` | 期待 | 実測 | 判定 |
|---|---|---|---|---|
| A | `jetuse:dev`（現行の正） | 自身を返す | rc=0 / dev に一致 | PASS |
| B | `jetuse`（親・旧解釈の後方互換） | 直下の dev を返す | rc=0 / dev に一致 | PASS |
| C | `jetuse:registry`（dev でない） | 中止する | rc=1 / 「COMPARTMENT_OCID 自身が dev でもなく、その直下の dev も一意に定まらない。中止。」 | PASS |

C が fail-closed を保っていることが重要。名前検査を外すと、承認外のコンパートメントを
片付け対象にしてしまう。

## 再現

`.env` の `COMPARTMENT_OCID` を各値に差し替えて以下を実行（実行後は元に戻す）:

```
.venv/bin/python -c "import sys; sys.path.insert(0,'spikes'); \
  from ragm02.common import resolve_dev_compartment; print(resolve_dev_compartment())"
```
