"""connector(L2 MCP)定義スキーマ＋合成バリデーション土台の単体テスト(CON-01)。

`contributes["connector"]` の構造検証(transport/endpoint/auth/actions)と、合成バリデーション
(権限スコープ宣言整合)を網羅する。**認証の実値を持たない**(secret_ref = 参照名のみ)契約も検証する。
"""

import pytest

from jetuse_core.plugins.connector import (
    ConnectorCompositionError,
    ConnectorDefinition,
    ConnectorError,
    connector_json_schema,
    required_permissions,
    validate_connector,
    validate_connector_composition,
)
from jetuse_core.plugins.manifest import (
    SCHEMA_VERSION,
    ManifestError,
    validate_manifest,
)


def _definition(**over) -> dict:
    d = {
        "provider": "slack",
        "transport": "builtin",
        "auth": {"kind": "oauth2", "secretRef": "slack-bot-token", "scopes": ["chat:write"]},
        "actions": [
            {
                "name": "post_message",
                "title": "メッセージ投稿",
                "description": "指定チャンネルへ投稿する",
            },
            {
                "name": "search_messages",
                "title": "メッセージ検索",
                "permissions": ["platform:conversations.read"],
            },
        ],
        "summary": "Slack コネクタ(コア)",
    }
    d.update(over)
    return d


def _manifest(definition=None, permissions=None, **over):
    data = {
        "schemaVersion": SCHEMA_VERSION,
        "id": "jetuse/slack-connector",
        "version": "1.0.0",
        "kind": "connector",
        "name": "Slack コネクタ",
        "publisher": "jetuse",
        "jetuse": {"minVersion": "0.3.0"},
        "permissions": ["platform:conversations.read"]
        if permissions is None
        else permissions,
        "contributes": {"connector": _definition() if definition is None else definition},
    }
    data.update(over)
    return validate_manifest(data)


# --- 正常系 ------------------------------------------------------------------


def test_validate_builtin_connector_ok():
    d = validate_connector(_definition())
    assert isinstance(d, ConnectorDefinition)
    assert d.provider == "slack"
    assert d.transport == "builtin"
    assert d.endpoint is None
    assert [a.name for a in d.actions] == ["post_message", "search_messages"]
    assert d.auth.kind == "oauth2"
    assert d.auth.secret_ref == "slack-bot-token"


def test_validate_mcp_connector_ok():
    d = validate_connector(
        _definition(
            transport="mcp",
            endpoint="https://mcp.example.com/slack",
            provider="teams",
        )
    )
    assert d.transport == "mcp"
    assert d.endpoint == "https://mcp.example.com/slack"


def test_validate_from_manifest_ok():
    d = validate_connector(_manifest())
    assert d.provider == "slack"


def test_auth_none_minimal():
    d = validate_connector(
        _definition(auth={"kind": "none"}, actions=[{"name": "ping", "title": "ping"}])
    )
    assert d.auth.kind == "none"
    assert d.auth.secret_ref is None


def test_roundtrip_by_alias():
    # 配布表現(camelCase: secretRef)で往復できる(保存→取り出しの前提)。
    d = validate_connector(_definition())
    dumped = d.model_dump(by_alias=True)
    assert dumped["auth"]["secretRef"] == "slack-bot-token"
    again = validate_connector(dumped)
    assert again.auth.secret_ref == "slack-bot-token"


# --- 異常系: transport / endpoint -------------------------------------------


def test_mcp_requires_endpoint():
    with pytest.raises(ConnectorError):
        validate_connector(_definition(transport="mcp"))


def test_builtin_forbids_endpoint():
    with pytest.raises(ConnectorError):
        validate_connector(
            _definition(transport="builtin", endpoint="https://mcp.example.com")
        )


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://mcp.example.com",  # https でない
        "https://localhost/mcp",  # localhost
        "https://127.0.0.1/mcp",  # loopback
        "https://10.0.0.5/mcp",  # private
        "https://169.254.1.1/mcp",  # link-local
        "https://[::1]/mcp",  # IPv6 loopback
        "https://[fe80::1]/mcp",  # IPv6 link-local
        "https://[fc00::1]/mcp",  # IPv6 ULA(private)
        "https://[::ffff:127.0.0.1]/mcp",  # IPv4-mapped loopback
        "https://[::ffff:10.0.0.5]/mcp",  # IPv4-mapped private
        "https://100.64.0.1/mcp",  # CGNAT(非公開)
        "https://224.0.0.1/mcp",  # IPv4 multicast
        "https://[ff02::1]/mcp",  # IPv6 multicast
        "https://mcp.example.com:abc/x",  # 非数値ポート
        "https://mcp.example.com:99999/x",  # 範囲外ポート
        "https://2130706433/mcp",  # 10進 IPv4(=127.0.0.1)
        "https://127.1/mcp",  # 短縮 IPv4(=127.0.0.1)
        "https://0x7f000001/mcp",  # 16進 IPv4(=127.0.0.1)
        "ftp://mcp.example.com",  # 非 http(s)
        "https://token@mcp.example.com/mcp",  # userinfo に認証
        "https://user:secret@mcp.example.com/mcp",  # userinfo に認証(pw)
        "https://mcp.example.com/mcp?token=abc",  # query に秘密
        "https://mcp.example.com/mcp#access_token=abc",  # fragment に秘密
    ],
)
def test_mcp_rejects_bad_endpoint(endpoint):
    with pytest.raises(ConnectorError):
        validate_connector(_definition(transport="mcp", endpoint=endpoint))


def test_mcp_allows_public_fqdn():
    # FQDN は DNS 解決せず通す(invoke 時に解決して SSRF 判定 = CON-03)。
    d = validate_connector(
        _definition(transport="mcp", endpoint="https://connectors.acme.example/slack")
    )
    assert d.endpoint.endswith("/slack")


# --- 異常系: auth ------------------------------------------------------------


def test_auth_token_requires_secret_ref():
    with pytest.raises(ConnectorError):
        validate_connector(_definition(auth={"kind": "api_token"}))


def test_auth_none_forbids_secret_ref():
    with pytest.raises(ConnectorError):
        validate_connector(
            _definition(auth={"kind": "none", "secretRef": "x"})
        )


def test_auth_none_forbids_scopes():
    with pytest.raises(ConnectorError):
        validate_connector(
            _definition(auth={"kind": "none", "scopes": ["chat:write"]})
        )


def test_auth_token_forbids_scopes():
    with pytest.raises(ConnectorError):
        validate_connector(
            _definition(auth={"kind": "api_token", "secretRef": "tok", "scopes": ["x"]})
        )


def test_secret_ref_rejects_value_like_string():
    # 参照名であって実値ではない。空白・記号入りの「いかにもトークン」な文字列を弾く。
    with pytest.raises(ConnectorError):
        validate_connector(
            _definition(
                auth={"kind": "api_token", "secretRef": "xoxb-1234-ABCD/secret token"}
            )
        )


def test_auth_scopes_dup_rejected():
    with pytest.raises(ConnectorError):
        validate_connector(
            _definition(
                auth={
                    "kind": "oauth2",
                    "secretRef": "tok",
                    "scopes": ["chat:write", "chat:write"],
                }
            )
        )


# --- 異常系: actions ---------------------------------------------------------


def test_actions_min_one():
    with pytest.raises(ConnectorError):
        validate_connector(_definition(actions=[]))


def test_action_name_dup_rejected():
    with pytest.raises(ConnectorError):
        validate_connector(
            _definition(
                actions=[
                    {"name": "post", "title": "a"},
                    {"name": "post", "title": "b"},
                ]
            )
        )


def test_action_unknown_permission_rejected():
    with pytest.raises(ConnectorError):
        validate_connector(
            _definition(
                actions=[
                    {"name": "x", "title": "x", "permissions": ["platform:bogus"]}
                ]
            )
        )


def test_action_dup_permission_rejected():
    with pytest.raises(ConnectorError):
        validate_connector(
            _definition(
                actions=[
                    {
                        "name": "x",
                        "title": "x",
                        "permissions": [
                            "platform:db.query",
                            "platform:db.query",
                        ],
                    }
                ]
            )
        )


def test_unknown_top_level_key_rejected():
    with pytest.raises(ConnectorError):
        validate_connector(_definition(bogus=1))


# --- 合成バリデーション -------------------------------------------------------


def test_composition_ok():
    report = validate_connector_composition(_manifest())
    assert report.ok is True
    assert report.provider == "slack"
    assert report.transport == "builtin"
    assert report.actions == ["post_message", "search_messages"]
    assert report.required_permissions == ["platform:conversations.read"]
    assert report.undeclared_permissions == []
    assert report.requires_secret is True
    assert report.secret_ref == "slack-bot-token"


def test_composition_undeclared_permission():
    # action が要求するスコープを manifest.permissions が宣言していない → 致命。
    report = validate_connector_composition(_manifest(permissions=[]))
    assert report.ok is False
    assert report.undeclared_permissions == ["platform:conversations.read"]


def test_composition_unused_permission_warns_only():
    report = validate_connector_composition(
        _manifest(permissions=["platform:conversations.read", "platform:files.read"])
    )
    assert report.ok is True  # unused は警告であって致命ではない
    assert report.unused_permissions == ["platform:files.read"]


def test_composition_no_secret_when_auth_none():
    d = _definition(
        auth={"kind": "none"},
        actions=[{"name": "ping", "title": "ping"}],
    )
    report = validate_connector_composition(_manifest(definition=d, permissions=[]))
    assert report.ok is True
    assert report.requires_secret is False
    assert report.secret_ref is None


def test_composition_rejects_wrong_kind():
    m = validate_manifest(
        {
            "schemaVersion": SCHEMA_VERSION,
            "id": "jetuse/x",
            "version": "1.0.0",
            "kind": "agent",
            "name": "x",
            "publisher": "jetuse",
            "jetuse": {"minVersion": "0.3.0"},
            "contributes": {"agent": {"instructions": "hi"}},
        }
    )
    with pytest.raises(ConnectorError):
        validate_connector_composition(m)


def test_required_permissions_helper():
    d = validate_connector(_definition())
    assert required_permissions(d) == {"platform:conversations.read"}


def test_composition_error_carries_report():
    report = validate_connector_composition(_manifest(permissions=[]))
    err = ConnectorCompositionError(report)
    assert err.report.undeclared_permissions == ["platform:conversations.read"]


# --- validate_manifest() の公開入口で詳細を強制する ---------------------------


def test_validate_manifest_enforces_connector_detail_mcp_no_endpoint():
    # 公開入口 validate_manifest() 単体で詳細違反(mcp なのに endpoint 無し)を弾く。
    bad = _definition(transport="mcp")  # endpoint 無し
    with pytest.raises(ManifestError):
        _manifest(definition=bad)


def test_validate_manifest_enforces_connector_detail_endpoint_userinfo():
    # endpoint に認証値を埋め込む経路を公開入口で塞ぐ(認証実値の混入防止)。
    bad = _definition(transport="mcp", endpoint="https://tok@mcp.example.com/x")
    with pytest.raises(ManifestError):
        _manifest(definition=bad)


def test_validate_manifest_enforces_connector_detail_bad_secret_ref():
    # secretRef にトークンらしい実値を入れる manifest は公開入口で拒否される。
    bad = _definition(
        auth={"kind": "api_token", "secretRef": "xoxb-12345/REALTOKEN value"}
    )
    with pytest.raises(ManifestError):
        _manifest(definition=bad)


def test_validate_manifest_detail_enforced_without_importing_connector():
    # import 順非依存の回帰: connector を import せず validate_manifest だけを使う新規プロセスで、
    # mcp なのに endpoint 無しの不正 connector manifest が ManifestError になることを確認する
    # (遅延 import dispatch が効くこと。同一プロセス内では他テストが connector を import 済みのため
    #  クリーンな import 状態を subprocess で再現する)。
    import subprocess
    import sys
    import textwrap

    code = textwrap.dedent(
        """
        from jetuse_core.plugins.manifest import validate_manifest, ManifestError
        bad = {
            "schemaVersion": "1", "id": "jetuse/c", "version": "1.0.0",
            "kind": "connector", "name": "c", "publisher": "jetuse",
            "jetuse": {"minVersion": "0.3.0"},
            "contributes": {"connector": {
                "provider": "slack", "transport": "mcp",
                "auth": {"kind": "none"},
                "actions": [{"name": "x", "title": "x"}],
            }},
        }
        try:
            validate_manifest(bad)
            print("NO_RAISE")
        except ManifestError:
            print("RAISED")
        """
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert out.returncode == 0, out.stderr
    assert "RAISED" in out.stdout, out.stdout


def test_validate_manifest_allows_composition_invalid_detail_valid():
    # 詳細は valid だが合成(undeclared permission)が NG の manifest は、validate_manifest() では
    # 通る(合成は register/synthesis 境界の責務)。詳細と合成の責務分離を固定する。
    m = _manifest(permissions=[])  # action は conversations.read を要求するが未宣言
    assert m.kind == "connector"  # 詳細 valid なので manifest 検証は成功
    report = validate_connector_composition(m)
    assert report.ok is False  # 合成側では落ちる


# --- JSON Schema -------------------------------------------------------------


def test_json_schema_has_enums():
    schema = connector_json_schema()
    props = schema["properties"]
    assert set(props["transport"]["enum"]) == {"mcp", "builtin"}
    # secretRef が camelCase の別名で出る(配布表現)。
    assert "auth" in props
