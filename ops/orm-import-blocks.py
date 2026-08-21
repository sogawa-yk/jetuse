#!/usr/bin/env python3
"""ローカル state から Terraform の `import` ブロックを生成する（ADR-0031 の ORM 移行用）。

**なぜ要るか**: ORM のスタックには state を持ち込む API が無い（`get-stack-tf-state` は読むだけ）。
既存環境を作り直さずに ORM へ移すには、初回 apply の config に `import` ブロックを入れて
既存 OCID を引き取らせるしかない。

**出力はコミットしない。** 実 OCID が並ぶため（CLAUDE.md の秘匿値規約）。移行のたびに生成する。

使い方:
    python3 ops/orm-import-blocks.py infra/terraform/environments/dev/terraform.tfstate > /tmp/imports.tf
"""

from __future__ import annotations

import json
import pathlib
import sys

# `attributes.id` をそのまま import ID に使えない型。
#
# 素朴に `id` を使うと **plan がエラーで止まる**（2026-08-07 の実測:
# 「can not marshal to path in request for field NetworkSecurityGroupId」）。
# NSG ルールの `id` は親 NSG 内でのみ一意な短いハッシュ(例 `5DF54C`)で、
# 単体では資源を指せない。ログも同様に親 LogGroup が要る。
COMPOSITE = {
    "oci_core_network_security_group_security_rule":
        lambda a: f"networkSecurityGroups/{a['network_security_group_id']}/securityRules/{a['id']}",
    "oci_logging_log":
        lambda a: f"logGroupId/{a['log_group_id']}/logId/{a['id']}",
}

# import しない型と、その理由。
SKIP = {
    # 作成は no-op。**destroy provisioner は走らない**（バケットを空にするのは destroy 時だけで、
    # 旧 state を destroy しない限り発火しない）。import すると output の再計算で差分が出る。
    "terraform_data": "新規作成させる（作成は no-op・destroy provisioner は発火しない）",
    # import せず、代わりに `spa_par_expiry` を明示して count=0 にする。
    # 空のままだと新しい基準時刻で作られ、PAR の失効日がずれる。
    "time_offset": "spa_par_expiry を明示して count=0 にする（失効日を据え置く）",
    # **import できない。** PAR の `access_uri` は**作成時にしか返らない**（GET では返らない）ため、
    # import すると null になり、api-gateway モジュールの
    # `"${local.os_host}${var.spa_par_access_uri}index.html"` が
    # 「Invalid template interpolation value: The expression result is null」で落ちる
    # （2026-08-07 の実測。api_gateway の deployment とログ2本まで巻き込んで plan が止まった）。
    # 作り直せば access_uri は既知になる。**公開 URL は API Gateway 側なので変わらない**
    # （PAR は GW が内部で叩く先）。旧 PAR はバケットに残るので、移行後に手で消す。
    "oci_objectstorage_preauthrequest": "作り直す（access_uri は作成時にしか返らず import できない）",
}

HEADER = """\
# ローカル state の既存資源を ORM スタックの state へ取り込む（ADR-0031）。
# `ops/orm-import-blocks.py` が生成。**コミットしない**（実 OCID を含む）。
#
# 使い方: この 1 ファイルを config に混ぜて ORM の plan → apply。取り込みが済んだら
#         このファイルだけ外して再 plan し、"No changes" になれば移行完了。
#
# 生成対象外:
{skips}
"""


def blocks(state: dict) -> tuple[list[str], list[str]]:
    out: list[str] = []
    skipped: list[str] = []
    for r in state.get("resources", []):
        if r.get("mode") != "managed":
            continue
        rtype = r["type"]
        if rtype in SKIP:
            skipped.append(rtype)
            continue
        mod = r.get("module", "")
        for inst in r.get("instances", []):
            attrs = inst.get("attributes") or {}
            idx = inst.get("index_key")
            addr = f'{mod + "." if mod else ""}{rtype}.{r["name"]}'
            if idx is not None:
                # **引用符やバックスラッシュを含むキーがありうる。** 素朴に埋め込むと
                # 壊れた Terraform address を生成する。JSON のエスケープ規則は
                # HCL の文字列リテラルと同じなので json.dumps に任せる。
                addr += f"[{json.dumps(idx)}]" if isinstance(idx, str) else f"[{idx}]"
            try:
                rid = COMPOSITE[rtype](attrs) if rtype in COMPOSITE else attrs["id"]
            except KeyError as e:
                raise SystemExit(f"{addr}: import ID を作れない（属性 {e} が無い）") from e
            out += ["import {", f"  to = {addr}", f'  id = "{rid}"', "}", ""]
    return out, sorted(set(skipped))


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {sys.argv[0]} <terraform.tfstate>")
    path = pathlib.Path(sys.argv[1])
    if not path.exists():
        raise SystemExit(f"state が見つからない: {path}")
    body, skipped = blocks(json.loads(path.read_text()))
    skips = "\n".join(f"#   - {t}: {SKIP[t]}" for t in skipped) or "#   （なし）"
    sys.stdout.write(HEADER.format(skips=skips) + "\n" + "\n".join(body))
    n = sum(1 for line in body if line == "import {")
    print(f"# 合計 {n} 個", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
