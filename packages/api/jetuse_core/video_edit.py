"""場面メタデータの確認・修正・削除(VID-05 / specs/20 §5 / 要求8)。

**AI の結果を確定情報として扱わない**(ADR-0032 決定5)。人が直せて、直したことが
記録に残り、直した結果が検索に効くところまでで 1 つの機能になる:

  * `patch_scene` … 説明・タグ・画面内文字・場所・種別を直し、`source` を `human` にする
  * `confirm_scene` … 中身は変えずに「人が見た」を残す(`ai` → `ai_confirmed`)
  * `delete_scene` … 不適切なメタデータを場面ごと消す
  * `list_edits` … 何を誰がいつ直したか(`VIDEO_SCENE_EDITS`)

**直したら埋め込みを作り直す。** 直したのに検索結果が変わらないのは筋が通らない
(specs/20 §5)。作り直せなかったときは古いベクトルを**残さず NULL にする** ——
古い説明で当たり続けるほうが、検索に出ないことより判りにくい。応答の
`embedding_state` / `embedding_error` で「なぜ検索に出ないか」を返す。

**出所を上書きして判らなくしない**(tasks/VID-05.md 禁止事項)。`confirm` は
`ai` のときだけ `ai_confirmed` にする —— 人が書いた文(`human`)を `ai_confirmed` に
落とすと、「AI が書いて人が確認した」ことになり、誰の言葉かが判らなくなる。

所有者分離は `video.py` と同じ流儀で **SQL の `WHERE owner_sub = :o` に強制させる**。
他人の場面は 403 ではなく「存在しない」(404) —— 所有者以外に id の存在有無を漏らさない。
"""

import array
import json
import logging
import uuid
from typing import Any

from . import embeddings, video, video_analyze
from .db import connect

logger = logging.getLogger("jetuse.video_edit")

# 人が直せる項目(specs/20 §5 の列挙)。「カテゴリ」は場面の種別 = `scene_kind`。
# `VIDEO_ASSETS.category` は映像の属性で場面の属性ではないため、ここでは触らない。
# ここに無い項目(objects / people / actions / indoor / time_of_day / weather)は v1 の
# 範囲外 —— 直せる項目を増やすこと自体は容易だが、仕様が挙げた範囲を勝手に広げない。
EDITABLE_FIELDS = ("description", "tags", "screen_text", "place", "scene_kind")

# 列幅(migration 023)。**人の入力は黙って切らない**(AI の応答は `video_analyze` 側で
# 切り詰めるが、あれは「モデルが長く答えた」ときの手当てで、人が書いた文を勝手に
# 短くするのとは別の話)。超えたら 422 で返し、本人に直させる。
DESCRIPTION_MAX = 4000
SCREEN_TEXT_MAX = 4000
TAG_MAX = video_analyze.ITEM_MAX
TAGS_MAX = video_analyze.LIST_MAX
EDITED_BY_MAX = 255

# 履歴の取得件数の天井(一覧は減っても意味が壊れないのでクランプする)。
EDITS_LIMIT_MAX = 200

UNKNOWN = video_analyze.UNKNOWN


class SceneChangedError(RuntimeError):
    """埋め込みを作っている間に、その場面の中身が変わっていた。

    そのまま書くと**直した内容と埋め込みが食い違う**(古い文のベクトルが新しい文に
    貼られる)。API は 409 に対応させ、取り直させる。
    """


# --- 入力の正規化(純粋。ここだけで単体テストできる) ---------------------------


def normalize_edits(changes: dict[str, Any]) -> dict[str, Any]:
    """PATCH の入力を**列に入る形**へ寄せる。受け取れない値は `ValueError`(API は 422)。

    **空の場面を作らせない。** `description` を空にすると、その場面は検索でも画面でも
    役に立たないのに `source` だけ `human` になり、「人が消した」のか「人が空にした」のか
    判らなくなる。消したいなら `delete_scene` を使う。

    **`unknown` と NULL を混ぜない**(specs/20 §1)。`place` / `scene_kind` を空で送るのは
    「判らない」であって「まだ分析していない」ではないので `unknown` に寄せる。
    `screen_text` だけは空 = NULL —— 「画面に文字が無かった」を表す値が NULL だから
    (`video_analyze.normalize_scene` と同じ約束。`unknown` は「文字はあるが読めない」)。
    """
    unknown_keys = sorted(set(changes) - set(EDITABLE_FIELDS))
    if unknown_keys:
        raise ValueError(
            f"直せない項目です: {', '.join(unknown_keys)}"
            f"(直せるのは {', '.join(EDITABLE_FIELDS)})"
        )
    if not changes:
        raise ValueError("変更する項目がありません")

    out: dict[str, Any] = {}
    for field, value in changes.items():
        if field == "tags":
            out["tags"] = _tags(value)
        elif field == "description":
            text = _required_text(value, DESCRIPTION_MAX, field)
            out["description"] = text
        elif field == "screen_text":
            out["screen_text"] = _optional_text(value, SCREEN_TEXT_MAX, field)
        else:  # place / scene_kind
            limit = video_analyze.PLACE_MAX if field == "place" else (
                video_analyze.SCENE_KIND_MAX
            )
            out[field] = _optional_text(value, limit, field) or UNKNOWN
    return out


def _required_text(value: Any, limit: int, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} は文字列で送ってください")
    text = value.strip()
    if not text:
        raise ValueError(f"{field} を空にはできません(場面ごと消すなら DELETE)")
    if len(text) > limit:
        raise ValueError(f"{field} が長すぎます({len(text)} 文字 > {limit})")
    return text


def _optional_text(value: Any, limit: int, field: str) -> str | None:
    """空・None は「値なし」。**黙って切り詰めない**(超過は呼び出し元へ返す)。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} は文字列で送ってください")
    text = value.strip()
    if len(text) > limit:
        raise ValueError(f"{field} が長すぎます({len(text)} 文字 > {limit})")
    return text or None


def _tags(value: Any) -> list[str]:
    """タグの配列。**捨てずに拒む** —— 人が入れた語を黙って落とすと、直したつもりの
    タグが検索に出ない理由が利用者に判らない(AI 応答の正規化とはここが違う)。"""
    if not isinstance(value, list):
        raise ValueError("tags は配列で送ってください")
    if len(value) > TAGS_MAX:
        raise ValueError(f"tags が多すぎます({len(value)} 件 > {TAGS_MAX})")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"tags の要素は文字列で送ってください: {type(item).__name__}")
        text = item.strip()
        if not text:
            continue  # 空文字は「タグを入れていない」。落としても意味が変わらない
        if len(text) > TAG_MAX:
            raise ValueError(f"タグが長すぎます({len(text)} 文字 > {TAG_MAX}): {text[:20]}")
        out.append(text)
    return out


def _json_list(value: Any) -> list[str]:
    """JSON 列(CLOB)を文字列の配列として読む。壊れていれば空(読めた分だけ使う)。"""
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    if not isinstance(value, str) or not value.strip():
        return []
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return []
    return [v for v in parsed if isinstance(v, str)] if isinstance(parsed, list) else []


def embedding_source(scene: dict[str, Any]) -> dict[str, Any]:
    """台帳の 1 行を `video_analyze.embedding_text` が読める形へ。

    **埋め込みに載せる文字の作り方を分析と揃える。** 別々に組み立てると、人が直した
    場面だけベクトルの作り方が違うことになり、検索の順位が出所によって歪む。
    """
    return {
        "description": scene.get("description") or "",
        "tags": _json_list(scene.get("tags")),
        "objects": _json_list(scene.get("objects")),
        "actions": _json_list(scene.get("actions")),
        "place": scene.get("place") or UNKNOWN,
        "scene_kind": scene.get("scene_kind") or UNKNOWN,
        "weather": scene.get("weather") or UNKNOWN,
        "screen_text": scene.get("screen_text"),
    }


def _db_value(field: str, value: Any) -> Any:
    """列に渡す値。JSON 列だけ文字列にする(日本語はそのまま持つ)。"""
    if field == "tags":
        return json.dumps(value, ensure_ascii=False)
    return value


# --- 台帳の読み書き -----------------------------------------------------------

_SCENE_SELECT = (
    f"SELECT {video.scene_columns('s.')}, s.asset_id, a.analysis_state"
    " FROM video_scenes s JOIN video_assets a ON a.id = s.asset_id"
    " WHERE s.id = :id AND a.owner_sub = :o"
)
# **場面の行だけを掴む。** 結合をそのまま `FOR UPDATE` にすると映像の行まで locked に
# なり、`claim_analysis` → `_begin_analysis`(映像の行を握ってから場面を消す)と
# 掴む順が逆向きになってデッドロックし得る。
_SCENE_SELECT_LOCKED = _SCENE_SELECT + " FOR UPDATE OF s.description"

_COLUMN_COUNT = video.SCENE_COLUMN_COUNT


def _load_scene(cur: Any, owner: str, scene_id: str, *, lock: bool) -> dict[str, Any]:
    cur.execute(_SCENE_SELECT_LOCKED if lock else _SCENE_SELECT, id=scene_id, o=owner)
    row = cur.fetchone()
    if row is None:
        # 他人の場面も「無い」。所有者以外に id の存在有無を漏らさない
        raise LookupError(scene_id)
    scene = video.row_to_scene(row)
    scene["asset_id"] = row[_COLUMN_COUNT]
    scene["analysis_state"] = row[_COLUMN_COUNT + 1]
    return scene


def _reject_while_analyzing(scene: dict[str, Any]) -> None:
    """分析中は直させない(API は 409)。

    再分析は場面の行を**作り直す**(`video_analyze._begin_analysis` が消し、
    `video_frames._save_scenes` が入れ直す)ので、いま直しても必ず消える。
    受け取って消すより、消えることを先に伝えるほうが利用者の損が小さい。

    分析が**この判定の直後に**始まる場合は、場面の行ロックが順番を決める:
    `_begin_analysis` の DELETE はこちらのトランザクションが終わるまで待ち、その後に
    消す。人が直した内容が AI の記述で上書きされて `source` だけ `human` に残る、
    という「出所が判らなくなる」状態は起きない(消えるか、直ったまま残るか)。
    """
    if scene["analysis_state"] == "running":
        raise video.AnalysisInProgressError(scene["asset_id"])


def _record_edits(
    cur: Any, scene_id: str, editor: str, before: dict[str, Any], after: dict[str, Any]
) -> list[str]:
    """**値が変わった列だけ**を 1 行ずつ履歴に残す(何を誰がいつ / specs/20 §1)。

    `source` と `confirmed_at` も同じ規則で残す —— 出所が動いたことこそ残す価値がある。
    変わっていない項目まで記録すると、履歴を見ても何が直ったのか判らなくなる。
    """
    changed = [k for k, v in after.items() if _as_text(before.get(k)) != _as_text(v)]
    if not changed:
        return []
    # **束縛名に予約語を使わない。** `:by` は ORA-01745(invalid host/bind variable name)に
    # なる —— 実 ADB で初めて出た(2026-08-20)。fake の cursor は SQL を解釈しないので
    # 単体テストでは出ない種類の失敗で、履歴を残す側が丸ごと 503 になっていた。
    cur.executemany(
        """
        INSERT INTO video_scene_edits(id, scene_id, field, before_value, after_value,
                                      edited_by)
        VALUES (:id, :sid, :field, :before_value, :after_value, :editor)
        """,
        [
            {
                "id": str(uuid.uuid4()), "sid": scene_id, "field": field,
                "before_value": _as_text(before.get(field)),
                "after_value": _as_text(after[field]),
                "editor": editor[:EDITED_BY_MAX],
            }
            for field in changed
        ],
    )
    return changed


def _as_text(value: Any) -> str | None:
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False) if isinstance(value, list) else str(value)


# --- 埋め込みの作り直し -------------------------------------------------------


def _reembed(scene: dict[str, Any]) -> tuple[array.array | None, str | None, str]:
    """直した場面のベクトルを作り直す。(ベクトル, 失敗理由, 載せた文字列) を返す。

    **落ちても編集は捨てない。** 埋め込みは検索に効かせるための派生物で、人が直した
    内容そのものではない。作れなければベクトルを NULL にして理由を返し、編集は保存する
    (古いベクトルを残すと、直したのに古い説明で当たり続ける = 仕様が禁じた状態)。

    **値まで検べる**(`embeddings.as_vector`)。非数値・NaN・次元違いをそのまま
    `array.array` や UPDATE へ渡すと素の例外になり、この「編集は保存する」が破れる。
    """
    text = video_analyze.embedding_text(embedding_source(scene))
    if not text.strip():
        return None, "埋め込みに載せる文字がありません", text
    try:
        vectors = embeddings.embed([text])
    except Exception as e:  # noqa: BLE001 — 認証・429・タイムアウト・サービス障害
        return None, f"埋め込みの生成に失敗: {str(e)[:200]}", text
    if len(vectors) != 1:
        return None, f"埋め込みが 1 件ではありません({len(vectors)} 件)", text
    try:
        return embeddings.as_vector(vectors[0]), None, text
    except ValueError as e:
        return None, f"埋め込みの値が使えません: {e}", text


# --- 修正 ---------------------------------------------------------------------


def patch_scene(
    owner: str, scene_id: str, changes: dict[str, Any]
) -> dict[str, Any]:
    """場面のメタデータを直し、`source` を `human` にする(specs/20 §5 / 要求8)。

    埋め込みは**トランザクションの外**で作る(上流への往復。行を掴んだまま待たない)。
    そのぶん作っている間に中身が変わり得るので、書く直前に掴み直した行から同じ文字列を
    組み立て、食い違えば `SceneChangedError` で降りる —— **直した内容と埋め込みが
    食い違う状態を作らない**。
    """
    values = normalize_edits(changes)

    with connect() as conn:
        scene = _load_scene(conn.cursor(), owner, scene_id, lock=False)
    _reject_while_analyzing(scene)
    vector, embed_error, text = _reembed({**scene, **values})

    with connect() as conn:
        cur = conn.cursor()
        current = _load_scene(cur, owner, scene_id, lock=True)
        _reject_while_analyzing(current)
        merged = {**current, **values}
        if video_analyze.embedding_text(embedding_source(merged)) != text:
            raise SceneChangedError(scene_id)

        sets = ", ".join(f"{f} = :{f}" for f in values)
        cur.execute(
            f"UPDATE video_scenes SET {sets}, source = 'human',"
            "       confirmed_at = SYS_EXTRACT_UTC(SYSTIMESTAMP), embedding = :emb"
            " WHERE id = :id AND asset_id = :asset",
            emb=vector, id=scene_id, asset=current["asset_id"],
            **{f: _db_value(f, v) for f, v in values.items()},
        )
        if cur.rowcount == 0:
            # 掴んでいた行が消えた = 再分析が作り直した。**書けていない**ことを伝える
            raise SceneChangedError(scene_id)

        after = {f: _db_value(f, v) for f, v in values.items()} | {"source": "human"}
        updated = _load_scene(cur, owner, scene_id, lock=False)
        after["confirmed_at"] = updated["confirmed_at"]
        changed = _record_edits(cur, scene_id, owner, current, after)
        conn.commit()

    logger.info(
        "video scene edited: scene=%s fields=%s embedding=%s",
        scene_id, ",".join(changed), "failed" if embed_error else "ok",
    )
    return _with_embedding_state(updated, embed_error, changed)


def _with_embedding_state(
    scene: dict[str, Any], embed_error: str | None, changed: list[str]
) -> dict[str, Any]:
    """**なぜ検索に出ないかを応答に残す。** 埋め込みが作れなかった場面はベクトルが
    NULL なので、条件では引けても自然言語検索には出てこない。黙って 200 を返すと、
    利用者は「直したのに出ない」理由に辿り着けない。"""
    return {
        **scene,
        "changed_fields": changed,
        "embedding_state": "failed" if embed_error else "ok",
        "embedding_error": embed_error,
    }


# --- 確認 ---------------------------------------------------------------------


def confirm_scene(owner: str, scene_id: str) -> dict[str, Any]:
    """人が確認したことを残す(`ai` → `ai_confirmed` / specs/20 §5)。

    **中身は変えない**ので埋め込みは作り直さない(同じ文から同じベクトルができるだけ)。

    `human` はそのまま `human` にする。人が書いた文を `ai_confirmed` にすると
    「AI が書いて人が確認した」ことになり、**誰の言葉かが判らなくなる**
    (tasks/VID-05.md 禁止事項「出所を上書きして判らなくすること」)。確認した事実は
    `confirmed_at` と履歴が持つ。
    """
    with connect() as conn:
        cur = conn.cursor()
        current = _load_scene(cur, owner, scene_id, lock=True)
        _reject_while_analyzing(current)
        source = "ai_confirmed" if current["source"] == "ai" else current["source"]
        cur.execute(
            "UPDATE video_scenes SET source = :s,"
            "       confirmed_at = SYS_EXTRACT_UTC(SYSTIMESTAMP)"
            " WHERE id = :id AND asset_id = :asset",
            s=source, id=scene_id, asset=current["asset_id"],
        )
        if cur.rowcount == 0:
            raise SceneChangedError(scene_id)
        updated = _load_scene(cur, owner, scene_id, lock=False)
        changed = _record_edits(
            cur, scene_id, owner, current,
            {"source": source, "confirmed_at": updated["confirmed_at"]},
        )
        conn.commit()
    return {**updated, "changed_fields": changed}


# --- 削除 ---------------------------------------------------------------------


def delete_scene(owner: str, scene_id: str) -> bool:
    """不適切なメタデータを場面ごと消す(specs/20 §5)。

    履歴(`VIDEO_SCENE_EDITS`)も `ON DELETE CASCADE` で一緒に消える —— 指す先の無い
    履歴を残さない、という migration 023 の決定に従う。**消したこと自体はログに残す。**

    サムネイルは先に消す(`video.delete_asset` と同じ順)。消せなくても台帳の削除は
    続ける —— 残骸は課金がわずかに増えるだけだが、消せない「不適切なメタデータ」は
    利用者に見え続ける。残ったオブジェクトは再分析(前世代の掃除)か映像の削除
    (プレフィックス一括)で片付く。
    """
    with connect() as conn:
        scene = _load_scene(conn.cursor(), owner, scene_id, lock=False)
    _reject_while_analyzing(scene)

    if scene["thumb_object"]:
        try:
            client = video.os_client()
            client.delete_object(
                client.get_namespace().data, video.require_bucket(),
                scene["thumb_object"],
            )
        except Exception:
            logger.exception("video scene thumbnail cleanup failed (ignored)")

    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM video_scenes WHERE id = :id AND asset_id = :asset",
            id=scene_id, asset=scene["asset_id"],
        )
        deleted = cur.rowcount > 0
        conn.commit()
    logger.info(
        "video scene deleted: scene=%s asset=%s by=%s", scene_id, scene["asset_id"], owner
    )
    return deleted


# --- 履歴 ---------------------------------------------------------------------


def list_edits(owner: str, scene_id: str, limit: int = 100) -> list[dict[str, Any]]:
    """その場面の修正履歴(新しい順)。**残した記録は読めるようにする** ——
    読めない履歴は「残した」ことにならない(要求8 の確認・修正の根拠)。"""
    limit = max(1, min(limit, EDITS_LIMIT_MAX))
    with connect() as conn:
        cur = conn.cursor()
        _load_scene(cur, owner, scene_id, lock=False)  # 所有者確認(無ければ LookupError)
        cur.execute(
            "SELECT field, before_value, after_value, edited_by, "
            + video.TS_UTC.format(col="edited_at")
            + " FROM video_scene_edits WHERE scene_id = :id"
            " ORDER BY edited_at DESC, id DESC FETCH NEXT :lim ROWS ONLY",
            id=scene_id, lim=limit,
        )
        return [
            {
                "field": r[0], "before": r[1], "after": r[2],
                "edited_by": r[3], "edited_at": r[4],
            }
            for r in cur.fetchall()
        ]
