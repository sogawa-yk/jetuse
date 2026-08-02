"""外部HTTPツール(TOOL-01)のテスト。

DB は fake(connect の差し替え)で、**検証ロジックとSSRFガードは実関数**を通す。
HTTP は httpx.MockTransport で差し替え、代理実行の境界(タイムアウト・サイズ上限・
リダイレクト・秘密ヘッダ)を実コードで確かめる。
"""

import contextlib
import json
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

import jetuse_core.chat as chat_mod
from jetuse_core import http_tools
from jetuse_core.tools import TOOLS, ToolError, execute_with
from jetuse_core.webtools import SsrfBlockedError
from service.main import app

client = TestClient(app)

SCHEMA = {
    "type": "object",
    "properties": {"part_number": {"type": "string", "description": "品番"}},
    "required": ["part_number"],
}


def tool_row(**over) -> dict:
    row = {
        "id": "t1", "name": "lookup_stock", "description": "在庫を引く",
        "parameters": SCHEMA, "url": "https://example.com/stock",
        "method": "GET", "auth_header": "Authorization", "auth_secret_ocid": None,
        "owner_sub": "u1",
    }
    row.update(over)
    return row


# --- DB fake -----------------------------------------------------------------

def _raw_headers(value):
    """`extra_headers` 列の中身。str はそのまま = 壊れた CLOB の再現に使える。"""
    if not value:
        return None
    return value if isinstance(value, str) else json.dumps(value)


class FakeCursor:
    def __init__(self, store: list[dict]):
        self.store = store
        self.rows: list[tuple] = []
        self.rowcount = 0
        self.last: tuple[str, dict] = ("", {})

    def execute(self, sql, **binds):
        self.last = (sql, binds)
        if sql.strip().startswith("SELECT"):
            self.rows = [
                (
                    t["id"], t["name"], t["description"], json.dumps(t["parameters"]),
                    t["url"], t["method"], t["auth_header"], t["auth_secret_ocid"],
                    t["owner"],
                    # TOOL-02: 既存行は両方 NULL（列を足す前に登録されたツール）。
                    # str をそのまま置けるのは「DB の CLOB を直接書き換えられた」状態の再現
                    _raw_headers(t.get("headers")),
                    t.get("idempotency_header"),
                )
                for t in self.store
                if t["owner"] == binds["o"]
                and (
                    "n" not in binds or t["name"] == binds["n"]
                )
                and (
                    not any(k.startswith("id") for k in binds)
                    or t["id"] in [v for k, v in binds.items() if k.startswith("id")]
                )
            ]
        elif sql.strip().startswith("INSERT"):
            if any(t["owner"] == binds["o"] and t["name"] == binds["n"]
                   for t in self.store):
                import oracledb
                raise oracledb.IntegrityError("ORA-00001: unique constraint violated")
            self.store.append({
                "id": binds["id"], "owner": binds["o"], "name": binds["n"],
                "description": binds["d"], "parameters": json.loads(binds["p"]),
                "url": binds["u"], "method": binds["m"], "auth_header": binds["h"],
                "auth_secret_ocid": binds["a"],
                "headers": json.loads(binds["x"]) if binds["x"] else None,
                "idempotency_header": binds["i"],
            })
        elif sql.strip().startswith("DELETE"):
            before = len(self.store)
            self.store[:] = [
                t for t in self.store
                if not (t["id"] == binds["id"] and t["owner"] == binds["o"])
            ]
            self.rowcount = before - len(self.store)

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeConn:
    def __init__(self, store):
        self.store = store

    def cursor(self):
        return FakeCursor(self.store)

    def commit(self):
        pass


@pytest.fixture()
def store(monkeypatch):
    rows: list[dict] = []

    @contextlib.contextmanager
    def fake_connect():
        yield FakeConn(rows)

    monkeypatch.setattr(http_tools, "connect", fake_connect)
    return rows


# --- 1. 登録できるURLの制限(SSRF・fail-closed) --------------------------------

@pytest.mark.parametrize("url", [
    "http://api.example.com/x",             # https でない
    "https://169.254.169.254/latest/meta",  # インスタンスメタデータ
    "https://127.0.0.1/x",                  # ループバック
    "https://10.0.0.5/x",                   # 私有レンジ
    "https://[::1]/x",                      # IPv6 ループバック
    "https://user:pw@api.example.com/x",    # URL に認証情報
])
def test_validate_url_rejects_dangerous(url):
    with pytest.raises(SsrfBlockedError):
        http_tools.validate_url(url)


def test_validate_url_accepts_public_https():
    http_tools.validate_url("https://objectstorage.ap-osaka-1.oraclecloud.com/x")


def test_create_tool_rejects_metadata_url_before_touching_db(store):
    with pytest.raises(SsrfBlockedError):
        http_tools.create_tool(
            "u1", "meta", "メタデータ", SCHEMA, "https://169.254.169.254/opc/v2/"
        )
    assert store == []


def test_route_rejects_metadata_url(store):
    res = client.post("/api/agent/http-tools", json={
        "name": "meta_probe", "description": "x", "parameters": SCHEMA,
        "url": "https://169.254.169.254/opc/v2/instance/",
    })
    assert res.status_code == 400
    assert store == []


# --- 2. 定義の検証 -------------------------------------------------------------

@pytest.mark.parametrize("name", ["web_search", "rag_search", "code_interpreter"])
def test_reserved_names_rejected(name):
    with pytest.raises(http_tools.HttpToolDefError):
        http_tools.validate_definition(name, "https://example.com/x", "GET", None)


@pytest.mark.parametrize("name", ["A_bad", "ab", "1abc", "has space", ""])
def test_bad_names_rejected(name):
    with pytest.raises(http_tools.HttpToolDefError):
        http_tools.validate_definition(name, "https://example.com/x", "GET", None)


@pytest.mark.parametrize("header", ["Host", "content-length", "Transfer-Encoding"])
def test_routing_headers_cannot_be_used_for_auth(header):
    """Host を認証ヘッダにできると IP ピン留めが指す origin を利用者に動かされる。"""
    with pytest.raises(http_tools.HttpToolDefError):
        http_tools.validate_definition("ok_tool", "https://example.com/x", "GET", header)


def test_host_header_is_fixed_to_the_validated_origin(monkeypatch):
    """登録時の禁止をすり抜けても、実行時に Host は検証済みのホストで上書きされる。"""
    monkeypatch.setattr(http_tools, "_read_secret", lambda ocid: "tok-abc")
    allow_secret(monkeypatch)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["host"] = request.headers.get("host")
        return httpx.Response(200, json={"ok": True})

    _mock(monkeypatch, handler)
    http_tools.call_tool(
        tool_row(auth_header="Host", auth_secret_ocid="ocid1.vaultsecret.oc1..x"),
        {"part_number": "X"})
    assert seen["host"] == "example.com"


@pytest.mark.parametrize("url", [
    "https://example.com:abc/x",   # ポートが数値でない(実行時 ValueError にしない)
    "https://example.com:0/x",     # 0 は接続先を 443 に化けさせる
    "https://example.com:99999/x",
])
def test_bad_port_rejected(url):
    with pytest.raises(SsrfBlockedError):
        http_tools.validate_url(url)


def test_method_and_header_validated():
    with pytest.raises(http_tools.HttpToolDefError):
        http_tools.validate_definition("ok_tool", "https://example.com/x", "DELETE", None)
    with pytest.raises(http_tools.HttpToolDefError):
        http_tools.validate_definition("ok_tool", "https://example.com/x", "GET", "bad header")
    assert http_tools.validate_definition(
        "ok_tool", "https://example.com/x", "post", "X-Api-Key"
    ) == ("POST", "X-Api-Key")


@pytest.mark.parametrize("params", [
    {"type": "array"},
    {"type": "object", "properties": {"a": {"type": "object"}}},
    {"type": "object", "properties": {"has space": {"type": "string"}}},
    {"type": "object", "properties": {}, "required": ["missing"]},
    {"type": "object", "properties": {}, "required": [{}]},  # 非文字列 → 500 にしない
    "not-a-dict",
])
def test_bad_parameter_schema_rejected(params):
    with pytest.raises(http_tools.HttpToolDefError):
        http_tools.validate_parameters(params)


def test_unsupported_schema_keywords_are_dropped():
    """検証できないキーワードをモデルへ渡さない(制約に見えて実行前検証が素通しになる)。"""
    out = http_tools.validate_parameters({
        "type": "object",
        "properties": {"a": {"type": "string", "enum": ["x"], "description": "説明"}},
        "required": ["a"],
    })
    assert out["properties"]["a"] == {"type": "string", "description": "説明"}


# --- 3. 秘密の扱い -------------------------------------------------------------

def allow_secret(monkeypatch, owner="u1"):
    """Vault 側の認可(freeform タグ)を通ったことにする。認可そのものは別テストで検証。"""
    def check(o, ocid):
        if o != owner:
            raise http_tools.HttpToolDefError("この秘密の利用が許可されていません")
    monkeypatch.setattr(http_tools, "assert_secret_usable", check)


def test_secret_requires_owner_tag_in_vault(monkeypatch):
    """他人/無関係の秘密 OCID を指定しても登録できない(confused deputy の遮断)。"""
    class Meta:
        compartment_id = "ocid1.compartment.oc1..app"
        freeform_tags = {"jetuse_tool_owner": "someone-else"}

    class FakeVaults:
        def __init__(self, **kw):
            pass

        def get_secret(self, ocid):
            return type("R", (), {"data": Meta()})()

    import oci

    from jetuse_core import oci_auth
    monkeypatch.setattr(oci.vault, "VaultsClient", FakeVaults)
    monkeypatch.setattr(oci_auth, "sdk_signer_args", lambda region=None: {"config": {}})
    from jetuse_core.settings import get_settings
    monkeypatch.setattr(get_settings(), "compartment_ocid",
                        "ocid1.compartment.oc1..app", raising=False)
    with pytest.raises(http_tools.HttpToolDefError) as e:
        http_tools.assert_secret_usable("u1", "ocid1.vaultsecret.oc1..other")
    assert "許可されていません" in str(e.value)
    # タグが一致すれば通る
    Meta.freeform_tags = {"jetuse_tool_owner": "u1"}
    http_tools.assert_secret_usable("u1", "ocid1.vaultsecret.oc1..mine")
    # 別コンパートメントの秘密は拒否
    Meta.compartment_id = "ocid1.compartment.oc1..other"
    with pytest.raises(http_tools.HttpToolDefError):
        http_tools.assert_secret_usable("u1", "ocid1.vaultsecret.oc1..mine")


def test_missing_compartment_setting_is_fail_closed(monkeypatch):
    """照合先が無いまま通すと『タグさえ合えば他コンパートメントも可』になる。"""
    from jetuse_core.settings import get_settings

    monkeypatch.setattr(get_settings(), "compartment_ocid", "", raising=False)
    with pytest.raises(http_tools.HttpToolDefError) as e:
        http_tools.assert_secret_usable("u1", "ocid1.vaultsecret.oc1..x")
    assert "COMPARTMENT_OCID" in str(e.value)


def test_secret_lookup_failure_is_fail_closed(monkeypatch):
    class Boom:
        def __init__(self, **kw):
            pass

        def get_secret(self, ocid):
            raise RuntimeError("403 NotAuthorized")

    import oci

    from jetuse_core import oci_auth
    from jetuse_core.settings import get_settings
    monkeypatch.setattr(oci.vault, "VaultsClient", Boom)
    monkeypatch.setattr(oci_auth, "sdk_signer_args", lambda region=None: {"config": {}})
    monkeypatch.setattr(get_settings(), "compartment_ocid",
                        "ocid1.compartment.oc1..app", raising=False)
    with pytest.raises(http_tools.HttpToolDefError):
        http_tools.assert_secret_usable("u1", "ocid1.vaultsecret.oc1..x")


def test_secret_never_in_db_columns_or_api_response(store, monkeypatch):
    monkeypatch.setattr(http_tools, "_read_secret", lambda ocid: "SUPER-SECRET-VALUE")
    allow_secret(monkeypatch)
    ocid = "ocid1.vaultsecret.oc1..aaaa"
    created = http_tools.create_tool(
        "u1", "lookup_stock", "在庫を引く", SCHEMA, "https://example.com/stock",
        auth_header="X-Api-Key", auth_secret_ocid=ocid,
    )
    assert created["has_auth"] is True
    # API 応答には OCID も秘密も出ない
    assert "auth_secret_ocid" not in created
    assert "SUPER-SECRET-VALUE" not in json.dumps(created, ensure_ascii=False)
    # DB に入るのは OCID のみ(平文の秘密列を持たない)
    assert store[0]["auth_secret_ocid"] == ocid
    assert "SUPER-SECRET-VALUE" not in json.dumps(store, ensure_ascii=False)
    # 一覧も同様
    listed = http_tools.list_tools("u1")
    assert listed[0]["has_auth"] is True and "auth_secret_ocid" not in listed[0]


def test_secret_header_sent_but_not_returned(monkeypatch):
    seen = {}
    monkeypatch.setattr(http_tools, "_read_secret", lambda ocid: "tok-abc")
    allow_secret(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        seen["header"] = request.headers.get("x-api-key")
        return httpx.Response(200, json={"authenticated": True})

    _mock(monkeypatch, handler)
    out = http_tools.call_tool(
        tool_row(auth_header="X-Api-Key", auth_secret_ocid="ocid1.vaultsecret..x"),
        {"part_number": "JX-7742"},
    )
    assert seen["header"] == "tok-abc"
    assert "tok-abc" not in out


def test_reflected_secret_is_redacted_from_the_response(monkeypatch):
    """相手が認証ヘッダを応答へ反射しても、秘密がモデル/UI/会話履歴へ流れない。"""
    monkeypatch.setattr(http_tools, "_read_secret", lambda ocid: "tok-abc")
    allow_secret(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        # エコー系エンドポイントの挙動(受け取ったヘッダをそのまま返す)
        return httpx.Response(200, json={"headers": dict(request.headers)})

    _mock(monkeypatch, handler)
    out = http_tools.call_tool(
        tool_row(auth_header="X-Api-Key", auth_secret_ocid="ocid1.vaultsecret.oc1..x"),
        {"part_number": "X"},
    )
    assert "tok-abc" not in out
    assert http_tools.REDACTED in out  # 反射された箇所が伏せ字になっている


def test_reflected_secret_is_redacted_from_error_bodies(monkeypatch):
    monkeypatch.setattr(http_tools, "_read_secret", lambda ocid: "tok-abc")
    allow_secret(monkeypatch)
    _mock(monkeypatch, lambda req: httpx.Response(500, text="bad token tok-abc"))
    with pytest.raises(http_tools.HttpToolCallError) as e:
        http_tools.call_tool(
            tool_row(auth_secret_ocid="ocid1.vaultsecret.oc1..x"), {"part_number": "X"})
    assert "tok-abc" not in str(e.value)


def test_secret_authorization_is_rechecked_at_execution(monkeypatch):
    """登録後に Vault のタグを外したら、その場で使えなくなる(権限剥奪が効く)。"""
    monkeypatch.setattr(http_tools, "_read_secret", lambda ocid: "tok-abc")
    called = {"n": 0}

    def revoked(owner, ocid):
        called["n"] += 1
        raise http_tools.HttpToolDefError("この秘密の利用が許可されていません")

    monkeypatch.setattr(http_tools, "assert_secret_usable", revoked)
    _mock(monkeypatch, lambda req: httpx.Response(200, json={"ok": True}))
    with pytest.raises(http_tools.HttpToolCallError) as e:
        http_tools.call_tool(
            tool_row(auth_secret_ocid="ocid1.vaultsecret.oc1..x"), {"part_number": "X"})
    assert called["n"] == 1 and "許可されていません" in str(e.value)


def test_no_auth_header_when_no_secret(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"ok": True})

    _mock(monkeypatch, handler)
    http_tools.call_tool(tool_row(), {"part_number": "X"})
    assert seen["auth"] is None


# --- 4. 代理実行の境界 ---------------------------------------------------------

def _mock(monkeypatch, handler):
    monkeypatch.setattr(
        http_tools, "_client",
        lambda: httpx.Client(
            transport=httpx.MockTransport(handler), follow_redirects=False
        ),
    )


def test_get_passes_args_as_query(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        return httpx.Response(200, json={"stock": 137})

    _mock(monkeypatch, handler)
    out = http_tools.call_tool(tool_row(), {"part_number": "JX-7742"})
    assert seen["method"] == "GET" and "part_number=JX-7742" in seen["url"]
    assert json.loads(out)["status"] == 200
    assert "137" in json.loads(out)["body"]


def test_post_passes_args_as_json_body(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    _mock(monkeypatch, handler)
    http_tools.call_tool(tool_row(method="POST"), {"part_number": "JX-7742"})
    assert seen["body"] == {"part_number": "JX-7742"}


def test_oversized_response_fails_not_truncated(monkeypatch):
    big = b"x" * (http_tools.MAX_RESPONSE_BYTES + 1024)
    _mock(monkeypatch, lambda req: httpx.Response(200, content=big))
    with pytest.raises(http_tools.HttpToolCallError) as e:
        http_tools.call_tool(tool_row(), {"part_number": "X"})
    assert "上限" in str(e.value)


def test_declared_oversize_is_rejected_before_reading(monkeypatch):
    """長さを申告しているなら 1 バイトも読まずに断る。"""
    read = {"n": 0}

    def body():
        read["n"] += 1
        yield b"x" * 1024

    def handler(request):
        return httpx.Response(
            200, headers={"content-length": str(http_tools.MAX_RESPONSE_BYTES + 1)},
            content=body(),  # ジェネレータ = 読まれたときだけ本文が生成される
        )

    _mock(monkeypatch, handler)
    with pytest.raises(http_tools.HttpToolCallError) as e:
        http_tools.call_tool(tool_row(), {"part_number": "X"})
    assert "上限" in str(e.value) and read["n"] == 0


def test_compressed_bomb_is_capped_on_decoded_bytes(monkeypatch):
    """小さい gzip が巨大に展開されても、展開後のバイト数で上限に掛かる。"""
    import gzip

    payload = gzip.compress(b"x" * (http_tools.MAX_RESPONSE_BYTES * 20))
    assert len(payload) < 10_000  # 送られてくる量はごく小さい
    _mock(monkeypatch, lambda req: httpx.Response(
        200, content=payload, headers={"content-encoding": "gzip"}))
    with pytest.raises(http_tools.HttpToolCallError) as e:
        http_tools.call_tool(tool_row(), {"part_number": "X"})
    assert "上限" in str(e.value)


def test_identity_encoding_is_requested(monkeypatch):
    seen = {}

    def handler(request):
        seen["ae"] = request.headers.get("accept-encoding")
        return httpx.Response(200, json={"ok": True})

    _mock(monkeypatch, handler)
    http_tools.call_tool(tool_row(), {"part_number": "X"})
    assert seen["ae"] == "identity"


def test_timeout_fails_and_is_not_retried(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ReadTimeout("too slow", request=request)

    _mock(monkeypatch, handler)
    with pytest.raises(http_tools.HttpToolCallError) as e:
        http_tools.call_tool(tool_row(), {"part_number": "X"})
    assert "タイムアウト" in str(e.value)
    assert calls["n"] == 1  # リトライしない


def test_redirect_is_not_followed(monkeypatch):
    def handler(request):
        return httpx.Response(302, headers={"location": "https://169.254.169.254/"})

    _mock(monkeypatch, handler)
    with pytest.raises(http_tools.HttpToolCallError) as e:
        http_tools.call_tool(tool_row(), {"part_number": "X"})
    assert "リダイレクト" in str(e.value)


@pytest.mark.parametrize("status", [300, 301, 302, 304, 307, 308])
def test_all_3xx_rejected_even_without_location(monkeypatch, status):
    """`is_redirect` は Location 付きしか真にならない。3xx はすべて失敗にする。"""
    _mock(monkeypatch, lambda req: httpx.Response(status))
    with pytest.raises(http_tools.HttpToolCallError) as e:
        http_tools.call_tool(tool_row(), {"part_number": "X"})
    assert "リダイレクト" in str(e.value)


def test_connects_to_the_validated_ip(monkeypatch):
    """検証したIPへ接続する(DNSリバインディング=検証後に内部へ向け直す手を塞ぐ)。"""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["host_in_url"] = request.url.host
        seen["host_header"] = request.headers.get("host")
        seen["sni"] = request.extensions.get("sni_hostname")
        return httpx.Response(200, json={"ok": True})

    _mock(monkeypatch, handler)
    http_tools.call_tool(tool_row(), {"part_number": "X"})
    # URL のホストは IP リテラルに差し替わり、Host ヘッダと SNI は元のホスト名のまま
    import ipaddress
    ipaddress.ip_address(seen["host_in_url"])
    assert seen["host_header"] == "example.com" and seen["sni"] == "example.com"


def test_pin_target_rejects_internal_resolution(monkeypatch):
    """名前解決の結果が1つでも内部を指したら接続前に止める。"""
    monkeypatch.setattr(
        http_tools.socket, "getaddrinfo",
        lambda *a, **k: [
            (2, 1, 6, "", ("93.184.216.34", 443)),
            (2, 1, 6, "", ("169.254.169.254", 443)),  # 混ぜられた内部アドレス
        ],
    )
    with pytest.raises(SsrfBlockedError):
        http_tools._pin_target("https://example.com/x")


@pytest.mark.parametrize("encode", [
    lambda s: s,
    lambda s: json.dumps(s)[1:-1],
    lambda s: __import__("urllib.parse", fromlist=["quote"]).quote(s, safe=""),
    lambda s: __import__("base64").b64encode(s.encode()).decode(),
])
def test_reflected_secret_redacted_in_common_encodings(monkeypatch, encode):
    """相手が反射する形は生とは限らない(JSON エスケープ・URL エンコード・Base64)。"""
    secret = 'tok"a\\b/c value'
    monkeypatch.setattr(http_tools, "_read_secret", lambda ocid: secret)
    allow_secret(monkeypatch)
    _mock(monkeypatch, lambda req: httpx.Response(
        200, content=f'{{"echo": "{encode(secret)}"}}'.encode()))
    out = http_tools.call_tool(
        tool_row(auth_secret_ocid="ocid1.vaultsecret.oc1..x"), {"part_number": "X"})
    assert encode(secret) not in out and http_tools.REDACTED in out


def test_error_status_reported_as_failure(monkeypatch):
    _mock(monkeypatch, lambda req: httpx.Response(503, text="upstream down"))
    with pytest.raises(http_tools.HttpToolCallError) as e:
        http_tools.call_tool(tool_row(), {"part_number": "X"})
    assert "HTTP 503" in str(e.value)


def test_execution_revalidates_host(monkeypatch):
    """登録後にURLが内部を向いても実行時に止まる(fail-closed)。"""
    _mock(monkeypatch, lambda req: httpx.Response(200, json={"leaked": True}))
    # 実行時の失敗は種別を問わず HttpToolCallError に揃える(呼び出し側の扱いを分岐させない)
    with pytest.raises(http_tools.HttpToolCallError) as e:
        http_tools.call_tool(tool_row(url="https://169.254.169.254/opc/v2/"), {})
    assert "許可しない宛先" in str(e.value)


def test_vault_read_failure_is_a_tool_failure(monkeypatch):
    def boom(ocid):
        raise RuntimeError("vault down")

    monkeypatch.setattr(http_tools, "_read_secret", boom)
    allow_secret(monkeypatch)
    _mock(monkeypatch, lambda req: httpx.Response(200, json={"ok": True}))
    with pytest.raises(http_tools.HttpToolCallError) as e:
        http_tools.call_tool(
            tool_row(auth_secret_ocid="ocid1.vaultsecret.oc1..x"), {"part_number": "X"})
    assert "Vault" in str(e.value)


# --- 5. 引数検証(ToolDef 経由) ------------------------------------------------

def test_tooldef_args_validated_before_call(monkeypatch):
    called = {"n": 0}

    def handler(request):
        called["n"] += 1
        return httpx.Response(200, json={"ok": True})

    _mock(monkeypatch, handler)
    registry = {**TOOLS, "lookup_stock": http_tools.to_tooldef(tool_row())}
    with pytest.raises(ToolError):  # 必須引数なし
        execute_with(registry, "lookup_stock", "{}")
    with pytest.raises(ToolError):  # 未知の引数
        execute_with(registry, "lookup_stock", json.dumps({"part_number": "a", "x": 1}))
    with pytest.raises(ToolError):  # 型不正
        execute_with(registry, "lookup_stock", json.dumps({"part_number": 1}))
    assert called["n"] == 0
    assert json.loads(execute_with(
        registry, "lookup_stock", json.dumps({"part_number": "a"})))["status"] == 200


def test_numeric_and_boolean_types_validated(monkeypatch):
    schema = {"type": "object", "properties": {
        "n": {"type": "integer"}, "b": {"type": "boolean"}}, "required": []}
    registry = {"t": http_tools.to_tooldef(tool_row(name="t", parameters=schema))}
    with pytest.raises(ToolError):
        execute_with(registry, "t", json.dumps({"n": "1"}))
    with pytest.raises(ToolError):
        execute_with(registry, "t", json.dumps({"b": 1}))
    with pytest.raises(ToolError):
        execute_with(registry, "t", json.dumps({"n": True}))  # bool は integer でない
    with pytest.raises(ToolError):
        execute_with(registry, "t", json.dumps({"n": 1.5}))   # 小数は integer でない
    num = {"type": "object", "properties": {"x": {"type": "number"}}, "required": []}
    numreg = {"t": http_tools.to_tooldef(tool_row(name="t", parameters=num))}
    for bad in ("NaN", "Infinity", "-Infinity"):
        with pytest.raises(ToolError):
            execute_with(numreg, "t", f'{{"x": {bad}}}')
    # 巨大整数は number として妥当(ここで OverflowError にしない)
    _mock(monkeypatch, lambda req: httpx.Response(200, json={"ok": True}))
    assert json.loads(
        execute_with(numreg, "t", f'{{"x": {10**400}}}'))["status"] == 200


# --- 6. エージェント実行への配線 -----------------------------------------------

def test_build_agent_tools_includes_external_alongside_builtin():
    specs = chat_mod._build_agent_tools(
        ["web_search"], None, False, None, [http_tools.to_tooldef(tool_row())]
    )
    names = [s.get("name") for s in specs if s["type"] == "function"]
    assert "web_search" in names and "lookup_stock" in names
    ext = next(s for s in specs if s.get("name") == "lookup_stock")
    assert ext["description"] == "在庫を引く" and ext["parameters"] == SCHEMA


def test_stream_agent_executes_external_tool(monkeypatch):
    """モデルが外部ツールを呼んだら JetUse がサーバー側で代理実行して結果を返す。"""
    _mock(monkeypatch, lambda req: httpx.Response(200, json={"stock": 137}))
    sent: list[dict] = []

    class Item:
        type = "function_call"
        name = "lookup_stock"
        arguments = '{"part_number": "JX-7742"}'
        call_id = "c1"

        def model_dump(self, exclude_none=False):
            return {"type": self.type, "name": self.name,
                    "arguments": self.arguments, "call_id": self.call_id}

    class Ev:
        type = "response.output_item.done"
        item = Item()

    class Stream:
        def __init__(self, events):
            self.events = events

        def __iter__(self):
            return iter(self.events)

        def close(self):
            pass

    hops = {"n": 0}

    class FakeResponses:
        def create(self, **kw):
            sent.append(kw)
            hops["n"] += 1
            return Stream([Ev()] if hops["n"] == 1 else [])

    class FakeClient:
        responses = FakeResponses()

    monkeypatch.setattr(chat_mod, "make_inference_client", lambda **kw: FakeClient())
    events = list(chat_mod.stream_agent(
        "gpt-oss-120b", [{"role": "user", "content": "JX-7742の在庫は?"}],
        auto_tools=True, http_tools=[http_tools.to_tooldef(tool_row())],
    ))
    # ツール仕様がモデルへ渡っている
    assert any(t.get("name") == "lookup_stock" for t in sent[0]["tools"])
    # 実行結果がモデルへ返っている
    outputs = [i for i in sent[1]["input"] if i.get("type") == "function_call_output"]
    assert outputs and "137" in outputs[0]["output"]
    assert any(e.get("tool_result", {}).get("name") == "lookup_stock" for e in events)


def test_builtin_wins_on_name_collision(monkeypatch):
    """万一同名が登録されていても組込ツールが上書きされない(登録時にも予約名は拒否)。"""
    rogue = http_tools.to_tooldef(tool_row(name="web_search"))
    registry = {**{rogue.name: rogue}, **TOOLS}
    assert registry["web_search"] is TOOLS["web_search"]


# --- 7. ルート(所有者強制・上限) ----------------------------------------------

def test_crud_is_owner_scoped(store):
    created = client.post("/api/agent/http-tools", json={
        "name": "lookup_stock", "description": "在庫を引く", "parameters": SCHEMA,
        "url": "https://example.com/stock",
    }).json()
    assert client.get("/api/agent/http-tools").json()["tools"][0]["id"] == created["id"]
    # 別所有者の行は SQL の WHERE 句で見えない → 削除は 404
    store[0]["owner"] = "someone-else"
    assert client.delete(f"/api/agent/http-tools/{created['id']}").status_code == 404


def test_duplicate_name_rejected(store):
    body = {"name": "lookup_stock", "description": "x", "parameters": SCHEMA,
            "url": "https://example.com/stock"}
    assert client.post("/api/agent/http-tools", json=body).status_code == 200
    assert client.post("/api/agent/http-tools", json=body).status_code == 400


def test_too_many_tools_per_agent_rejected():
    over = [f"id{i}" for i in range(http_tools.MAX_TOOLS_PER_AGENT + 1)]
    res = client.post("/api/chat/stream", json={
        "model": "gpt-oss-120b", "agent": True, "http_tool_ids": over,
        "messages": [{"role": "user", "content": "x"}],
    })
    assert res.status_code == 422


def test_unresolvable_tool_id_is_404_not_silently_dropped(store):
    """他人所有・削除済み・不正idを黙って外すと、業務APIを見ずに答えてしまう。"""
    created = client.post("/api/agent/http-tools", json={
        "name": "lookup_stock", "description": "在庫を引く", "parameters": SCHEMA,
        "url": "https://example.com/stock",
    }).json()
    store[0]["owner"] = "someone-else"
    res = client.post("/api/chat/stream", json={
        "model": "gpt-oss-120b", "agent": True, "http_tool_ids": [created["id"]],
        "messages": [{"role": "user", "content": "在庫は?"}],
    })
    assert res.status_code == 404 and "http tool not found" in res.json()["detail"]


def test_get_tools_caps_at_limit(store):
    for i in range(http_tools.MAX_TOOLS_PER_AGENT + 2):
        store.append({
            "id": f"t{i}", "owner": "u1", "name": f"tool_{i}", "description": "x",
            "parameters": SCHEMA, "url": "https://example.com/x", "method": "GET",
            "auth_header": "Authorization", "auth_secret_ocid": None,
        })
    got = http_tools.get_tools("u1", [t["id"] for t in store])
    assert len(got) == http_tools.MAX_TOOLS_PER_AGENT


def test_approval_event_carries_tool_id_and_route_honours_it(store, monkeypatch):
    """承認したその1件を id で名指しする(同名の別ツールへ差し替えられない)。"""
    _mock(monkeypatch, lambda req: httpx.Response(200, json={"stock": 137}))
    created = client.post("/api/agent/http-tools", json={
        "name": "lookup_stock", "description": "在庫を引く", "parameters": SCHEMA,
        "url": "https://example.com/stock",
    }).json()
    # 承認イベントに id が載る
    events = list(chat_mod._emit_pending_approval(
        [{"type": "function_call", "name": "lookup_stock", "arguments": "{}",
          "call_id": "c1"}],
        {"lookup_stock": http_tools.to_tooldef({**tool_row(), "id": created["id"]})},
    ))
    assert events[0]["tool_call"]["http_tool_id"] == created["id"]
    # 承認待ちの間に同名で別 URL のツールへ差し替えられたら 409
    store[0]["name"] = "renamed_tool"
    res = client.post("/api/agent/execute-tool", json={
        "name": "lookup_stock", "arguments": '{"part_number": "X"}',
        "http_tool_id": created["id"]})
    assert res.status_code == 409


def test_execute_tool_route_resolves_owner_http_tool(store, monkeypatch):
    _mock(monkeypatch, lambda req: httpx.Response(200, json={"stock": 137}))
    created = client.post("/api/agent/http-tools", json={
        "name": "lookup_stock", "description": "在庫を引く", "parameters": SCHEMA,
        "url": "https://example.com/stock",
    }).json()
    res = client.post("/api/agent/execute-tool", json={
        "name": "lookup_stock", "arguments": '{"part_number": "JX-7742"}',
        "http_tool_id": created["id"]})
    assert res.status_code == 200 and "137" in res.json()["output"]


def test_unknown_approved_tool_id_is_404(store, monkeypatch):
    """削除済み・他人所有の id は 404(登録の解決と同じ契約)。"""
    res = client.post("/api/agent/execute-tool", json={
        "name": "lookup_stock", "arguments": "{}",
        "http_tool_id": "00000000-0000-0000-0000-000000000000"})
    assert res.status_code == 404


def test_execute_tool_requires_the_approved_tool_id(store, monkeypatch):
    """名前だけでの再解決を許すと、承認待ちの間に同名の別ツールへ差し替えられる。"""
    _mock(monkeypatch, lambda req: httpx.Response(200, json={"stock": 137}))
    client.post("/api/agent/http-tools", json={
        "name": "lookup_stock", "description": "在庫を引く", "parameters": SCHEMA,
        "url": "https://example.com/stock",
    })
    res = client.post("/api/agent/execute-tool", json={
        "name": "lookup_stock", "arguments": '{"part_number": "JX-7742"}'})
    assert res.status_code == 400 and "http_tool_id" in res.json()["detail"]


# --- 8. 固定ヘッダと冪等キー(TOOL-02) ------------------------------------------

def _seen_headers(monkeypatch) -> dict:
    """相手が受け取ったヘッダを覚えるモック。実際に送られた値だけを見る。"""
    seen: dict = {"all": []}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["all"].append(dict(request.headers))
        seen.update(dict(request.headers))
        return httpx.Response(200, json={"ok": True})

    _mock(monkeypatch, handler)
    return seen


def test_fixed_headers_reach_the_remote(monkeypatch):
    """認証以外に必須ヘッダを持つ API を呼べる(このタスクの主目的)。"""
    seen = _seen_headers(monkeypatch)
    http_tools.call_tool(
        tool_row(headers={"X-Correlation-Id": "corr-123", "X-Tenant": "acme"}),
        {"part_number": "X"},
    )
    assert seen["x-correlation-id"] == "corr-123" and seen["x-tenant"] == "acme"


def test_idempotency_key_is_new_on_every_call(monkeypatch):
    """冪等キーは呼び出しごとに JetUse が発行する(モデルに作らせない=使い回させない)。"""
    seen = _seen_headers(monkeypatch)
    tool = tool_row(idempotency_header="X-Idempotency-Key")
    http_tools.call_tool(tool, {"part_number": "A"})
    http_tools.call_tool(tool, {"part_number": "B"})
    keys = [h["x-idempotency-key"] for h in seen["all"]]
    assert len(keys) == 2 and keys[0] != keys[1]
    for k in keys:
        uuid.UUID(k)  # 推測されにくい形式であること


def test_fixed_headers_cannot_override_auth_or_host_at_registration():
    """登録時に禁止する(そのツール自身の認証ヘッダ名も含む)。"""
    for bad in ({"Host": "evil.example"}, {"Authorization": "Bearer x"},
                {"Cookie": "a=b"}, {"Proxy-Authorization": "x"},
                {"Content-Length": "0"}, {"Accept-Encoding": "gzip"},
                {"X-Api-Key": "attacker"}):
        with pytest.raises(http_tools.HttpToolDefError):
            http_tools.validate_extra_headers(bad, None, "X-Api-Key")
    with pytest.raises(http_tools.HttpToolDefError):
        http_tools.validate_extra_headers(None, "Host", "X-Api-Key")
    with pytest.raises(http_tools.HttpToolDefError):
        http_tools.validate_extra_headers(None, "X-Api-Key", "X-Api-Key")
    # 大小無視で判定する
    with pytest.raises(http_tools.HttpToolDefError):
        http_tools.validate_extra_headers({"x-api-KEY": "attacker"}, None, "X-Api-Key")
    with pytest.raises(http_tools.HttpToolDefError):
        http_tools.validate_extra_headers({"HOST": "evil.example"}, None, None)


def test_fixed_headers_are_rejected_at_execution_too(monkeypatch):
    """登録後に DB を直接書き換えられても素通りさせない(fail-closed)。"""
    _mock(monkeypatch, lambda req: httpx.Response(200, json={"ok": True}))
    for bad in ({"Host": "evil.example"}, {"X-Bad": "a\r\nX-Injected: b"},
                {f"X-H{i}": "v" for i in range(http_tools.MAX_EXTRA_HEADERS + 1)}):
        with pytest.raises(http_tools.HttpToolCallError) as e:
            http_tools.call_tool(tool_row(headers=bad), {"part_number": "X"})
        assert "ヘッダ" in str(e.value)


def test_legit_values_win_over_fixed_headers(monkeypatch):
    """検証をすり抜けても、後から入る認証・Host が必ず勝つ(組み立て順の担保)。"""
    monkeypatch.setattr(http_tools, "_read_secret", lambda ocid: "tok-abc")
    allow_secret(monkeypatch)
    # 検証は通ったことにして、組み立て順そのものを確かめる
    monkeypatch.setattr(
        http_tools, "validate_extra_headers",
        lambda h, i, a: ({"Host": "evil.example", "X-Api-Key": "attacker",
                          "X-Idempotency-Key": "fixed-by-attacker"}, "X-Idempotency-Key"),
    )
    seen = _seen_headers(monkeypatch)
    http_tools.call_tool(
        tool_row(auth_header="X-Api-Key", auth_secret_ocid="ocid1.vaultsecret.oc1..x"),
        {"part_number": "X"},
    )
    assert seen["host"] == "example.com"
    assert seen["x-api-key"] == "tok-abc"
    assert seen["x-idempotency-key"] != "fixed-by-attacker"
    uuid.UUID(seen["x-idempotency-key"])


@pytest.mark.parametrize("headers", [
    {"X-Bad": "line1\r\nX-Injected: yes"},          # CRLF インジェクション
    {"X-Bad": "line1\nX-Injected: yes"},
    {"X-Bad": "tab\tseparated"},                    # 制御文字
    {"X-Bad": "日本語"},                             # 非 ASCII
    {"X-Bad": ""},                                  # 空値
    {"X-Bad": "x" * (http_tools.MAX_HEADER_VALUE_LENGTH + 1)},
    {"X-Bad": 1},                                   # 文字列でない
    {"bad header": "v"},                            # ヘッダ名にトークン外文字
    {"X-Dup": "a", "x-dup": "b"},                   # 大小違いの重複
    {f"X-H{i}": "v" for i in range(http_tools.MAX_EXTRA_HEADERS + 1)},  # 個数上限
    "not-a-dict",
])
def test_bad_fixed_headers_rejected(headers):
    with pytest.raises(http_tools.HttpToolDefError):
        http_tools.validate_extra_headers(headers, None, None)


@pytest.mark.parametrize("name", ["bad header", "x" * 64, 5, "X-A\r\nX-B"])
def test_bad_idempotency_header_rejected(name):
    with pytest.raises(http_tools.HttpToolDefError):
        http_tools.validate_extra_headers(None, name, None)


@pytest.mark.parametrize("empty", [None, ""])
def test_unset_idempotency_header_means_no_key(empty):
    assert http_tools.validate_extra_headers(None, empty, None) == ({}, None)


@pytest.mark.parametrize("falsy", [[], False, 0, ""])
def test_falsy_non_dict_headers_are_rejected_not_ignored(falsy):
    """DB を書き換えられたとき、型違いを「未指定」として黙って通さない(fail-closed)。"""
    with pytest.raises(http_tools.HttpToolDefError):
        http_tools.validate_extra_headers(falsy, None, None)


def test_idempotency_header_cannot_duplicate_a_fixed_header():
    with pytest.raises(http_tools.HttpToolDefError):
        http_tools.validate_extra_headers(
            {"X-Idem": "fixed"}, "x-idem", None)


def test_existing_tools_without_the_new_columns_are_unchanged(monkeypatch):
    """両方 NULL の既存ツールは送るヘッダが1つも増えない(回帰)。"""
    seen = _seen_headers(monkeypatch)
    http_tools.call_tool(tool_row(), {"part_number": "X"})
    before = set(seen["all"][0])
    http_tools.call_tool(
        tool_row(headers=None, idempotency_header=None), {"part_number": "X"})
    assert set(seen["all"][1]) == before
    # 冪等キーらしきヘッダが勝手に付かない
    assert not [h for h in before if "idempot" in h]


def test_registration_round_trip_and_values_not_listed(store, monkeypatch):
    """登録 → 保存 → 実行までヘッダが運ばれる。一覧は名前だけ返す(値を返さない)。"""
    created = client.post("/api/agent/http-tools", json={
        "name": "create_order", "description": "発注する", "parameters": SCHEMA,
        "url": "https://example.com/orders", "method": "POST",
        "headers": {"X-Correlation-Id": "corr-123"},
        "idempotency_header": "X-Idempotency-Key",
    }).json()
    assert created["header_names"] == ["X-Correlation-Id"]
    assert created["idempotency_header"] == "X-Idempotency-Key"
    assert "corr-123" not in json.dumps(created, ensure_ascii=False)
    listed = client.get("/api/agent/http-tools").json()["tools"][0]
    assert listed["header_names"] == ["X-Correlation-Id"]
    assert "corr-123" not in json.dumps(listed, ensure_ascii=False)
    # DB から読み直した行では実行に使える
    seen = _seen_headers(monkeypatch)
    row = http_tools.get_tools(store[0]["owner"], [created["id"]])[0]
    http_tools.call_tool(row, {"part_number": "X"})
    assert seen["x-correlation-id"] == "corr-123"
    uuid.UUID(seen["x-idempotency-key"])


def test_route_rejects_forbidden_header_at_registration(store):
    res = client.post("/api/agent/http-tools", json={
        "name": "bad_tool", "description": "x", "parameters": SCHEMA,
        "url": "https://example.com/x",
        "headers": {"Host": "evil.example"},
    })
    assert res.status_code == 400 and store == []


def test_route_rejects_crlf_in_header_value(store):
    res = client.post("/api/agent/http-tools", json={
        "name": "bad_tool", "description": "x", "parameters": SCHEMA,
        "url": "https://example.com/x",
        "headers": {"X-Trace": "a\r\nX-Injected: b"},
    })
    assert res.status_code == 400 and store == []


# --- 9. ヘッダ名の受理範囲と、DB 値が壊れている場合(TOOL-02 review-2) ------------

@pytest.mark.parametrize("name", [
    "X-Trace", "X_Trace", "api.version", "x-trace-9", "1st-header", "X+Y", "a",
])
def test_http_token_header_names_accepted(name):
    """相手の API は `_` や `.`、数字始まりのヘッダ名を要求することがある(RFC 9110 token)。"""
    clean, idem = http_tools.validate_extra_headers({name: "v"}, None, None)
    assert clean == {name: "v"}


@pytest.mark.parametrize("name", [
    "bad header", "X:Trace", "X,Trace", "X\r\nY", "X\x00", "", "x" * 64, "X/Y", '"X"',
])
def test_non_token_header_names_rejected(name):
    with pytest.raises(http_tools.HttpToolDefError):
        http_tools.validate_extra_headers({name: "v"}, None, None)


@pytest.mark.parametrize("raw", [
    "not-json", '{"X-A": "v"', '["X-A"]', "5", '"str"', "null",
    '{"X-A": "' + "v" * http_tools.MAX_HEADERS_JSON_CHARS + '"}',  # 過大な CLOB
])
def test_broken_db_headers_do_not_crash_list_and_block_execution(
        store, monkeypatch, raw):
    """壊れた 1 行で一覧 API が 500 にならず、その行の実行は拒否される(fail-closed)。"""
    seen = _seen_headers(monkeypatch)
    client.post("/api/agent/http-tools", json={
        "name": "lookup_stock", "description": "在庫を引く", "parameters": SCHEMA,
        "url": "https://example.com/stock"})
    store[0]["headers"] = raw       # DB を直接書き換えられた状態(JSON として壊れている)

    listed = client.get("/api/agent/http-tools")
    assert listed.status_code == 200
    assert listed.json()["tools"][0]["header_names"] == []

    row = http_tools.get_tools(store[0]["owner"], [store[0]["id"]])[0]
    with pytest.raises(http_tools.HttpToolCallError) as e:
        http_tools.call_tool(row, {"part_number": "X"})
    assert "ヘッダ" in str(e.value)
    assert seen["all"] == []        # 1 バイトも送っていない


def test_broken_db_headers_are_flagged_not_hidden(store, monkeypatch):
    client.post("/api/agent/http-tools", json={
        "name": "lookup_stock", "description": "在庫を引く", "parameters": SCHEMA,
        "url": "https://example.com/stock"})
    store[0]["headers"] = "not-json"
    listed = client.get("/api/agent/http-tools").json()["tools"][0]
    assert listed["headers_invalid"] is True
    store[0]["headers"] = {"X-Correlation-Id": "corr"}
    ok = client.get("/api/agent/http-tools").json()["tools"][0]
    assert ok["headers_invalid"] is False and ok["header_names"] == ["X-Correlation-Id"]


# --- 10. 入力境界(TOOL-02 review-3) --------------------------------------------

@pytest.mark.parametrize("value", ["value\n", "value\r", "value\r\n"])
def test_trailing_newline_in_header_value_rejected(value):
    """`$` は末尾 LF の直前にも一致する。CR/LF は「途中」だけでなく「末尾」も拒否する。"""
    with pytest.raises(http_tools.HttpToolDefError):
        http_tools.validate_extra_headers({"X-Trace": value}, None, None)


@pytest.mark.parametrize("name", ["X-Trace\n", "X-Trace\r\n"])
def test_trailing_newline_in_header_name_rejected(name):
    with pytest.raises(http_tools.HttpToolDefError):
        http_tools.validate_extra_headers({name: "v"}, None, None)
    with pytest.raises(http_tools.HttpToolDefError):
        http_tools.validate_extra_headers(None, name, None)


def test_max_sized_headers_survive_the_db_round_trip(store, monkeypatch):
    """登録を通った**最大構成**が、DB 読み直しで「壊れた値」にならない。"""
    limit = http_tools.MAX_HEADER_VALUE_LENGTH
    # JSON で最も膨らむ値(引用符とバックスラッシュはエスケープされる)
    worst = ('"\\' * (limit // 2))[:limit]
    headers = {f"X-H{i}-{'x' * 55}": worst for i in range(http_tools.MAX_EXTRA_HEADERS)}
    created = http_tools.create_tool(
        "u1", "lookup_stock", "在庫を引く", SCHEMA, "https://example.com/stock",
        headers=headers)
    assert len(created["header_names"]) == http_tools.MAX_EXTRA_HEADERS
    row = http_tools.get_tools("u1", [created["id"]])[0]
    assert row["headers"] == headers          # 読み直しても壊れた印にならない
    seen = _seen_headers(monkeypatch)
    http_tools.call_tool(row, {"part_number": "X"})
    assert seen[next(iter(headers)).lower()] == worst
