"""VID-01 映像の保管と登録。Object Storage と ADB は fake で置き換え、
登録・一覧・詳細・削除 / PAR の期限 / 所有者分離を検証する。
"""

import contextlib
import io
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import oci
import pytest
from fastapi.testclient import TestClient

from jetuse_core import video
from service.deps import require_video
from service.main import app

client = TestClient(app)

OWNER = "dev-user"
OTHER = "someone-else"


# --- fake Object Storage ------------------------------------------------------


class FakeObj:
    def __init__(self, name):
        self.name = name


class FakeListObjects:
    def __init__(self, names, next_start_with=None):
        self.objects = [FakeObj(n) for n in names]
        self.next_start_with = next_start_with


class FakePar:
    def __init__(self, pid, name, object_name, access_type, time_expires):
        self.id = pid
        self.name = name
        self.object_name = object_name
        self.access_type = access_type
        self.time_expires = time_expires
        self.access_uri = f"/p/token-{pid}/n/ns/b/bkt/o/{object_name}"
        self.full_path = f"https://objectstorage.example.com{self.access_uri}"


class Resp:
    """`oci.response.Response` と同じ形。

    実 SDK は `opc-next-page` ヘッダから `next_page` を組み立てる
    （`oci/response.py`: `self.next_page = self.headers.get(HEADER_NEXT_PAGE)`）ので、
    fake も**ヘッダ由来**にして実物とずれないようにする。
    """

    def __init__(self, data, next_page=None):
        self.data = data
        self.headers = {"opc-next-page": next_page} if next_page else {}
        self.next_page = self.headers.get("opc-next-page")

    @property
    def has_next_page(self):
        return self.next_page is not None


class HeadResp:
    """`head_object` の応答。中身は無く、素性は**ヘッダ**に入る(実 SDK と同じ)。"""

    def __init__(self, headers):
        self.data = None
        self.headers = headers


class FakeOS:
    """put/delete/list と PAR 発行だけを持つ最小の Object Storage。"""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}
        self.sizes: dict[str, int] = {}
        self.pars: dict[str, FakePar] = {}
        self._par_seq = 0
        self.list_calls = 0

    def get_namespace(self):
        return Resp("ns")

    def put_object(self, ns, bucket, name, body, **kw):
        data = body.read() if hasattr(body, "read") else body
        self.objects[name] = data
        if kw.get("content_type"):
            self.content_types[name] = kw["content_type"]
        return Resp(None)

    def delete_object(self, ns, bucket, name):
        self.objects.pop(name, None)
        self.content_types.pop(name, None)
        self.sizes.pop(name, None)
        return Resp(None)

    def head_object(self, ns, bucket, name, **kw):
        """実物の素性(存在・サイズ・Content-Type)。無ければ実 SDK と同じ 404 を投げる。

        `sizes` に値があればそれを返す —— 上限超過(500MB 超)の検証を、実際に
        500MB を確保せずに書けるようにするため。
        """
        if name not in self.objects:
            raise oci.exceptions.ServiceError(404, "ObjectNotFound", {}, "not found")
        size = self.sizes.get(name, len(self.objects[name]))
        return HeadResp({
            "content-length": str(size),
            "content-type": self.content_types.get(name, "application/octet-stream"),
        })

    def list_objects(self, ns, bucket, prefix=None, fields=None, start=None, **kw):
        self.list_calls += 1
        names = sorted(n for n in self.objects if n.startswith(prefix or ""))
        if start:
            names = [n for n in names if n >= start]
        # ページングを1件ずつに刻んで、呼び出し側が全件辿ることを検証する
        if len(names) > 1:
            return Resp(FakeListObjects(names[:1], next_start_with=names[1]))
        return Resp(FakeListObjects(names))

    def create_preauthenticated_request(self, ns, bucket, details, **kw):
        self._par_seq += 1
        pid = str(self._par_seq)
        par = FakePar(
            pid, details.name, details.object_name,
            details.access_type, details.time_expires,
        )
        self.pars[pid] = par
        return Resp(par)

    def list_preauthenticated_requests(self, ns, bucket, object_name_prefix=None,
                                       page=None, **kw):
        hits = [
            p for p in self.pars.values()
            if p.object_name.startswith(object_name_prefix or "")
        ]
        # **1 ページ 1 件に刻む。** 実 API はページングするので、1 ページ目しか
        # 消さない実装をここで落とせるようにする
        start = int(page or 0)
        window = hits[start:start + 1]
        nxt = str(start + 1) if len(hits) > start + 1 else None
        return Resp(window, next_page=nxt)

    def delete_preauthenticated_request(self, ns, bucket, par_id, **kw):
        # **実サービスと同じく、無い PAR の削除は 404**。黙って成功にすると、
        # 同時実行で「もう消えていた」場合の扱いを単体で検証できない
        if par_id not in self.pars:
            raise oci.exceptions.ServiceError(
                404, "PreauthenticatedRequestNotFound", {}, "not found"
            )
        self.pars.pop(par_id)
        return Resp(None)


# --- fake ADB -----------------------------------------------------------------


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self.rows: list[tuple] = []
        self.rowcount = 0

    def execute(self, sql, **binds):
        s = " ".join(sql.split())
        if s.startswith("INSERT INTO video_assets"):
            if self.db["fail_insert"]:
                raise RuntimeError("insert failed")
            row = dict(binds)
            # upload_state は SQL の中の定数。**行に写して**以後の絞り込みに効かせる
            # (無視すると「一覧に出さない」「回収する」の検証が素通りする)
            row["up"] = "uploading" if "'uploading'" in s else "ready"
            row["created"] = self.db["now"]
            self.db["assets"].append(row)
        elif s.startswith("SELECT") and "FROM video_assets" in s:
            rows = [
                a for a in self.db["assets"]
                if a["o"] == binds["o"] and ("id" not in binds or a["id"] == binds["id"])
            ]
            if ":ready = 0" in s:
                # object_name_for の `ready_only`。**この形のときは下の一律の
                # 絞り込みを掛けない** —— 同じ SQL に "upload_state = 'ready'" が
                # 含まれるので、掛けると削除(uploading も対象)まで効いてしまう
                if binds.get("ready"):
                    rows = [a for a in rows if a["up"] == "ready"]
            elif "upload_state = 'ready'" in s:
                rows = [a for a in rows if a["up"] == "ready"]
            elif "upload_state = 'uploading'" in s:
                rows = [a for a in rows if a["up"] == "uploading"]
            if "created_at < :cut" in s:
                rows = [a for a in rows if a["created"] < binds["cut"]]
            rows.sort(key=lambda a: a["id"], reverse=True)
            if "id, object_name FROM video_assets" in s:
                self.rows = [(a["id"], a["obj"]) for a in rows]
            elif "object_name, upload_state FROM video_assets" in s:
                self.rows = [(a["obj"], a["up"]) for a in rows]
            elif "object_name FROM video_assets" in s:
                self.rows = [(a["obj"],) for a in rows]
            elif "SELECT upload_state FROM video_assets" in s:
                self.rows = [(a["up"],) for a in rows]
            else:
                self.rows = [(
                    a["id"], a["t"], "2026-08-19T22:00:00", a["dur"],
                    a["coll"], a["cat"], a["rights"], a["captured"],
                    "pending", None, None, a["obj"], None, None,
                ) for a in rows]
        elif s.startswith("SELECT") and "FROM video_scenes" in s:
            self.rows = list(self.db["scenes"])
        elif s.startswith("UPDATE video_assets SET analysis_state = 'running'"):
            # 分析の権利取り(claim_analysis)。**`upload_state = 'ready'` の条件を
            # 効かせる** —— 無視すると「本体がまだ無い映像を分析できない」の検証が
            # 素通りする(取れてしまい、UploadIncompleteError が出ない)
            hit = [
                a for a in self.db["assets"]
                if a["id"] == binds["id"] and a["o"] == binds["o"]
                and a["up"] == "ready"
            ]
            self.rowcount = len(hit)
        elif s.startswith("UPDATE video_assets SET upload_state = 'ready'"):
            hit = [
                a for a in self.db["assets"]
                if a["id"] == binds["id"] and a["o"] == binds["o"]
                and a["up"] == "uploading"
            ]
            for a in hit:
                a["up"] = "ready"
            self.rowcount = len(hit)
        elif s.startswith("DELETE FROM video_assets"):
            before = len(self.db["assets"])
            self.db["assets"] = [
                a for a in self.db["assets"]
                if not (
                    a["id"] == binds["id"] and a["o"] == binds["o"]
                    and ("upload_state = 'uploading'" not in s or a["up"] == "uploading")
                )
            ]
            self.rowcount = before - len(self.db["assets"])
        else:  # 想定外の SQL を黙って成功させない
            raise AssertionError(f"unexpected SQL: {s[:80]}")

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeConn:
    def __init__(self, db):
        self.db = db

    def cursor(self):
        return FakeCursor(self.db)

    def commit(self):
        pass


@pytest.fixture()
def env(monkeypatch):
    db = {
        "assets": [], "scenes": [], "fail_insert": False,
        # 登録時刻。回収(reap_stale_uploads)の検証で「古い行」を作るために動かす
        "now": datetime.now(UTC).replace(tzinfo=None),
    }
    os_client = FakeOS()

    @contextlib.contextmanager
    def fake_connect():
        yield FakeConn(db)

    monkeypatch.setattr(video, "connect", fake_connect)
    monkeypatch.setattr(video, "os_client", lambda: os_client)
    monkeypatch.setattr(video, "require_bucket", lambda: "jetuse-loop-video")
    return {"db": db, "os": os_client}


def _upload(env, name="clip.mp4", owner=OWNER, **meta):
    return video.create_asset(
        owner, name, io.BytesIO(b"fake-mp4-bytes"), 14, **meta
    )


# --- 登録 ---------------------------------------------------------------------


def test_create_puts_body_in_object_storage_and_row_in_db(env):
    asset = _upload(env, title="現場の記録", collection="設備点検")
    assert asset["analysis_state"] == "pending"
    assert asset["title"] == "現場の記録"
    # 本体は Object Storage。DB には位置だけ
    obj = asset["object_name"]
    assert obj.startswith(f"video/{OWNER}/{asset['id']}/")
    assert env["os"].objects[obj] == b"fake-mp4-bytes"
    assert env["os"].content_types[obj] == "video/mp4"
    assert env["db"]["assets"][0]["obj"] == obj


def test_create_defaults_title_to_filename(env):
    assert _upload(env, name="drone-01.mov")["title"] == "drone-01.mov"


def test_create_removes_object_when_db_insert_fails(env):
    env["db"]["fail_insert"] = True
    with pytest.raises(RuntimeError):
        _upload(env)
    # 台帳に載らない本体を残さない(誰からも辿れないゴミになる)
    assert env["os"].objects == {}


# --- 一覧・詳細・所有者分離 ---------------------------------------------------


def test_list_and_get_are_scoped_to_owner(env):
    mine = _upload(env, name="mine.mp4")
    theirs = _upload(env, name="theirs.mp4", owner=OTHER)

    assert [a["id"] for a in video.list_assets(OWNER)] == [mine["id"]]
    assert [a["id"] for a in video.list_assets(OTHER)] == [theirs["id"]]
    assert video.get_asset(OWNER, mine["id"])["id"] == mine["id"]
    # 他人の映像は「見えない」(403 ではなく存在しない扱い)
    assert video.get_asset(OWNER, theirs["id"]) is None


def test_get_asset_includes_scenes(env):
    asset = _upload(env)
    assert video.get_asset(OWNER, asset["id"])["scenes"] == []


def test_get_asset_returns_every_scene_column(env):
    """場面の全列が詳細から取れること（列位置のずれも落とす）。"""
    asset = _upload(env)
    env["db"]["scenes"].append((
        "sc1", 0, 5000, "傘を差した人物", '["雨"]', '["傘"]', '{"count": 1}',
        '["話している"]', "駅前", "屋外", "outdoor", "day", "rain",
        "ニュース速報", "video/o/thumb.jpg", "ai", "2026-08-19T10:00:00Z",
    ))
    scene = video.get_asset(OWNER, asset["id"])["scenes"][0]
    assert scene == {
        "id": "sc1", "start_ms": 0, "end_ms": 5000, "description": "傘を差した人物",
        "tags": '["雨"]', "objects": '["傘"]', "people": '{"count": 1}',
        "actions": '["話している"]', "place": "駅前", "scene_kind": "屋外",
        "indoor": "outdoor", "time_of_day": "day", "weather": "rain",
        "screen_text": "ニュース速報", "thumb_object": "video/o/thumb.jpg",
        "source": "ai", "confirmed_at": "2026-08-19T10:00:00Z",
    }


# --- 削除 ---------------------------------------------------------------------


def test_delete_removes_db_row_objects_and_pars(env):
    asset = _upload(env)
    # サムネイル等、同じ映像に属する別オブジェクトも消えること
    env["os"].objects[f"video/{OWNER}/{asset['id']}/thumb/0.jpg"] = b"jpg"
    # 再生のたびに PAR が増える。**ページをまたいでも**消えること
    for _ in range(3):
        video.playback_url(OWNER, asset["id"])
    assert len(env["os"].pars) == 3

    assert video.delete_asset(OWNER, asset["id"]) is True
    assert env["os"].objects == {}      # 残骸を残さない
    assert env["os"].pars == {}         # 期限内の PAR も残さない
    assert env["db"]["assets"] == []


def test_delete_rejects_other_owner(env):
    theirs = _upload(env, owner=OTHER)
    assert video.delete_asset(OWNER, theirs["id"]) is False
    # 他人の本体には触れない
    assert env["os"].objects


def test_delete_keeps_row_when_object_delete_fails(env, monkeypatch):
    asset = _upload(env)

    def boom(*a, **kw):
        raise RuntimeError("object storage down")

    monkeypatch.setattr(env["os"], "delete_object", boom)
    with pytest.raises(RuntimeError):
        video.delete_asset(OWNER, asset["id"])
    # 台帳を先に消すと本体が辿れなくなる。行が残っていれば再試行できる
    assert env["db"]["assets"]


# --- PAR(再生 URL) ------------------------------------------------------------


def test_playback_url_is_time_limited(env):
    asset = _upload(env)
    before = datetime.now(UTC)
    res = video.playback_url(OWNER, asset["id"])
    par = next(iter(env["os"].pars.values()))

    # 読み取りだけ。書き込みできる PAR を再生用に配らない
    assert par.access_type == "ObjectRead"
    assert res["url"] == par.full_path
    expires = datetime.fromisoformat(res["expires_at"])
    assert expires > before
    assert expires <= before + timedelta(seconds=video.PLAYBACK_TTL_SECONDS + 5)
    # 無期限にしない
    assert par.time_expires is not None


def test_playback_ttl_is_bounded(env):
    asset = _upload(env)
    with pytest.raises(ValueError):
        video.playback_url(OWNER, asset["id"], ttl_seconds=0)
    with pytest.raises(ValueError):
        video.playback_url(OWNER, asset["id"], ttl_seconds=video.PLAYBACK_TTL_MAX + 1)


def test_playback_url_rejects_other_owner(env):
    theirs = _upload(env, owner=OTHER)
    assert video.playback_url(OWNER, theirs["id"]) is None
    assert env["os"].pars == {}


# --- ルート -------------------------------------------------------------------


@pytest.fixture()
def routed(env):
    """ルート経由。バケット設定済みとして扱う(依存を上書きする)。"""
    app.dependency_overrides[require_video] = lambda: None
    yield env
    app.dependency_overrides.pop(require_video, None)


def test_route_roundtrip(routed):
    res = client.post(
        "/api/video/assets",
        files={"file": ("clip.mp4", b"fake-mp4-bytes", "video/mp4")},
        data={"collection": "設備点検", "captured_at": "2026-08-19T10:00:00"},
    )
    assert res.status_code == 200
    aid = res.json()["id"]

    listed = client.get("/api/video/assets").json()["assets"]
    assert [a["id"] for a in listed] == [aid]
    assert client.get(f"/api/video/assets/{aid}").json()["collection"] == "設備点検"

    play = client.get(f"/api/video/assets/{aid}/playback").json()
    assert play["url"].startswith("https://")
    assert play["expires_at"]

    assert client.delete(f"/api/video/assets/{aid}").json() == {"deleted": True}
    assert client.get(f"/api/video/assets/{aid}").status_code == 404
    assert client.delete(f"/api/video/assets/{aid}").status_code == 404
    assert client.get(f"/api/video/assets/{aid}/playback").status_code == 404


def test_route_rejects_bad_uploads(routed):
    bad_ext = client.post(
        "/api/video/assets", files={"file": ("a.txt", b"x", "text/plain")}
    )
    assert bad_ext.status_code == 422
    empty = client.post(
        "/api/video/assets", files={"file": ("a.mp4", b"", "video/mp4")}
    )
    assert empty.status_code == 422


def test_route_rejects_unparsable_captured_at(routed):
    # 撮影日時は推測で埋めない(specs/20 §1)。読めない値は静かに NULL にせず 422
    res = client.post(
        "/api/video/assets",
        files={"file": ("a.mp4", b"x", "video/mp4")},
        data={"captured_at": "昨日"},
    )
    assert res.status_code == 422


def test_route_503_when_bucket_not_configured(monkeypatch):
    """VIDEO_BUCKET 未設定は 500 ではなく 503(未設定と故障を混ぜない)。"""
    import service.deps as deps

    monkeypatch.setattr(deps, "get_settings", lambda: SimpleNamespace(video_bucket=""))
    assert client.get("/api/video/assets").status_code == 503


# --- 時刻の扱い / 列幅（review-2 指摘 VID-01-003 / VID-01-004） ----------------


def test_captured_at_is_normalized_to_utc(env):
    """オフセット付きの入力は UTC へ寄せて保存する(TIMESTAMP はタイムゾーンを持たない)。"""
    jst = datetime(2026, 8, 19, 19, 0, tzinfo=timezone(timedelta(hours=9)))
    utc = datetime(2026, 8, 19, 10, 0, tzinfo=UTC)
    naive = datetime(2026, 8, 19, 10, 0)

    for given in (jst, utc, naive):
        env["db"]["assets"].clear()
        asset = _upload(env, captured_at=given)
        stored = env["db"]["assets"][0]["captured"]
        assert stored == naive, f"{given!r} -> {stored!r}"
        assert stored.tzinfo is None
        # 応答は「どの時間帯の値か」が判る表記で返す
        assert asset["captured_at"] == "2026-08-19T10:00:00Z"


def test_route_accepts_offset_and_z_forms(routed):
    for raw, expect in (
        ("2026-08-19T19:00:00+09:00", "2026-08-19T10:00:00Z"),
        ("2026-08-19T10:00:00Z", "2026-08-19T10:00:00Z"),
        ("2026-08-19T10:00:00", "2026-08-19T10:00:00Z"),
    ):
        res = client.post(
            "/api/video/assets",
            files={"file": ("a.mp4", b"x", "video/mp4")},
            data={"captured_at": raw},
        )
        assert res.status_code == 200
        assert res.json()["captured_at"] == expect, raw


def test_metadata_is_truncated_once_and_returned_as_stored(env):
    """POST の応答と DB の中身を食い違わせない(直後の GET で値が変わらない)。"""
    asset = _upload(
        env, title="あ" * 600, collection="い" * 300, category="う" * 300,
        rights="え" * 1200,
    )
    row = env["db"]["assets"][0]
    assert (asset["title"], asset["collection"], asset["category"], asset["rights"]) == (
        row["t"], row["coll"], row["cat"], row["rights"]
    )
    assert (len(asset["title"]), len(asset["collection"]), len(asset["rights"])) == (
        500, 255, 1000
    )
    # 空文字は「値なし」に寄せる(空文字は eq 一致を壊す)
    assert _upload(env, collection="")["collection"] is None


# --- アップロードの境界（review-4 指摘 VID-01-012） ---------------------------


def test_upload_size_boundaries(routed, monkeypatch):
    """上限ちょうどは通し、1 バイト超過は 413。空は 422。

    multipart 経路の上限は **`MULTIPART_MAX_BYTES`**(ゲートウェイの本文上限に合わせた値)。
    `MAX_BYTES`(500MB)ではない —— そちらは本文を通さない直接アップロードの上限。
    """
    import service.routes.video as video_routes

    monkeypatch.setattr(video_routes.video_repo, "MULTIPART_MAX_BYTES", 16)
    assert client.post(
        "/api/video/assets", files={"file": ("a.mp4", b"x" * 16, "video/mp4")}
    ).status_code == 200
    assert client.post(
        "/api/video/assets", files={"file": ("a.mp4", b"x" * 17, "video/mp4")}
    ).status_code == 413
    assert client.post(
        "/api/video/assets", files={"file": ("a.mp4", b"", "video/mp4")}
    ).status_code == 422


def test_upload_measures_size_when_client_omits_it(routed, monkeypatch):
    """`UploadFile.size` を持たないクライアントでも、末尾 seek で測って先頭へ戻す。"""
    import service.routes.video as video_routes

    monkeypatch.setattr(
        video_routes.UploadFile, "size", property(lambda self: None), raising=False
    )
    res = client.post(
        "/api/video/assets", files={"file": ("a.mp4", b"0123456789", "video/mp4")}
    )
    assert res.status_code == 200
    assert res.json()["bytes"] == 10
    # 先頭へ戻していないと本体が欠ける
    assert routed["os"].objects[res.json()["object_name"]] == b"0123456789"


# --- 直接アップロード（VID-07 / specs/20 §2） --------------------------------
#
# ゲートウェイの本文上限は **20 MiB**(2026-08-20 実測。
# `runs/2026-08-20T2223_VID-07/e2e/gateway-body-limit.md`)。4K の素材はそこに入らないので、
# 本体は「書き込み専用 PAR へブラウザが直接 PUT する」経路で入れる。ここで守るのは
# (a) 配る鍵が書き込み専用・短命・オブジェクト単位であること、
# (b) **実物を検証してから**確定すること、(c) 中断した登録が回収されること。


def _ticket(env, name="big.mp4", size=200 * 1024 * 1024, owner=OWNER, **meta):
    return video.create_upload_url(owner, name, size, **meta)


def _put(env, ticket, body=b"x" * 32, content_type=None):
    """ブラウザの PUT を模す(PAR 経由で本体が置かれた状態を作る)。"""
    env["os"].objects[ticket["object_name"]] = body
    env["os"].content_types[ticket["object_name"]] = (
        content_type if content_type is not None else ticket["content_type"]
    )


def test_upload_url_par_is_write_only_object_scoped_and_short_lived(env):
    """配る鍵の性質。**読み書き両用・長命・バケット単位にしない**(tasks/VID-07 禁止事項)。"""
    before = datetime.now(UTC)
    ticket = _ticket(env)
    (par,) = env["os"].pars.values()

    assert par.access_type == "ObjectWrite"          # 書き込み専用(読めない)
    assert par.object_name == ticket["object_name"]  # この映像 1 個だけ
    assert par.name.startswith("jetuse-video-upload-")
    # 短命。既定は 1 時間で、天井を超える寿命は作らない
    assert par.time_expires <= before + timedelta(seconds=video.UPLOAD_TTL_SECONDS + 5)
    assert par.time_expires > before
    assert ticket["upload_url"].startswith("https://")
    assert ticket["content_type"] == "video/mp4"
    assert ticket["max_bytes"] == video.MAX_BYTES


def test_upload_url_ttl_is_bounded(env):
    """天井を超える寿命は**黙って縮めず拒否する**(playback_url と同じ流儀)。"""
    for bad in (0, -1, video.UPLOAD_TTL_SECONDS + 1):
        with pytest.raises(ValueError):
            video.create_upload_url(OWNER, "a.mp4", 100, ttl_seconds=bad)
    assert env["os"].pars == {}


def test_upload_url_rejects_sizes_outside_the_real_limit(env):
    for bad in (0, -1, video.MAX_BYTES + 1):
        with pytest.raises(ValueError):
            video.create_upload_url(OWNER, "a.mp4", bad)
    assert env["db"]["assets"] == []


def test_uploading_asset_is_hidden_until_it_is_confirmed(env):
    """**本体の無い行を一覧に出さない。** 開いても再生も分析もできない映像が並ぶ。"""
    ticket = _ticket(env)
    assert video.list_assets(OWNER) == []
    # 再生 URL も出さない(期限まで 404 を返す URL を配らない)
    assert video.playback_url(OWNER, ticket["id"]) is None
    # 分析も入口で止める。**404 ではなく「まだ上がっていない」**(行は在る)
    with pytest.raises(video.UploadIncompleteError):
        video.claim_analysis(OWNER, ticket["id"])

    _put(env, ticket)
    video.complete_upload(OWNER, ticket["id"])
    assert [a["id"] for a in video.list_assets(OWNER)] == [ticket["id"]]


def test_complete_verifies_the_real_object_and_removes_the_upload_par(env):
    ticket = _ticket(env)
    _put(env, ticket, body=b"y" * 1024)

    asset = video.complete_upload(OWNER, ticket["id"])

    assert asset["id"] == ticket["id"]
    assert asset["bytes"] == 1024                    # 実物から測った値
    assert env["db"]["assets"][0]["up"] == "ready"
    # **使い回しを塞ぐ。** 確定後にこの URL へ書けてはいけない
    assert env["os"].pars == {}
    # 本体は消さない(確定したのだから残る)
    assert ticket["object_name"] in env["os"].objects


def test_complete_closes_the_write_path_before_it_verifies(env):
    """**見る前に書き込み口を閉じる**(review-1 VID07-003)。

    PAR を消すのが検証の後だと、`head_object` で確かめてから台帳を `ready` にするまでの
    隙間に、まだ生きている同じ PAR で中身を差し替えられる —— 確かめた実物と確定した
    実物が別のものになり、検証そのものが意味を失う。
    """
    ticket = _ticket(env)
    _put(env, ticket, body=b"y" * 64)

    pars_when_verified = []
    real_head = env["os"].head_object

    def spy_head(*args, **kw):
        pars_when_verified.append(dict(env["os"].pars))
        return real_head(*args, **kw)

    env["os"].head_object = spy_head
    video.complete_upload(OWNER, ticket["id"])

    # 検証した時点で、この映像へ書ける PAR は 1 つも残っていない
    assert pars_when_verified and pars_when_verified[0] == {}


def test_complete_tolerates_a_par_that_was_already_closed(env):
    """同時実行で**相手が先に PAR を消していても**確定は成功する(review-3 VID07-009)。

    両方が同じ PAR を数えてから消しにいくので、後から消したほうは 404 を受け取る。
    それを失敗にすると、登録は成功しているのに 500 を返すことになる。
    """
    ticket = _ticket(env)
    _put(env, ticket, body=b"z" * 256)
    real_list = env["os"].list_preauthenticated_requests

    def list_then_someone_else_deletes(*args, **kw):
        res = real_list(*args, **kw)
        env["os"].pars.clear()          # 相手の complete が先に消した
        return res

    env["os"].list_preauthenticated_requests = list_then_someone_else_deletes

    asset = video.complete_upload(OWNER, ticket["id"])

    assert asset["id"] == ticket["id"]
    assert asset["bytes"] == 256
    assert env["db"]["assets"][0]["up"] == "ready"


def test_complete_fails_closed_when_the_write_path_cannot_be_closed(env):
    """PAR を消せなかったら**確定しない**。閉じられないまま確かめても保証にならない。"""
    ticket = _ticket(env)
    _put(env, ticket)

    def boom(*args, **kw):
        raise RuntimeError("PAR delete failed")

    env["os"].delete_preauthenticated_request = boom

    with pytest.raises(RuntimeError):
        video.complete_upload(OWNER, ticket["id"])
    # 台帳は uploading のまま = やり直せる(中途半端に ready にしない)
    assert env["db"]["assets"][0]["up"] == "uploading"


def test_abandon_leaves_alone_what_it_could_not_claim(env):
    """**行を取れなかったら本体に触らない**(review-1 VID07-004)。

    回収(`reap_stale_uploads`)が古い行を拾った直後に complete が通ると、行はもう
    `ready`。ここで本体を消すと「台帳にはあるが本体が無い映像」が残る。
    """
    ticket = _ticket(env)
    _put(env, ticket)
    env["db"]["assets"][0]["up"] = "ready"      # 先に確定された

    assert video._abandon_upload(OWNER, ticket["id"], ticket["object_name"]) is False
    assert ticket["object_name"] in env["os"].objects   # 確定済みの本体は残る
    assert len(env["db"]["assets"]) == 1


def test_reap_reports_only_what_it_actually_reclaimed(env):
    """拾った後に確定された行は**回収した数に入れない**(触っていない)。"""
    ticket = _ticket(env)
    _put(env, ticket)
    env["db"]["assets"][0]["created"] = (
        datetime.now(UTC).replace(tzinfo=None)
        - timedelta(seconds=video.UPLOAD_STALE_SECONDS + 60)
    )
    real_abandon = video._abandon_upload

    def confirm_then_abandon(owner, asset_id, object_name):
        env["db"]["assets"][0]["up"] = "ready"   # SELECT の後、DELETE の前に確定
        return real_abandon(owner, asset_id, object_name)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(video, "_abandon_upload", confirm_then_abandon)
    try:
        assert video.reap_stale_uploads(OWNER) == []
    finally:
        monkey.undo()
    assert ticket["object_name"] in env["os"].objects
    assert len(env["db"]["assets"]) == 1


def test_complete_is_idempotent(env):
    """応答が届かずに再送されるのは正常な経路。成功した登録を 4xx に見せない。"""
    ticket = _ticket(env)
    _put(env, ticket)
    first = video.complete_upload(OWNER, ticket["id"])
    again = video.complete_upload(OWNER, ticket["id"])
    assert again["id"] == first["id"]
    assert env["db"]["assets"][0]["up"] == "ready"


@pytest.mark.parametrize(
    ("body", "content_type", "size_override", "hint"),
    [
        (b"", None, None, "空"),                       # サイズ 0
        (b"x" * 32, "application/zip", None, "種別"),   # 別 Content-Type
        (b"x" * 32, None, video.MAX_BYTES + 1, "大き"),  # 上限超過
    ],
)
def test_complete_rejects_tampered_objects_and_cleans_up(
    env, body, content_type, size_override, hint
):
    """**「入れたと言われたから入った」ことにしない。** 落ちたらオブジェクトごと片付ける。"""
    ticket = _ticket(env)
    _put(env, ticket, body=body, content_type=content_type)
    if size_override is not None:
        env["os"].sizes[ticket["object_name"]] = size_override

    with pytest.raises(video.UploadVerificationError) as e:
        video.complete_upload(OWNER, ticket["id"])
    assert hint in str(e.value)  # 理由を残す(「失敗しました」では上げ直しようがない)

    # オブジェクト・PAR・台帳の行が揃って消えている(中途半端な登録を残さない)
    assert ticket["object_name"] not in env["os"].objects
    assert env["os"].pars == {}
    assert env["db"]["assets"] == []


def test_concurrent_complete_does_not_delete_the_confirmed_body(env, monkeypatch):
    """complete が同時に 2 回走っても、**確定した本体を消さない**。

    二度押しや応答の再送で起こる。負けた側から見ると行は既に `ready` で、
    そこを「行が消えた」と同じ扱いにすると、勝った側が確定した映像の本体を片付けてしまう
    (利用者から見れば、登録に成功した直後に映像が消える)。
    """
    ticket = _ticket(env)
    _put(env, ticket, body=b"y" * 512)

    # 検証を終えて UPDATE を打つ直前に、別の実行が確定した状況を作る
    real_head = env["os"].head_object

    def head_then_confirm(*args, **kw):
        res = real_head(*args, **kw)
        env["db"]["assets"][0]["up"] = "ready"   # 相手が先に確定した
        return res

    monkeypatch.setattr(env["os"], "head_object", head_then_confirm)

    asset = video.complete_upload(OWNER, ticket["id"])

    assert asset["id"] == ticket["id"]
    assert asset["bytes"] == 512
    assert ticket["object_name"] in env["os"].objects   # **本体は残る**
    assert env["db"]["assets"][0]["up"] == "ready"


def test_complete_rejects_when_nothing_was_uploaded(env):
    """PUT せずに complete を呼ぶ(申告だけで確定させようとする)。"""
    ticket = _ticket(env)
    with pytest.raises(video.UploadVerificationError):
        video.complete_upload(OWNER, ticket["id"])
    assert env["db"]["assets"] == []
    assert env["os"].pars == {}


def test_complete_rejects_other_owner(env):
    """他人の登録は確定できない。**存在の有無も漏らさない**(404 相当の LookupError)。"""
    ticket = _ticket(env)
    _put(env, ticket)
    with pytest.raises(LookupError):
        video.complete_upload(OTHER, ticket["id"])
    assert env["db"]["assets"][0]["up"] == "uploading"


def test_upload_par_is_removed_when_the_row_cannot_be_created(env):
    """行を作れなければ**誰も使えない書き込み口を残さない**。"""
    env["db"]["fail_insert"] = True
    with pytest.raises(RuntimeError):
        _ticket(env)
    assert env["os"].pars == {}


def test_stale_uploads_are_reaped_but_fresh_ones_are_left_alone(env):
    """中断した登録は回収する。**まだ上げている最中の行は消さない**。"""
    old = _ticket(env, name="interrupted.mp4")
    _put(env, old, body=b"partial")           # 途中まで上がった本体
    fresh = _ticket(env, name="in-flight.mp4")
    # **古くするのは 2 本目を作った後**。登録の入口でも回収が走るので(VID07-006)、
    # 先に古くすると 2 本目の発行時に回収されてしまい、この検査は素通りする
    env["db"]["assets"][0]["created"] = (
        datetime.now(UTC).replace(tzinfo=None)
        - timedelta(seconds=video.UPLOAD_STALE_SECONDS + 60)
    )

    reclaimed = video.reap_stale_uploads(OWNER)

    assert reclaimed == [old["id"]]
    assert old["object_name"] not in env["os"].objects   # 途中の本体も片付く
    assert [a["id"] for a in env["db"]["assets"]] == [fresh["id"]]
    # 上げている最中の PAR は残す(消すと上げ切った直後の complete が 404 になる)
    assert [p.object_name for p in env["os"].pars.values()] == [fresh["object_name"]]


def test_registering_again_reclaims_your_own_interrupted_upload(env):
    """**一度も分析しない利用者でも回収される**(review-2 VID07-006)。

    回収を分析の前段だけに置くと、分析を使わない人の中断分は永久に残る(途中まで
    上がった大きな本体と、上げようのない行)。登録をやり直す人は必ず入口を通るので、
    そこでも回す。
    """
    stale = _ticket(env, name="interrupted.mp4")
    _put(env, stale, body=b"partial")
    env["db"]["assets"][0]["created"] = (
        datetime.now(UTC).replace(tzinfo=None)
        - timedelta(seconds=video.UPLOAD_STALE_SECONDS + 60)
    )

    fresh = _ticket(env, name="retry.mp4")     # 分析は一度も呼ばない

    assert [a["id"] for a in env["db"]["assets"]] == [fresh["id"]]
    assert stale["object_name"] not in env["os"].objects


def test_registering_again_survives_a_failing_reap(env, monkeypatch):
    """片付けが転んでも**新しい登録は通す**(片付けの失敗で登録できなくならない)。"""
    monkeypatch.setattr(
        video, "reap_stale_uploads",
        lambda owner: (_ for _ in ()).throw(RuntimeError("list failed")),
    )
    ticket = _ticket(env)
    assert ticket["upload_url"].startswith("https://")


def test_reap_leaves_confirmed_assets_alone(env):
    """確定した映像は回収の対象にしない(古くても消えては困る)。"""
    ticket = _ticket(env)
    _put(env, ticket)
    video.complete_upload(OWNER, ticket["id"])
    env["db"]["assets"][0]["created"] = datetime(2020, 1, 1)

    assert video.reap_stale_uploads(OWNER) == []
    assert len(env["db"]["assets"]) == 1


def test_reap_is_scoped_to_the_owner(env):
    """他人の中断した登録には触らない。"""
    theirs = _ticket(env, owner=OTHER)
    env["db"]["assets"][0]["created"] = datetime(2020, 1, 1)
    assert video.reap_stale_uploads(OWNER) == []
    assert [a["id"] for a in env["db"]["assets"]] == [theirs["id"]]


def test_deleting_an_unconfirmed_asset_still_works(env):
    """登録を途中でやめた利用者が自分で消せること(回収待ちにしない)。"""
    ticket = _ticket(env)
    _put(env, ticket)
    assert video.delete_asset(OWNER, ticket["id"]) is True
    assert env["db"]["assets"] == []
    assert env["os"].objects == {}


# --- 直接アップロードのルート ------------------------------------------------


def test_upload_url_route_roundtrip(routed):
    res = client.post(
        "/api/video/assets/upload-url",
        json={"filename": "big.mp4", "size_bytes": 300 * 1024 * 1024,
              "collection": "設備点検", "captured_at": "2026-08-19T10:00:00"},
    )
    assert res.status_code == 200
    ticket = res.json()
    assert ticket["upload_url"].startswith("https://")
    assert ticket["content_type"] == "video/mp4"
    # 確定するまで一覧には出ない
    assert client.get("/api/video/assets").json()["assets"] == []

    _put(routed, ticket, body=b"z" * 4096)
    done = client.post(f"/api/video/assets/{ticket['id']}/complete")
    assert done.status_code == 200
    assert done.json()["bytes"] == 4096
    assert done.json()["collection"] == "設備点検"
    assert [a["id"] for a in client.get("/api/video/assets").json()["assets"]] == [
        ticket["id"]
    ]


def test_upload_url_route_rejects_bad_input(routed):
    """拡張子は multipart 経路と同じ規則。上限超過は**発行前に** 413。"""
    bad_ext = client.post(
        "/api/video/assets/upload-url", json={"filename": "a.txt", "size_bytes": 10}
    )
    assert bad_ext.status_code == 422

    too_big = client.post(
        "/api/video/assets/upload-url",
        json={"filename": "a.mp4", "size_bytes": video.MAX_BYTES + 1},
    )
    assert too_big.status_code == 413
    assert "500MB" in too_big.json()["detail"]
    # 上限ちょうどとの差が丸めで消えないよう、実数も出す
    assert f"{video.MAX_BYTES + 1:,}" in too_big.json()["detail"]

    # 誤字を無視して題名なしで登録しない(extra="forbid")
    typo = client.post(
        "/api/video/assets/upload-url",
        json={"filename": "a.mp4", "size_bytes": 10, "titel": "x"},
    )
    assert typo.status_code == 422
    assert routed["os"].pars == {}


def test_complete_route_maps_failures_to_reasons(routed):
    missing = client.post("/api/video/assets/does-not-exist/complete")
    assert missing.status_code == 404

    ticket = client.post(
        "/api/video/assets/upload-url",
        json={"filename": "a.mp4", "size_bytes": 10},
    ).json()
    _put(routed, ticket, body=b"")           # 0 バイト
    res = client.post(f"/api/video/assets/{ticket['id']}/complete")
    assert res.status_code == 422
    assert "空" in res.json()["detail"]       # 理由をそのまま見せる


def test_analyze_route_rejects_unconfirmed_upload(routed):
    """本体がまだ無い映像の分析は 409。**404 にしない**(行は在る)。"""
    ticket = client.post(
        "/api/video/assets/upload-url",
        json={"filename": "a.mp4", "size_bytes": 10},
    ).json()
    res = client.post(f"/api/video/assets/{ticket['id']}/analyze")
    assert res.status_code == 409
    assert "アップロード" in res.json()["detail"]


def test_multipart_limit_tells_the_truth_about_the_gateway(routed):
    """**実態と違う案内をしない**(tasks/VID-07 禁止事項)。

    ゲートウェイの本文上限(20 MiB)より小さい値をアプリの上限にしておかないと、
    413 の理由は利用者に届かない(ゲートウェイが先に切るため)。
    """
    assert video.MULTIPART_MAX_BYTES < video.GATEWAY_MAX_BODY_BYTES
    assert video.GATEWAY_MAX_BODY_BYTES == 20 * 1024 * 1024  # 実測値

    res = client.post(
        "/api/video/assets",
        files={"file": ("a.mp4", b"x" * (video.MULTIPART_MAX_BYTES + 1), "video/mp4")},
    )
    assert res.status_code == 413
    detail = res.json()["detail"]
    assert "500MB" not in detail.split("直接アップロード")[0]  # 嘘の上限を先に出さない
    assert "20MB" in detail and "直接アップロード" in detail
    # **丸めた値だけにしない。** 境界の 1 バイト超過は「20MB までです(送られたのは
    # 20MB)」に丸まって読めなくなる(実機でそう出た)。実数を必ず添える
    assert f"{video.MULTIPART_MAX_BYTES:,}" in detail
    assert f"{video.MULTIPART_MAX_BYTES + 1:,}" in detail
