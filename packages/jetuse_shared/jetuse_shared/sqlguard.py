"""SQLサニタイズ(SELECT/WITH限定の多層ガード)。API/コンテナ共有 — jetuse_shared。

元実装: `jetuse_core/nl2sql.py::sanitize_sql`(SqlRejectedError) と
`agent-containers/agent_db.py::_sanitize`(ValueError)を一本化。

例外は jetuse_shared 固有の `SqlRejectedError` を送出する(ValueError サブクラスなので
従来 ValueError を catch していた呼び出し側も引き続き機能する)。API側は自前の
`SqlRejectedError` を別名で再エクスポートして後方互換を保つ。

`enforce_sql_boundary` は層2の fail-closed SQL ゲート(specs/18 §4.3 — SP2-03)。
行データの境界は VPD(層1)が持ち、こちらは VPD が隠さない面 — データディクショナリの
メタデータ・パッケージ経由の動的 SQL・DB リンク・登録簿外の JETUSE_DS_ 参照 — を塞ぐ。
"""

import re
from collections.abc import Collection

_BANNED = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|CREATE|GRANT|REVOKE|TRUNCATE|"
    r"EXECUTE|CALL|LOCK|COMMIT|ROLLBACK)\b",
    re.I,
)


class SqlRejectedError(ValueError):
    """ガードで拒否したSQL。"""


class SqlBoundaryError(SqlRejectedError):
    """層2 SQL ゲートで拒否した越境参照(specs/18 §4.3)。ルートは 403 に写像する。"""


def sanitize_sql(sql: str) -> str:
    """SELECT/WITH以外を拒否。コメント・セミコロン除去後に判定(SQL-02ガード)。"""
    cleaned = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    cleaned = re.sub(r"--[^\n]*", " ", cleaned)
    cleaned = cleaned.strip().rstrip(";").strip()
    if ";" in cleaned:
        raise SqlRejectedError("複数ステートメントは実行できません")
    if not cleaned.upper().startswith(("SELECT", "WITH")):
        raise SqlRejectedError("SELECT文のみ実行できます")
    if _BANNED.search(cleaned):
        raise SqlRejectedError("更新系キーワードを含むSQLは実行できません")
    return cleaned


# --- 層2 fail-closed SQL ゲート(specs/18 §4.3。allowlist 方式 — codex review-1 B001) ---

_DS_PREFIX = "JETUSE_DS_"
# 辞書・動的ビューの接頭辞。CTE 名で shadow しても素名テーブル参照では常に拒否する
# (codex review-2 B001 — 内側 CTE 名が外側の実辞書ビューを許可する迂回を塞ぐ多重防御)。
_DICT_PREFIXES = ("ALL_", "DBA_", "USER_", "CDB_", "V$", "GV$")
# パッケージ/既知の動的 SQL ベクタ(DBMS_XMLGEN 等は SELECT 句に関数として現れ FROM 位置に
# 出ないため、テーブル allowlist とは別に位置非依存で検出する)
_PKG_PREFIXES = ("DBMS_", "UTL_")
_BLOCKED_QUALIFIERS = frozenset({"SYS", "SYSTEM"})
# 文字列/URI をデータアクセス経路として解釈する関数・型コンストラクタ。文字列リテラル内の
# 表/ビュー参照は字句解析を素通りするため、関数名・型名で一律拒否する(codex review-7/8 B001)。
# デモ NL2SQL に正当な用途はない。カバー範囲:
#   - XQuery/XPath 評価: XMLQUERY / XMLTABLE / XMLEXISTS / EXISTSNODE / EXTRACTVALUE
#   - URI 型コンストラクタ(URI から表・ビューの行を取得): DBURITYPE / XDBURITYPE / HTTPURITYPE /
#     URITYPE、および URI 生成 SYS_DBURIGEN(`DBURIType('/SYS/ALL_USERS').getXML()` 迂回 — review-8)
#   - 辞書/システム露出の組み込み(ADR-0022 C の段階的硬化 — 人間承認 2026-07-07): セッション/環境・
#     辞書オブジェクト名・実行ユーザー・DB 名を返し、デモ NL2SQL に正当な用途がないもの。
#     ※ 全て `(` 付き呼び出しのみ拒否(素の列名 `SELECT SYS_CONTEXT FROM t` は非該当 = 後方互換)。
#     層2 完了条件は ADR-0022 B(実データ境界=VPD+最小権限、層2 はベストエフォート硬化)を正とする。
_FORBIDDEN_FUNCS = frozenset({
    "XMLQUERY", "XMLTABLE", "XMLEXISTS", "EXISTSNODE", "EXTRACTVALUE",
    "DBURITYPE", "XDBURITYPE", "HTTPURITYPE", "URITYPE", "SYS_DBURIGEN",
    "SYS_CONTEXT", "USERENV", "ORA_INVOKING_USER", "ORA_INVOKING_USERID",
    "ORA_DATABASE_NAME", "ORA_DICT_OBJ_NAME", "ORA_DICT_OBJ_OWNER",
    "ORA_DICT_OBJ_TYPE", "ORA_DICT_OBJ_NAME_LIST", "ORA_DICT_OBJ_OWNER_LIST",
})
# XML DB のデータアクセス URI(文字列リテラル内に隠れて字句解析を素通りする)。生 SQL を走査して
# 拒否する多重防御(oradb:/ URI・ora:view・fn:collection/doc — codex review-7 B001)。
_XMLDB_ACCESS_RE = re.compile(r"oradb:|ora:view|ora:contains|fn:collection|fn:doc", re.I)
# 共有サンプルスキーマ SH(Oracle Sales History)の既知オブジェクト。SH 修飾でも許可リスト外
# (未知の表・ビュー・SH 内 synonym 等)は拒否する fail-closed(codex review-3 M001)。
_SH_SCHEMA = "SH"
_SH_TABLES = frozenset({
    "SALES", "COSTS", "CUSTOMERS", "COUNTRIES", "CHANNELS", "PROMOTIONS",
    "PRODUCTS", "TIMES", "SUPPLEMENTARY_DEMOGRAPHICS", "PROFITS",
    "CAL_MONTH_SALES_MV", "FWEEK_PSCAT_SALES_MV",
})
# FROM 句を終える句境界キーワード(この後は列・条件でありテーブル参照ではない)。
# PIVOT/UNPIVOT は **含めない** — table_reference に付随する句で、その閉じ括弧の後も FROM リスト
# (カンマ結合・JOIN)が続くため、ここで in_from を落とすと後続テーブルが未検査になる
# (codex review-6 B002 — `FROM SH.SALES PIVOT(..) p, ALL_USERS u` の ALL_USERS 迂回を塞ぐ)。
# PIVOT の括弧内は式グループ(expect=False の子 ctx)で処理され、テーブル参照は現れない。
_FROM_END_KW = frozenset({
    "WHERE", "GROUP", "HAVING", "ORDER", "CONNECT", "START", "MODEL", "UNION",
    "INTERSECT", "MINUS", "FETCH", "OFFSET", "FOR",
})
# JOIN の装飾(この後もテーブルが続く)。APPLY / LATERAL は右辺に新しい table_reference を取る
# ため装飾ではなく JOIN と同様に「次はテーブル」を再武装する(codex review-4 B001 — CROSS/OUTER
# APPLY の右辺辞書ビュー・table function を検査から外さない)。
_JOIN_LEAD = frozenset({"INNER", "OUTER", "LEFT", "RIGHT", "FULL", "CROSS", "NATURAL"})
_JOIN_START = frozenset({"JOIN", "APPLY", "LATERAL"})

# 非 ASCII(日本語列名等)も識別子として字句を進める(辞書・パッケージ・JETUSE_DS_ は
# すべて ASCII 名のため、非 ASCII 識別子は allowlist に載らず拒否される)
_IDENT_RE = re.compile(r"[^\W\d][\w$#]*")


def _tokenize(sql: str) -> list[tuple[str, str]]:
    """単一パスの簡易字句解析。(kind, value) の列を返す。
    kind ∈ ident/dot/lparen/rparen/comma。

    文字列リテラル('' エスケープ対応)とクォート識別子を正しく区別する
    (逐次の正規表現置換は `"a'"` と `"b'"` の間を文字列と誤認して識別子を隠せる —
    ゲートは実行される SQL と同じ字句で判定しなければならない)。
    """
    tokens: list[tuple[str, str]] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c == "'":  # 文字列リテラル
            i += 1
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        i += 2
                        continue
                    break
                i += 1
            if i >= n:
                raise SqlBoundaryError("閉じられていない文字列リテラルがあります")
            i += 1
        elif c == '"':  # クォート識別子(大小文字を保存 — Oracle は quoted を別名として区別)
            j = sql.find('"', i + 1)
            if j < 0:
                raise SqlBoundaryError("閉じられていないクォート識別子があります")
            tokens.append(("qident", sql[i + 1:j]))  # qident = 大小文字保存(codex review-5 B001)
            i = j + 1
        elif c.isalpha() or c == "_" or ord(c) > 127:
            m = _IDENT_RE.match(sql, i)
            word = m.group(0)
            # q'...' 代替クォートは簡易字句解析の判定不能ケース → 拒否(fail-closed)
            if word.upper() in ("Q", "NQ") and m.end() < n and sql[m.end()] == "'":
                raise SqlBoundaryError("代替クォート(q'')リテラルは実行できません")
            # 未引用識別子は Oracle が大文字へ畳む(`tab` と `"tab"` は別名 — review-5 B001)
            tokens.append(("ident", word.upper()))
            i = m.end()
        elif c == "@":
            raise SqlBoundaryError("DBリンク(@)経由の参照は実行できません")
        elif c == ".":
            tokens.append(("dot", c))
            i += 1
        elif c == "(":
            tokens.append(("lparen", c))
            i += 1
        elif c == ")":
            tokens.append(("rparen", c))
            i += 1
        elif c == ",":
            tokens.append(("comma", c))
            i += 1
        else:
            i += 1
    return tokens


_NAME_KINDS = ("ident", "qident")  # 名前を構成する字句(引用/未引用。値は既に canonical)


def _read_name(tokens: list[tuple[str, str]], j: int) -> tuple[list[str], int]:
    """位置 j から修飾名 a(.b)* を読み、(parts, 次位置) を返す。名前でなければ ([], j)。
    値は canonical(未引用=大文字畳み / 引用=保存)なので呼び出し側で再変換しない。"""
    if j >= len(tokens) or tokens[j][0] not in _NAME_KINDS:
        return [], j
    parts = [tokens[j][1]]
    j += 1
    while (j + 1 < len(tokens) and tokens[j][0] == "dot"
           and tokens[j + 1][0] in _NAME_KINDS):
        parts.append(tokens[j + 1][1])
        j += 2
    return parts, j


def enforce_sql_boundary(
    sql: str,
    allowed_tables: Collection[str] | None = None,
    app_schema: str | None = None,
) -> None:
    """層2 fail-closed SQL ゲート(specs/18 §4.3)。違反は SqlBoundaryError(→403)。

    **allowlist 方式**(codex review-1 B001): FROM/JOIN のテーブル参照は次だけを許可し、
    それ以外(未知 synonym・別スキーマ・辞書ビュー・table function 等)は一律拒否する:
      - `DUAL` / WITH で定義された CTE 名
      - `SH.<表>`(共有サンプルスキーマ — 修飾必須)
      - `allowed_tables` の登録済み表(素名、または app_schema 修飾)
    加えて位置に依らず拒否: パッケージ(DBMS_/UTL_)・スキーマ修飾の関数呼び出し・SYS./SYSTEM.
    修飾・@ DBリンク・判定不能(q'' 等)。

    allowed_tables: 呼び出し元が許可する素名の表(SH 表名 ∪ 呼び出し元 owner の登録済み DS)。
    None/空 = owner なしモード(agent 経路 — SH 修飾照会のみ通り DS は全拒否)。
    app_schema: DS 表の許容修飾スキーマ(通常 JETUSE_APP)。None なら app 修飾は拒否。
    """
    # XML DB 経由の辞書/表アクセス(XQuery の oradb:/ URI・ora:view・fn:collection)は文字列
    # リテラル内に隠れて字句解析を素通りする → 生 SQL を走査して拒否(codex review-7 B001 —
    # `XMLQUERY('fn:collection("oradb:/SYS/ALL_TAB_COLUMNS")')` 迂回。関数名検出との多重防御)。
    if _XMLDB_ACCESS_RE.search(sql):
        raise SqlBoundaryError("XML DB 経由(oradb:/ora:view/fn:collection)の参照は実行できません")

    allowed = {t.upper() for t in (allowed_tables or ())}
    app = app_schema.upper() if app_schema else None
    tokens = _tokenize(sql)
    n = len(tokens)

    def check_ref(parts: list[str], cte: set[str]) -> None:
        # parts は canonical(未引用=大文字 / 引用=保存)。allowed/cte/定数も canonical で比較する。
        # 引用小文字名(例 `"tab"`)は未引用 TAB と別物扱い。許可集合に無ければ拒否(review-5 B001)。
        if len(parts) == 1:
            t = parts[0]
            # 辞書・DS 接頭辞は CTE 名で shadow しても拒否(review-2 B001 の多重防御)
            if t.startswith(_DICT_PREFIXES):
                raise SqlBoundaryError(f"辞書ビュー {t[:64]} は参照できません")
            if t.startswith(_DS_PREFIX):
                if t in allowed:
                    return
                raise SqlBoundaryError(
                    f"データセット表 {t[:64]} は参照できません(登録簿外)")
            if t == "DUAL" or t in cte or t in allowed:
                return
            raise SqlBoundaryError(f"テーブル {t[:64]} は参照できません(許可外)")
        if len(parts) == 2:
            schema, t = parts[0], parts[1]
            if schema == _SH_SCHEMA:
                if t in _SH_TABLES:
                    return
                raise SqlBoundaryError(
                    f"サンプルスキーマの未知オブジェクト SH.{t[:32]} は参照できません")
            if app and schema == app and t in allowed:
                return
            raise SqlBoundaryError(
                f"テーブル {schema[:32]}.{t[:32]} は参照できません(許可外スキーマ)"
            )
        raise SqlBoundaryError("3 段以上の修飾参照は実行できません")

    # paren 文脈スタック: sub=True は SELECT/サブクエリ文脈(FROM が句境界)。
    # sub=False は関数/グルーピング括弧(内部の FROM は EXTRACT(.. FROM ..) 等で句境界でない)。
    # cte は query block ごとにスコープする(codex review-2 B001 — 内側 CTE 名を外側で使わせない)。
    # push 時に親の cte を複製して継承し、pop で破棄する。同名 CTE を内側に定義して外側の実辞書
    # ビュー/DS を許可する迂回を防ぐ。
    ctx: list[dict] = [{"sub": True, "in_from": False, "expect": False, "cte": set()}]
    i = 0
    while i < n:
        kind, value = tokens[i]
        if kind == "lparen":
            parent = ctx[-1]
            nxt = tokens[i + 1] if i + 1 < n else None
            is_sub = bool(nxt and nxt[0] == "ident"
                          and nxt[1] in ("SELECT", "WITH"))  # 未引用キーワード(canonical 大文字)
            # 親が FROM/JOIN のテーブル位置で開いた括弧が SELECT/WITH でないなら、Oracle の
            # 括弧付き結合 `(t1 JOIN t2)` / 括弧付きテーブル参照。内部の先頭 table_reference と
            # 内部 JOIN も必ず検査する(codex review-3 B001 — 括弧付き JOIN で辞書ビュー参照を
            # 検査から外す迂回を塞ぐ)。サブクエリ(SELECT/WITH)は従来どおり内部が独自に検証される。
            grouped = parent["expect"] and not is_sub
            parent["expect"] = False  # 括弧がテーブル枠を満たす(素名として扱わない)
            ctx.append({"sub": is_sub or grouped, "in_from": grouped,
                        "expect": grouped, "cte": set(parent["cte"])})  # 親の可視 CTE を継承
            i += 1
            continue
        if kind == "rparen":
            if len(ctx) > 1:
                ctx.pop()
            i += 1
            continue
        if kind == "dot":
            i += 1
            continue
        if kind == "comma":
            if ctx[-1]["in_from"]:
                ctx[-1]["expect"] = True  # 旧式カンマ結合の次テーブル
            i += 1
            continue
        # ident / qident。value は canonical(未引用=大文字 / 引用=保存)。
        lvl = ctx[-1]
        # パッケージ・修飾・スキーマ修飾関数の拒否は **名前ベース**で引用/未引用いずれにも適用する
        # (canonical 値で照合。`"DBMS_XMLGEN"."GETXML"(..)` の引用大文字も同一パッケージに解決される
        # ため通さない — review-6 B001。引用小文字は別名なので接頭辞非該当=fail-closed)。
        if value.startswith(_PKG_PREFIXES):
            raise SqlBoundaryError(f"パッケージ {value[:64]} は呼び出せません")
        if value in _FORBIDDEN_FUNCS and _next_is(tokens, i, "lparen"):
            raise SqlBoundaryError(f"禁止された関数 {value[:32]}() は実行できません")
        if value in _BLOCKED_QUALIFIERS and _next_is(tokens, i, "dot"):
            raise SqlBoundaryError(f"{value[:32]}. 修飾の参照は実行できません")
        if (_next_is(tokens, i, "dot") and i + 2 < n
                and tokens[i + 2][0] in _NAME_KINDS and _next_is(tokens, i + 2, "lparen")):
            raise SqlBoundaryError(
                f"スキーマ修飾の関数呼び出し {value[:32]}.{tokens[i + 2][1][:32]}() "
                "は実行できません"
            )
        # CTE 定義 `X AS (`(X は引用/未引用いずれも。AS は未引用キーワード)。canonical で登録し、
        # 参照側も canonical で照合する(`"tab" AS(..)` と未引用 TAB を混同しない — review-5 B001)。
        if (_next_is(tokens, i, "ident") and tokens[i + 1][1] == "AS"
                and i + 2 < n and tokens[i + 2][0] == "lparen"):
            lvl["cte"].add(value)
            i += 1
            continue
        # 列リスト形 `X (cols) AS (`(Oracle で有効な `WITH t(a,b) AS (...)`)。`(...) AS (` は
        # 列別名リスト付き CTE を一意に示す(スカラ関数の別名は `AS ident` で `AS (` にならない)ため
        # 登録する — review-10 M001(旧ゲート下で通っていた公開 SQL の後方互換を守る)。
        if _next_is(tokens, i, "lparen"):
            close = _match_paren(tokens, i + 1)
            if (close is not None and close + 2 < n
                    and tokens[close + 1] == ("ident", "AS")
                    and tokens[close + 2][0] == "lparen"):
                lvl["cte"].add(value)
                i += 1
                continue
        if kind == "ident":
            if value == "FROM" and lvl["sub"]:
                lvl["in_from"] = True
                lvl["expect"] = True
                i += 1
                continue
            if value in _JOIN_START:  # JOIN / CROSS|OUTER APPLY / LATERAL — 右辺はテーブル参照
                lvl["expect"] = True
                i += 1
                continue
            if value in ("ON", "USING"):
                lvl["expect"] = False  # 結合条件へ(テーブルでなく列)
                i += 1
                continue
            if value in _JOIN_LEAD:
                i += 1
                continue
            if lvl["in_from"] and value in _FROM_END_KW:
                lvl["in_from"] = False
                lvl["expect"] = False
                i += 1
                continue
        if lvl["expect"]:
            parts, j = _read_name(tokens, i)
            if parts:
                if j < n and tokens[j][0] == "lparen":
                    raise SqlBoundaryError("FROM 位置の table function は実行できません")
                check_ref(parts, lvl["cte"])
                lvl["expect"] = False
                i = j
                continue
        i += 1


def _next_is(tokens: list[tuple[str, str]], idx: int, kind: str) -> bool:
    return idx + 1 < len(tokens) and tokens[idx + 1][0] == kind


def _match_paren(tokens: list[tuple[str, str]], open_idx: int) -> int | None:
    """open_idx の lparen に対応する rparen の index を返す(なければ None)。"""
    depth = 0
    for j in range(open_idx, len(tokens)):
        k = tokens[j][0]
        if k == "lparen":
            depth += 1
        elif k == "rparen":
            depth -= 1
            if depth == 0:
                return j
    return None
