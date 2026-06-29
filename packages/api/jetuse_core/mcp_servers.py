"""MCPサーバーレジストリ(AGT-02)。owner_sub分離、認証情報はVault(OCIDのみ保持)。

SPIKE-11実機確定: Responses APIの type:"mcp" ツールでサーバーサイド実行される。
"""

import logging
import re
import uuid
from typing import Any
from urllib.parse import urlparse

from .db import connect
from .settings import get_settings
from .webtools import SsrfBlockedError, _assert_public_host

logger = logging.getLogger("jetuse.mcp")


class VaultWriteError(Exception):
    """認証付き MCP の資格情報を Vault へ束ねられない(未設定 / 権限欠如)。fail-closed 用。"""


def _uid() -> str:
    return str(uuid.uuid4())


# Bearer credential として安全な文字集合: 空白・制御文字を含まない可視 ASCII(0x21-0x7E)のみ。
# RFC 7235 の token68 より緩いが、ヘッダー分割・制御文字注入を防ぐには十分(BE08-R3-005/R4-004)。
# fullmatch で全体一致を強制する($ は末尾改行直前にもマッチするため使わない / BE08-R5-001)。
_BEARER_TOKEN_RE = re.compile(r"[\x21-\x7E]+")


def _normalize_token(token: str | None) -> str | None:
    """auth_token を正規化・検証する(BE08-R3-005/R4-004)。

    空文字/空白のみは「認証なし」として None に畳む(本番もテストダブルも同じ判定)。それ以外は
    **トリムせず原文のまま**検証し、内部空白・先頭末尾の空白/改行・制御文字・非 ASCII を含めば拒否
    する(silent な資格情報変更を起こさない。Authorization: Bearer に安全に載るものだけ通す)。
    """
    if token is None or not token.strip():
        return None
    if not _BEARER_TOKEN_RE.fullmatch(token):
        raise ValueError("auth_token に空白/制御文字/非ASCII文字は使用できません")
    return token


def validate_url(url: str) -> None:
    """https必須 + 公開ホストのみ(SSRFガード流用)"""
    p = urlparse(url)
    if p.scheme != "https" or not p.hostname:
        raise SsrfBlockedError("MCPサーバーのURLはhttpsである必要があります")
    _assert_public_host(p.hostname)


def list_servers(owner: str) -> list[dict[str, Any]]:
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, label, url, auth_secret_ocid,
                   TO_CHAR(created_at, 'YYYY-MM-DD"T"HH24:MI:SS')
            FROM mcp_servers WHERE owner_sub = :o ORDER BY created_at
            """,
            o=owner,
        )
        return [
            {
                "id": r[0], "label": r[1], "url": r[2],
                "has_auth": r[3] is not None, "created_at": r[4],
            }
            for r in cur.fetchall()
        ]


def get_servers(owner: str, ids: list[str]) -> list[dict[str, Any]]:
    if not ids:
        return []
    binds = {f"id{i}": v for i, v in enumerate(ids[:10])}
    placeholders = ", ".join(f":id{i}" for i in range(len(binds)))
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT id, label, url, auth_secret_ocid FROM mcp_servers
            WHERE owner_sub = :o AND id IN ({placeholders})
            """,
            o=owner, **binds,
        )
        return [
            {"id": r[0], "label": r[1], "url": r[2], "auth_secret_ocid": r[3]}
            for r in cur.fetchall()
        ]


def create_server(
    owner: str,
    label: str,
    url: str,
    auth_secret_ocid: str | None = None,
    *,
    auth_token: str | None = None,
) -> dict:
    """MCP サーバーを登録する(BE-08)。

    認証情報の与え方は2通り(同時指定は不可):
    - ``auth_token``(keyword-only): 生トークン。**Vault に書込**み、DB には OCID 参照のみ保持し
      ``secret_managed=1``(=アプリ管理。削除時に削除予約)。実値は DB/コードに置かない(ADR-0014)。
    - ``auth_secret_ocid``(後方互換の第4位置引数): 既に外部で作成済みの secret OCID をそのまま登録
      する従来経路。``secret_managed=0``(=外部管理。削除時に Vault を触らない)。

    SSRF/URL ガードは Vault 書込の **前** に効き、不正 URL では Vault を一切触らない(fail-closed)。
    Vault 未設定/権限欠如は VaultWriteError(呼び出し側で 503)。auth_token は位置引数の OCID を
    トークンとして二重 secret 化する事故を防ぐため keyword-only(BE08-R2-004/R3-004)。
    """
    if auth_token is not None and auth_secret_ocid is not None:
        raise ValueError("auth_token と auth_secret_ocid は同時指定できません")
    auth_token = _normalize_token(auth_token)  # ""/空白は None 扱いに正規化(BE08-R3-005)
    validate_url(url)  # SSRF/URL ガード: Vault 書込より前に fail-closed
    sid = _uid()
    managed = 0
    if auth_token is not None:
        # 認証付き(生トークン): Vault へ束ねる。retry_token=sid で create_secret を冪等化。
        # secret 作成は ACTIVE 待ちまで _write_secret 内で完結。
        auth_secret_ocid = _write_secret(f"mcp-{sid}", auth_token, retry_token=sid)
        managed = 1
    try:
        with connect() as conn:
            conn.cursor().execute(
                """
                INSERT INTO mcp_servers(id, owner_sub, label, url, auth_secret_ocid, secret_managed)
                VALUES (:id, :o, :l, :u, :a, :m)
                """,
                id=sid, o=owner, l=label[:100], u=url[:1000], a=auth_secret_ocid, m=managed,
            )
            conn.commit()
    except Exception:
        # DB 登録に失敗したら、直前に作成した **自管理** secret のみ補償削除（外部 OCID は触らない /
        # 孤児防止 / BE08-003）。
        if managed and auth_secret_ocid:
            _schedule_secret_deletion_safe(auth_secret_ocid)
        raise
    return {"id": sid, "label": label, "url": url, "has_auth": auth_secret_ocid is not None}


def delete_server(owner: str, sid: str) -> bool:
    """MCP サーバー登録を削除する(BE-08)。

    アプリ管理 secret(secret_managed=1)は **DB 行を消す前に** Vault 削除予約を行う。予約に失敗
    したら DB 行を残したまま VaultWriteError を投げる(呼び出し側で 503)。これにより唯一の永続
    参照(OCID)が失われた孤児 secret を作らず、利用者は再試行できる(BE08-R2-002)。外部管理 OCID
    (secret_managed=0)は触らない。
    """
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT auth_secret_ocid, secret_managed FROM mcp_servers "
            "WHERE id = :id AND owner_sub = :o",
            id=sid, o=owner,
        )
        row = cur.fetchone()
        if not row:
            return False
        auth_secret_ocid, managed = row[0], row[1]
        # 1) 自管理 secret の削除予約を先に確定させる(失敗時は DB 行を残し fail-closed)。
        if auth_secret_ocid and managed:
            _schedule_secret_deletion(auth_secret_ocid)  # 失敗で VaultWriteError → 行は残る
        # 2) 予約成功後に DB 行を削除。
        cur.execute(
            "DELETE FROM mcp_servers WHERE id = :id AND owner_sub = :o", id=sid, o=owner
        )
        conn.commit()
    return True


def mcp_tool_spec(server: dict, auto: bool) -> dict:
    """Responses APIのmcpツール定義に変換(AGT-02)"""
    spec: dict = {
        "type": "mcp",
        "server_label": server["label"],
        "server_url": server["url"],
        "require_approval": "never" if auto else "always",
    }
    if server.get("auth_secret_ocid"):
        token = _read_secret(server["auth_secret_ocid"])
        spec["headers"] = {"Authorization": f"Bearer {token}"}
    return spec


def _read_secret(secret_ocid: str) -> str:
    import base64
    import os

    import oci

    if os.environ.get("AUTH_MODE") == "resource_principal":
        signer = oci.auth.signers.get_resource_principals_signer()
        client = oci.secrets.SecretsClient({}, signer=signer)
    else:
        client = oci.secrets.SecretsClient(oci.config.from_file())
    bundle = client.get_secret_bundle(secret_ocid).data
    return base64.b64decode(bundle.secret_bundle_content.content).decode()


# OCI 呼び出しの明示 timeout(connect, read)秒。SDK 既定(connect 10/read 60)＋既定再試行だと
# 単一呼出でも API Gateway 60秒を超え得るため短く固定し、再試行も無効化(BE08-R3-003)。
_VAULT_TIMEOUT = (5, 15)
# create + ACTIVE 待ちの総 deadline 秒(monotonic)。client timeout(5,15)＋retry無効と併せ、
# 最悪でも「deadline 直前に始まった呼出(≤15s)＋補償削除(≤15s)」を足して Gateway 60秒未満に収める
# 設計値(25+15+15=55<60 / BE08-R3-003/R5-003。ADR-0018 と一致)。deadline は in-flight な OCI 呼出を
# 中断しないため、絶対保証には各呼出 timeout の動的調整が要る(ADR-0018 の残リスク・人間ゲート)。
_OP_DEADLINE_S = 25


def _vault_client():
    """VaultsClient(コントロールプレーン: create/get/schedule_secret_deletion)を構築する。

    初期化失敗(設定/署名子の不備)も VaultWriteError へ畳む(呼び出し側で 503 fail-closed)。
    timeout を明示し retry を無効化して、Gateway 60秒以内に収める(BE08-R3-003)。
    """
    import os

    import oci

    try:
        kwargs = {
            "timeout": _VAULT_TIMEOUT,
            "retry_strategy": oci.retry.NoneRetryStrategy(),
        }
        if os.environ.get("AUTH_MODE") == "resource_principal":
            signer = oci.auth.signers.get_resource_principals_signer()
            return oci.vault.VaultsClient({}, signer=signer, **kwargs)
        return oci.vault.VaultsClient(oci.config.from_file(), **kwargs)
    except Exception as e:  # 署名子/設定の初期化失敗(BE08-005)
        raise VaultWriteError(f"Vault クライアントの初期化に失敗: {e}") from e


def _write_secret(name: str, value: str, retry_token: str | None = None) -> str:
    """資格情報を Vault へ書込み、ACTIVE になった secret の OCID を返す(BE-08)。

    create_secret はコントロールプレーン(VaultsClient)。read(_read_secret)はデータ
    プレーン(SecretsClient)で対になる。Vault/暗号鍵/コンパートメント未設定は VaultWriteError
    で fail-closed(実 Vault 書込 IAM の付与は人間ゲート)。OCI 障害(権限欠如・スロットリング・
    5xx・ACTIVE 待ちタイムアウト等)はすべて VaultWriteError へ畳み(呼び出し側で 503)、
    通常の未処理 500 を出さない(BE08-002/005)。ACTIVE になる前の失敗 secret は補償削除する。
    """
    import base64
    import time

    import oci

    s = get_settings()
    if not (s.vault_ocid and s.vault_key_ocid and s.compartment_ocid):
        raise VaultWriteError(
            "認証付き MCP の登録には Vault 書込設定"
            "(vault_ocid / vault_key_ocid / compartment_ocid)が必要です"
            "(docs/setup/iam.md。Vault 書込 IAM ポリシー追加は人間ゲート)"
        )
    client = _vault_client()
    content = base64.b64encode(value.encode()).decode()
    details = oci.vault.models.CreateSecretDetails(
        compartment_id=s.compartment_ocid,
        vault_id=s.vault_ocid,
        key_id=s.vault_key_ocid,
        secret_name=name,
        secret_content=oci.vault.models.Base64SecretContentDetails(content=content),
    )
    create_kwargs = {"opc_retry_token": retry_token} if retry_token else {}
    # 総 deadline は **create の前** に固定し、create と ACTIVE 待ちを合わせて bound する。
    # client timeout(5,15)＋retry無効と併せ、処理全体を API Gateway 60秒より確実に下回らせる
    # (BE08-R3-003)。GW タイムアウト後にサーバが完走→再送で別 secret が増える競合を抑える。
    deadline = time.monotonic() + _OP_DEADLINE_S
    try:
        secret_id = client.create_secret(details, **create_kwargs).data.id
    except oci.exceptions.ServiceError as e:
        raise _vault_service_error(e) from e
    except Exception as e:  # ネットワーク等
        raise VaultWriteError(f"Vault への secret 作成に失敗: {e}") from e

    # secret が ACTIVE になるまで待つ。CREATING/FAILED のまま 200 を返すと後で _read_secret が
    # 失敗する登録ができてしまう(BE08-002)。失敗/タイムアウトは補償削除して VaultWriteError。
    active = oci.vault.models.Secret.LIFECYCLE_STATE_ACTIVE
    bad_states = {"FAILED", "DELETING", "DELETED", "PENDING_DELETION", "CANCELLING_DELETION"}
    while time.monotonic() < deadline:
        try:
            state = client.get_secret(secret_id).data.lifecycle_state
        except oci.exceptions.ServiceError as e:
            _schedule_secret_deletion_safe(secret_id)
            raise _vault_service_error(e) from e
        except Exception as e:
            _schedule_secret_deletion_safe(secret_id)
            raise VaultWriteError(f"Vault secret の状態取得に失敗: {e}") from e
        if state == active:
            return secret_id
        if state in bad_states:
            _schedule_secret_deletion_safe(secret_id)
            raise VaultWriteError(f"Vault secret が不正状態({state})になりました")
        time.sleep(2)
    _schedule_secret_deletion_safe(secret_id)
    raise VaultWriteError("Vault secret が時間内に ACTIVE になりませんでした")


def _vault_service_error(e) -> "VaultWriteError":
    """OCI ServiceError を VaultWriteError(→503)へ分類する(BE08-005)。

    いずれも fail-closed(503)だが、恒久的な不備(権限・入力)と一時障害(スロットリング・5xx)を
    メッセージで区別する。自動再試行はしない(冪等化は create_secret の opc_retry_token に委ねる)。
    """
    status = e.status
    if status in (401, 403, 404):
        return VaultWriteError(
            f"Vault への書込に失敗(権限欠如/不在の可能性 status={status})。"
            "Vault 書込 IAM ポリシー(use keys / use vaults / manage secret-family)の"
            "付与は人間ゲートです(docs/setup/iam.md)"
        )
    if status == 429 or status >= 500:
        # スロットリング・一時障害。時間をおいて再試行可能(自動再試行はしない)。
        return VaultWriteError(f"Vault への書込に失敗(一時障害/再試行可能 status={status})")
    # その他 4xx(400 不正リクエスト・409 競合等)は恒久的な入力不備。
    return VaultWriteError(f"Vault への書込に失敗(恒久的な入力不備 status={status})")


# 削除予約の時刻オフセット。OCI の許容は「受信時点から1〜30日」。クライアント時計のずれ・通信
# 遅延で下限(1日)を割らないよう 2 日後にする(ちょうど24時間は境界割れの危険 / BE08-R3-001)。
_SECRET_DELETION_OFFSET_DAYS = 2


# 「確実に削除へ向かっている」状態のみ。CANCELLING_DELETION は ACTIVE へ戻る途中なので **含めない**
# (含めると削除予約済みと誤認し→ DB 行を消す→ secret が ACTIVE に戻り孤児化 / BE08-R5-002)。
_DELETING_STATES = {"SCHEDULING_DELETION", "PENDING_DELETION", "DELETING", "DELETED"}


def _schedule_secret_deletion(secret_ocid: str) -> None:
    """自管理 secret を削除予約する(strict)。失敗は VaultWriteError で伝播する。

    OCI は secret の即時削除を許さず、削除予約(時刻指定)のみ。delete_server はこれを DB 行削除の
    **前** に呼び、失敗時は行を残して fail-closed する(孤児防止 / BE08-R2-002)。

    エラー時の冪等性は安全側に倒す(BE08-R4-001):
    - **404**: OCI の NotAuthorizedOrNotFound は「不在」と「権限不足」を区別しない。権限不足を成功と
      誤認して live secret を残すのを避けるため **fail-closed**(VaultWriteError → 行を残す)。
    - **409**: `get_secret` で削除進行中(PENDING_DELETION 等)を**確認できたときだけ**冪等成功。
      確認できなければ状態競合として fail-closed。「予約成功後 DB 失敗→再試行が 409 で詰まる」の
      回避は、削除進行中が確認できる正当ケースに限定する(BE08-R3-002)。
    """
    import datetime

    import oci

    client = _vault_client()  # 失敗で VaultWriteError
    when = datetime.datetime.now(datetime.UTC) + datetime.timedelta(
        days=_SECRET_DELETION_OFFSET_DAYS
    )
    try:
        client.schedule_secret_deletion(
            secret_ocid,
            oci.vault.models.ScheduleSecretDeletionDetails(time_of_deletion=when),
        )
    except oci.exceptions.ServiceError as e:
        if e.status == 409 and _secret_is_deleting(client, secret_ocid):
            logger.info("MCP secret は既に削除進行中(冪等成功): %s", secret_ocid)
            return
        raise _vault_service_error(e) from e
    except Exception as e:
        raise VaultWriteError(f"Vault secret の削除予約に失敗: {e}") from e


def _secret_is_deleting(client, secret_ocid: str) -> bool:
    """secret が既に削除進行中の状態かを get_secret で確認する(BE08-R4-001)。確認不能は False。"""
    import oci

    try:
        return client.get_secret(secret_ocid).data.lifecycle_state in _DELETING_STATES
    except oci.exceptions.ServiceError:
        return False
    except Exception:
        return False


def _schedule_secret_deletion_safe(secret_ocid: str) -> None:
    """削除予約のベストエフォート版(create 補償用 / BE08-003)。

    create_server の DB 登録失敗時の後始末。すでに上位で元例外を投げるため、ここでの失敗は
    握って監査ログに残す(孤児の可能性を記録)。delete 経路は strict 版を使うこと。
    """
    try:
        _schedule_secret_deletion(secret_ocid)
    except Exception:
        logger.exception("MCP secret 削除予約に失敗(孤児の可能性): %s", secret_ocid)
