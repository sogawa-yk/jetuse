"""semver 比較(「最新版」選択のための最小実装)。

`get` が version 省略時に最新版を返すため、semver.org の優先順位規則に沿って版を比較する。
manifest の version は PLG-01 で semver パターン検証済み(MAJOR.MINOR.PATCH[-prerelease][+build])。
ここでは precedence のみを扱う: build メタデータは優先順位に影響しない /
prerelease は対応する正式版より低い / prerelease 識別子は数値は数値比較・非数値は辞書順、
数値は非数値より低い。
"""

from __future__ import annotations


def _prerelease_key(pre: str) -> tuple:
    """prerelease 文字列を比較キーへ。形は常に (release_flag, identifiers) で型を揃える。

    - 正式版(prerelease 無し): (1, ()) — 同 MAJOR.MINOR.PATCH の全 prerelease より高い。
    - prerelease: (0, (id1, id2, ...)) — 各 id は (kind, num, text) の 3 要素で比較可能に揃える。
      数値識別子は kind=0(数値比較)・非数値は kind=1(辞書順)で、数値 < 非数値(semver 規則)。
    """
    if pre == "":
        return (1, ())
    ids: list[tuple] = []
    for ident in pre.split("."):
        if ident.isdigit():
            ids.append((0, int(ident), ""))
        else:
            ids.append((1, 0, ident))
    return (0, tuple(ids))


def precedence_key(version: str) -> tuple:
    """semver の優先順位比較キーを返す。大きいほど新しい。"""
    core, _, _build = version.partition("+")  # build は precedence に影響しない。
    core_part, _, pre = core.partition("-")
    major, minor, patch = (int(x) for x in core_part.split("."))
    return (major, minor, patch, _prerelease_key(pre))


def latest(versions: list[str]) -> str:
    """版リストから最新(最大 precedence)を返す。空リストは ValueError。"""
    if not versions:
        raise ValueError("versions が空")
    return max(versions, key=precedence_key)
