"""VID-01 映像の保管と登録。Object Storage と ADB は fake で置き換え、
登録・一覧・詳細・削除 / PAR の期限 / 所有者分離を検証する。
"""

import contextlib
import io
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

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


class FakeOS:
    """put/delete/list と PAR 発行だけを持つ最小の Object Storage。"""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}
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
        return Resp(None)

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
        self.pars.pop(par_id, None)
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
            self.db["assets"].append(dict(binds))
        elif s.startswith("SELECT") and "FROM video_assets" in s:
            rows = [
                a for a in self.db["assets"]
                if a["o"] == binds["o"] and ("id" not in binds or a["id"] == binds["id"])
            ]
            rows.sort(key=lambda a: a["id"], reverse=True)
            if "object_name FROM video_assets" in s:
                self.rows = [(a["obj"],) for a in rows]
            else:
                self.rows = [(
                    a["id"], a["t"], "2026-08-19T22:00:00", a["dur"],
                    a["coll"], a["cat"], a["rights"], a["captured"],
                    "pending", None, None, a["obj"], None, None,
                ) for a in rows]
        elif s.startswith("SELECT") and "FROM video_scenes" in s:
            self.rows = list(self.db["scenes"])
        elif s.startswith("DELETE FROM video_assets"):
            before = len(self.db["assets"])
            self.db["assets"] = [
                a for a in self.db["assets"]
                if not (a["id"] == binds["id"] and a["o"] == binds["o"])
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
    db = {"assets": [], "scenes": [], "fail_insert": False}
    os_client = FakeOS()

    @contextlib.contextmanager
    def fake_connect():
        yield FakeConn(db)

    monkeypatch.setattr(video, "connect", fake_connect)
    monkeypatch.setattr(video, "_os_client", lambda: os_client)
    monkeypatch.setattr(video, "_require_bucket", lambda: "jetuse-loop-video")
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
    """上限ちょうどは通し、1 バイト超過は 413。空は 422。"""
    import service.routes.video as video_routes

    monkeypatch.setattr(video_routes.video_repo, "MAX_BYTES", 16)
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
