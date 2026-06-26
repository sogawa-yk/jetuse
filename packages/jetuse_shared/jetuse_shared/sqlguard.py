"""SQLサニタイズ(SELECT/WITH限定の多層ガード)。API/コンテナ共有 — jetuse_shared。

元実装: `jetuse_core/nl2sql.py::sanitize_sql`(SqlRejectedError) と
`agent-containers/agent_db.py::_sanitize`(ValueError)を一本化。

例外は jetuse_shared 固有の `SqlRejectedError` を送出する(ValueError サブクラスなので
従来 ValueError を catch していた呼び出し側も引き続き機能する)。API側は自前の
`SqlRejectedError` を別名で再エクスポートして後方互換を保つ。
"""

import re

_BANNED = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|CREATE|GRANT|REVOKE|TRUNCATE|"
    r"EXECUTE|CALL|LOCK|COMMIT|ROLLBACK)\b",
    re.I,
)


class SqlRejectedError(ValueError):
    """ガードで拒否したSQL。"""


def strip_code_fences(text: str) -> str:
    """LLM 生成 SQL から markdown コードフェンス(```sql ... ```)を除去して本文を返す。

    NL2SQL の生成器(Select AI / schema-in-prompt LLM)は ```sql で囲んだ応答を返すことがある。
    実行ガード(sanitize_sql)へ渡す前に剥がす純粋関数(DB 非依存・両バックエンド共用)。
    """
    return re.sub(r"^```(sql)?\s*|\s*```$", "", (text or "").strip(), flags=re.I | re.M).strip()


# --- テーブル参照の抽出と許可リスト検証(NL2SQL のスキーマ境界ガード / SBA-B) ----------
# 完全な SQL パーサではなく、FROM / JOIN 直後のテーブル参照を素朴に抽出する近似実装。
# 「許可された既知テーブルのみ参照」を強制する用途に閉じる(未知/スキーマ修飾名は呼び出し側で拒否)。
# 実行は読取専用ユーザー(SELECT 権限のみ)で多層に守るが、生成 SQL を定義スキーマ内に閉じる
# コード側ガードを足して別スキーマ/辞書ビュー露出のリスクを下げる(defense-in-depth)。

_TOKEN_RE = re.compile(r'"[^"]*"|[A-Za-z_][A-Za-z0-9_$#]*|[(),.*]|\S')

#: FROM のテーブルリストを終える句/結合キーワード(これらはエイリアスとして消費しない)。
_FROM_STOP = frozenset(
    {
        "WHERE", "GROUP", "ORDER", "HAVING", "UNION", "INTERSECT", "MINUS",
        "CONNECT", "START", "FETCH", "OFFSET", "MODEL", "PIVOT", "UNPIVOT",
        "ON", "USING", "JOIN", "INNER", "LEFT", "RIGHT", "FULL", "CROSS", "NATURAL",
    }
)

#: 文字列リテラル('...'。'' は埋め込みエスケープ)。テーブル/CTE 抽出前に中身を消す。
_STRING_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")
#: Oracle q-quote リテラル(q'[..]' / q'{..}' / q'(..)' / q'<..>' / q'X..X' 任意区切り)。
#: ペア区切り(角/波/丸/山括弧)を先に、それ以外は自己区切り(同一文字で閉じる)で処理する。
_QQUOTE_RE = re.compile(
    r"[qQ]'(?:\[.*?\]|\{.*?\}|\(.*?\)|<.*?>|(.).*?\1)'",
    re.S,
)


def _blank_string_literals(sql: str) -> str:
    """文字列リテラルの中身を空にする(リテラル内の FROM/JOIN/AS を誤抽出しないため)。

    例: `WHERE note = ', x AS ('` や q-quote `q'[, x AS (]'` のリテラルを潰し、許可リスト回避
    (B2)を防ぐ。q-quote を先に潰す(内部に素の `'` を含みうるため)。
    """
    s = _QQUOTE_RE.sub("''", sql)
    return _STRING_LITERAL_RE.sub("''", s)


def _is_ident(tok: str) -> bool:
    return bool(re.match(r'"?[A-Za-z_]', tok))


def _skip_alias(toks: list[str], j: int, n: int) -> int:
    """テーブル参照直後の(任意の)エイリアスを読み飛ばす。"""
    if j < n and toks[j].upper() == "AS":
        j += 1
    if j < n and _is_ident(toks[j]) and toks[j].upper() not in _FROM_STOP:
        j += 1
    return j


def _skip_balanced(toks: list[str], j: int, n: int) -> int:
    """toks[j] が '(' のとき、対応する ')' の次の位置を返す。"""
    depth = 0
    while j < n:
        if toks[j] == "(":
            depth += 1
        elif toks[j] == ")":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return j


def cte_names(sql: str) -> set[str]:
    """先頭のトップレベル WITH 句で定義された CTE 名(大文字・引用符除去)の集合。

    CTE はクエリブロック内スコープなので、ネストした(部分問い合わせ内の)WITH 由来の名前は
    外側の FROM/JOIN 参照に適用しない(安全側=過剰拒否)。文字列リテラルは抽出前に潰す。
    先頭が WITH でなければ CTE 無し(空集合)。これにより、ネスト WITH で実テーブル名と同名の
    CTE を定義して外側参照を通す回避(B1)を防ぐ。
    """
    toks = _TOKEN_RE.findall(_blank_string_literals(sql))
    n = len(toks)
    if not toks or toks[0].upper() != "WITH":
        return set()
    names: set[str] = set()
    j = 1
    while j < n:
        if not _is_ident(toks[j]):
            break
        name = toks[j].strip('"').upper()
        j += 1
        if j < n and toks[j] == "(":  # 任意の列リスト name (c1, c2) AS (...)
            j = _skip_balanced(toks, j, n)
        if j < n and toks[j].upper() == "AS":
            j += 1
        if j < n and toks[j] == "(":  # CTE 本体。名前を確定し本体を読み飛ばす。
            names.add(name)
            j = _skip_balanced(toks, j, n)
        else:
            break  # WITH の構文から外れた → 中断
        if j < n and toks[j] == ",":  # 次の CTE
            j += 1
            continue
        break  # メインクエリ開始
    return names


def referenced_tables(sql: str) -> list[str]:
    """FROM / JOIN 直後に現れるテーブル参照を抽出する(部分問い合わせは括弧で除外)。

    返り値はスキーマ修飾名(例 'SYS.DBA_USERS')も含む。許可リスト検証は呼び出し側。
    文字列リテラル内の FROM/JOIN を誤抽出しないよう、リテラルを潰してから走査する。
    """
    toks = _TOKEN_RE.findall(_blank_string_literals(sql))
    n = len(toks)
    refs: list[str] = []
    i = 0
    while i < n:
        kw = toks[i].upper()
        if kw in ("FROM", "JOIN"):
            single = kw == "JOIN"
            j = i + 1
            while j < n:
                t = toks[j]
                if t == "(":  # 部分問い合わせ/派生表。内側の FROM/JOIN は本ループが別途拾う。
                    depth = 1
                    j += 1
                    while j < n and depth:
                        depth += 1 if toks[j] == "(" else (-1 if toks[j] == ")" else 0)
                        j += 1
                    j = _skip_alias(toks, j, n)
                elif _is_ident(t):
                    name = t
                    j += 1
                    while j + 1 < n and toks[j] == ".":  # スキーマ修飾を連結
                        name += "." + toks[j + 1]
                        j += 2
                    if j < n and toks[j] == "@":  # DB link 記法(name@dblink)も参照に含める
                        name += "@" + (toks[j + 1] if j + 1 < n else "")
                        j += 2
                        while j + 1 < n and toks[j] == ".":  # dblink の db.domain 部分
                            name += "." + toks[j + 1]
                            j += 2
                    refs.append(name)
                    j = _skip_alias(toks, j, n)
                else:
                    break
                if not single and j < n and toks[j] == ",":  # FROM のみカンマで継続
                    j += 1
                    continue
                break
            # キーワードの次から1トークンだけ進める。部分問い合わせ内の FROM/JOIN も
            # 本ループが必ず走査するため(クラスタごと飛ばすと内側参照を取りこぼす)。
            i += 1
        else:
            i += 1
    return refs


def _norm_table_ref(ref: str) -> str:
    """テーブル参照を Oracle のケース規則で正規化する。

    引用識別子("…")はケースを保持(unquoted のフォールド先=大文字と完全一致時のみ許可される)、
    非引用は大文字フォールド。これにより `"inventory"`(小文字引用=別オブジェクト)を
    unquoted `INVENTORY` と取り違えない(M2)。
    """
    if len(ref) >= 2 and ref.startswith('"') and ref.endswith('"'):
        return ref[1:-1]  # 引用: ケース保持(フォールドしない)
    return ref.upper()    # 非引用: 大文字フォールド


def assert_tables_allowed(
    sql: str,
    allowed: set[str],
    *,
    allow_dual: bool = True,
    require_table: bool = False,
) -> None:
    """SQL が参照するテーブルが allowed(大文字・非修飾名)に閉じているか検証する。

    スキーマ修飾名(`.`)・DB link(`@`)・未知テーブルは SqlRejectedError。WITH の CTE 名は
    暗黙に許可する。allowed は呼び出し側が「定義スキーマのテーブル名(大文字)」で与える。
    引用識別子はケースを区別する(`"inventory"` は `INVENTORY` と別物として拒否 / M2)。

    allow_dual=False: `DUAL` を暗黙許可しない(sample-app 専用 execute など、業務テーブル以外を
    一切通さない経路向け。`SELECT USER FROM DUAL` 等を拒否)。
    require_table=True: allowed の業務テーブルを最低1つ参照していなければ拒否(CTE/DUAL のみ、
    あるいは FROM 無しのスカラ照会=`SELECT SYS_CONTEXT(...)`/関数呼び出しだけの SQL を弾く)。
    """
    allowed_norm = {a.strip('"').upper() for a in allowed}
    cte = cte_names(sql)
    permitted = set(allowed_norm) | cte
    if allow_dual:
        permitted |= {"DUAL"}
    refs = referenced_tables(sql)
    hit_dataset_table = False
    for ref in refs:
        if "." in ref:
            raise SqlRejectedError(f"スキーマ修飾テーブルは参照できません: {ref}")
        if "@" in ref:
            raise SqlRejectedError(f"DB link 経由のテーブルは参照できません: {ref}")
        norm = _norm_table_ref(ref)
        if norm not in permitted:
            raise SqlRejectedError(f"許可外のテーブルを参照しています: {ref}")
        # 業務テーブル参照としてカウントするのは「allowed かつ同名 CTE に shadow されていない」場合のみ。
        # `WITH INVENTORY AS (...) SELECT * FROM INVENTORY` は CTE 解決のため実テーブル不参照とみなす
        # (require_table の CTE シャドーイング回避を防ぐ)。
        if norm in allowed_norm and norm not in cte:
            hit_dataset_table = True
    if require_table and not hit_dataset_table:
        raise SqlRejectedError("許可された業務テーブルを参照していません")


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
