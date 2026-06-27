"""プラグイン manifest 仕様＋バリデータのテスト(PLG-01)。

正常系3種(usecase / agent / 署名つき)＋不正 manifest 拒否を網羅する。
署名検証は cryptography の ed25519 を実鍵で往復させる。
"""

import base64
import copy

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from jetuse_core.plugins.manifest import (
    PLATFORM_SCOPES,
    SCHEMA_VERSION,
    ManifestError,
    canonical_signing_payload,
    manifest_json_schema,
    validate_manifest,
    verify_signature,
)


def _base_usecase() -> dict:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "id": "acme/faq-summarizer",
        "version": "1.2.0",
        "kind": "usecase",
        "name": "FAQ要約",
        "description": "FAQを要約するユースケース",
        "publisher": "acme-corp",
        "jetuse": {"minVersion": "0.3.0"},
        "requires": {"models": ["gpt-oss-120b"], "datasources": [], "tools": []},
        "permissions": ["platform:rag.search"],
        "contributes": {
            "usecase": {
                "fields": [{"name": "text", "type": "textarea"}],
                "template": "要約して: {{text}}",
            }
        },
        "icon": "📝",
        "tags": ["faq", "summary"],
        "license": "MIT",
    }


def _base_agent() -> dict:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "id": "acme/sales-agent",
        "version": "0.1.0-beta.1",
        "kind": "agent",
        "name": "営業エージェント",
        "publisher": "acme-corp",
        "jetuse": {"minVersion": "0.3.0"},
        "permissions": ["platform:db.query", "platform:conversations.read"],
        "contributes": {
            "agent": {
                "instructions": "案件を整理する",
                "tools": ["nl2sql"],
            }
        },
    }


# --- 正常系 ---------------------------------------------------------------


def test_valid_usecase_manifest():
    m = validate_manifest(_base_usecase())
    assert m.kind == "usecase"
    assert m.id == "acme/faq-summarizer"
    assert m.version == "1.2.0"
    assert m.permissions == ["platform:rag.search"]
    # camelCase で往復できる。
    assert m.model_dump(by_alias=True)["schemaVersion"] == SCHEMA_VERSION
    assert m.jetuse.min_version == "0.3.0"


def test_valid_agent_manifest_with_prerelease_version():
    m = validate_manifest(_base_agent())
    assert m.kind == "agent"
    assert m.version == "0.1.0-beta.1"
    # 任意フィールドの既定。
    assert m.requires.models == []
    assert m.tags == []
    assert m.signature is None


def test_valid_signed_manifest_roundtrip():
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    data = _base_usecase()
    # 署名は signature を除いた正準ペイロードに対して付与する。
    unsigned = validate_manifest(data)
    sig = priv.sign(canonical_signing_payload(unsigned))
    data["signature"] = {
        "algorithm": "ed25519",
        "publicKeyId": "acme-key-1",
        "value": base64.b64encode(sig).decode(),
    }
    signed = validate_manifest(data)
    pub_bytes = pub.public_bytes_raw()
    assert verify_signature(signed, pub_bytes) is True
    # 別の鍵では失敗する。
    other = Ed25519PrivateKey.generate().public_key().public_bytes_raw()
    assert verify_signature(signed, other) is False


# --- 不正 manifest 拒否 ---------------------------------------------------


@pytest.mark.parametrize(
    "mutate, needle",
    [
        (lambda d: d.update(schemaVersion="2"), "schemaVersion"),
        (lambda d: d.update(id="ACME/Bad_Name"), "id"),
        (lambda d: d.update(id="noslash"), "id"),
        (lambda d: d.update(version="1.2"), "version"),
        (lambda d: d.update(version="v1.2.0"), "version"),
        (lambda d: d.update(kind="tool"), "kind"),
        (lambda d: d.update(name="  "), "name"),
        (lambda d: d.update(publisher=""), "publisher"),
        (lambda d: d.update(jetuse={"minVersion": "latest"}), "minVersion"),
        (lambda d: d.update(permissions=["platform:secrets.read"]), "permission"),
        (lambda d: d.update(permissions=["platform:rag.search", "platform:rag.search"]), "重複"),
        (lambda d: d.update(contributes={"agent": {}}), "contributes"),
        (lambda d: d.update(unknownField=1), "unknownField"),
    ],
)
def test_invalid_manifest_rejected(mutate, needle):
    data = _base_usecase()
    mutate(data)
    with pytest.raises(ManifestError) as exc:
        validate_manifest(data)
    assert needle in str(exc.value)


def test_id_and_version_length_bounds():
    """長さ上限(永続化層の VARCHAR2 幅と一致)。境界ちょうどは通り、超過は拒否する。

    検証を通った manifest が必ず installed_plugins / source_* カラムに保存できることを保証
    する(PLG-02 で Codex が指摘した validator と DB 制約の乖離回避)。
    """
    from jetuse_core.plugins.manifest import MAX_ID_LEN, MAX_VERSION_LEN

    # id: "acme/" + name。境界ちょうど(255)は valid、+1(256)は拒否。
    name_at = "a" * (MAX_ID_LEN - len("acme/"))
    validate_manifest({**_base_usecase(), "id": f"acme/{name_at}"})
    with pytest.raises(ManifestError) as exc:
        validate_manifest({**_base_usecase(), "id": f"acme/{name_at}a"})
    assert "id" in str(exc.value)

    # version: build metadata で伸ばす。境界ちょうど(64)は valid、+1(65)は拒否。
    build_at = "b" * (MAX_VERSION_LEN - len("1.0.0+"))
    validate_manifest({**_base_usecase(), "version": f"1.0.0+{build_at}"})
    with pytest.raises(ManifestError) as exc:
        validate_manifest({**_base_usecase(), "version": f"1.0.0+{build_at}b"})
    assert "version" in str(exc.value)


@pytest.mark.parametrize(
    "missing",
    ["schemaVersion", "id", "version", "kind", "name", "publisher", "jetuse", "contributes"],
)
def test_missing_required_field_rejected(missing):
    data = _base_usecase()
    data.pop(missing)
    with pytest.raises(ManifestError):
        validate_manifest(data)


def test_invalid_signature_fields_rejected():
    data = _base_usecase()
    data["signature"] = {
        "algorithm": "rsa",  # ed25519 以外
        "publicKeyId": "k1",
        "value": base64.b64encode(b"\x00" * 64).decode(),
    }
    with pytest.raises(ManifestError) as exc:
        validate_manifest(data)
    assert "algorithm" in str(exc.value)


def test_signature_value_must_be_base64_and_64_bytes():
    data = _base_usecase()
    data["signature"] = {"algorithm": "ed25519", "publicKeyId": "k1", "value": "not-base64!!"}
    with pytest.raises(ManifestError):
        validate_manifest(data)
    data["signature"]["value"] = base64.b64encode(b"short").decode()
    with pytest.raises(ManifestError) as exc:
        validate_manifest(data)
    assert "64" in str(exc.value)


def test_signature_blank_public_key_id_rejected():
    data = _base_usecase()
    data["signature"] = {
        "algorithm": "ed25519",
        "publicKeyId": "   ",
        "value": base64.b64encode(b"\x00" * 64).decode(),
    }
    with pytest.raises(ManifestError) as exc:
        validate_manifest(data)
    assert "publicKeyId" in str(exc.value)


def test_unsigned_manifest_fails_verification():
    m = validate_manifest(_base_usecase())
    assert verify_signature(m, b"\x00" * 32) is False


@pytest.mark.parametrize("bad_key", ["not-bytes", b"too-short", b"\x00" * 31, 12345, None])
def test_verify_signature_never_raises_on_bad_key(bad_key):
    """契約: 鍵が不正型/不正長でも例外を漏らさず False を返す(review-4 blocker)。"""
    priv = Ed25519PrivateKey.generate()
    data = _base_usecase()
    sig = priv.sign(canonical_signing_payload(validate_manifest(data)))
    data["signature"] = {
        "algorithm": "ed25519",
        "publicKeyId": "k1",
        "value": base64.b64encode(sig).decode(),
    }
    signed = validate_manifest(data)
    assert verify_signature(signed, bad_key) is False


def test_verify_signature_fail_closed_on_bypassed_manifest():
    """検証を迂回(model_construct)した不正 signature でも例外を漏らさず False(fail-closed)。"""
    from jetuse_core.plugins.manifest import PluginManifest, Signature

    sig = Signature.model_construct(algorithm="ed25519", public_key_id="k1", value=12345)
    m = PluginManifest.model_construct(signature=sig)
    assert verify_signature(m, b"\x00" * 32) is False


def test_verify_signature_fail_closed_on_missing_algorithm_attr():
    """signature に algorithm 属性が欠落していても AttributeError を漏らさず False。"""
    from jetuse_core.plugins.manifest import PluginManifest, Signature

    # model_construct は属性を埋めない → signature.algorithm 参照は AttributeError になりうる。
    sig = Signature.model_construct(public_key_id="k1", value="AA==")
    m = PluginManifest.model_construct(signature=sig)
    assert verify_signature(m, b"\x00" * 32) is False


def test_canonical_payload_rejects_non_finite_numbers():
    """canonical_signing_payload は NaN/Infinity を含む迂回 manifest で非 JSON を作らない。"""
    import math

    from jetuse_core.plugins.manifest import PluginManifest

    data = _base_usecase()
    m = PluginManifest.model_construct(
        **{**validate_manifest(data).model_dump(by_alias=True), "version": "1.0.0"}
    )
    # contributes に NaN を注入(検証を迂回した状態を模す)。
    object.__setattr__(m, "contributes", {"usecase": {"x": math.nan}})
    with pytest.raises(ValueError):
        canonical_signing_payload(m)


def test_verify_signature_rejects_non_ed25519_algorithm():
    """迂回構築で algorithm が ed25519 以外なら、ed25519 として検証せず False。"""
    from jetuse_core.plugins.manifest import Signature

    priv = Ed25519PrivateKey.generate()
    payload_src = validate_manifest(_base_usecase())
    sig = priv.sign(canonical_signing_payload(payload_src))
    bad = Signature.model_construct(
        algorithm="rsa", public_key_id="k1", value=base64.b64encode(sig).decode()
    )
    m = payload_src.model_copy(update={"signature": bad})
    assert verify_signature(m, priv.public_key().public_bytes_raw()) is False


@pytest.mark.parametrize("ver", ["1.2.３", "１.0.0", "1.2.3٠"])
def test_version_rejects_non_ascii_digits(ver):
    """semver は ASCII 数字のみ。全角・アラビア数字等は拒否する。"""
    data = _base_usecase()
    data["version"] = ver
    with pytest.raises(ManifestError):
        validate_manifest(data)


def test_tampered_manifest_fails_verification():
    priv = Ed25519PrivateKey.generate()
    data = _base_usecase()
    unsigned = validate_manifest(data)
    sig = priv.sign(canonical_signing_payload(unsigned))
    data["signature"] = {
        "algorithm": "ed25519",
        "publicKeyId": "acme-key-1",
        "value": base64.b64encode(sig).decode(),
    }
    # 署名後に本文を改ざんする。
    tampered = copy.deepcopy(data)
    tampered["name"] = "改ざん済み"
    m = validate_manifest(tampered)
    assert verify_signature(m, priv.public_key().public_bytes_raw()) is False


# --- JSON Schema ----------------------------------------------------------


def test_json_schema_exposes_camelcase_keys():
    schema = manifest_json_schema()
    props = schema["properties"]
    assert "schemaVersion" in props
    required = set(schema["required"])
    assert required >= {"schemaVersion", "id", "version", "kind", "name", "publisher"}


def test_json_schema_reflects_validator_constraints():
    """F-001: 配布スキーマと実バリデータの乖離を防ぐ。const/enum/pattern が出る。"""
    schema = manifest_json_schema()
    props = schema["properties"]
    assert props["schemaVersion"].get("const") == SCHEMA_VERSION
    assert props["kind"]["enum"] == [
        "usecase",
        "agent",
        "sample-app",
        "connector",
        "external-app",
    ]
    assert "pattern" in props["id"]
    assert "pattern" in props["version"]
    assert set(props["permissions"]["items"]["enum"]) == set(PLATFORM_SCOPES)
    assert props["contributes"].get("maxProperties") == 1
    assert props["contributes"].get("minProperties") == 1
    # signature.algorithm は $defs 側に const として出る。
    sig_def = schema["$defs"]["Signature"]["properties"]
    assert sig_def["algorithm"].get("const") == "ed25519"
    assert sig_def["value"].get("contentEncoding") == "base64"


@pytest.mark.parametrize("bad", [None, [], "text", 1])
def test_contributes_non_object_payload_rejected(bad):
    """F-002: kind に対応する値が object でない payload を拒否する。"""
    data = _base_usecase()
    data["contributes"] = {"usecase": bad}
    with pytest.raises(ManifestError):
        validate_manifest(data)


@pytest.mark.parametrize(
    "bad_value",
    [b"bytes", float("nan"), float("inf"), object(), {"nested": b"x"}, ["ok", b"x"]],
)
def test_contributes_non_json_value_rejected(bad_value):
    """検証済み manifest が必ず正準 JSON 化できるよう、非 JSON 値を拒否する(review-4 major)。"""
    data = _base_usecase()
    data["contributes"] = {"usecase": {"payload": bad_value}}
    with pytest.raises(ManifestError):
        validate_manifest(data)


def test_validated_manifest_is_always_canonicalizable():
    """検証を通った manifest は canonical_signing_payload で例外を出さない。"""
    m = validate_manifest(_base_usecase())
    payload = canonical_signing_payload(m)
    assert isinstance(payload, bytes) and payload


def test_all_platform_scopes_accepted():
    data = _base_agent()
    data["permissions"] = sorted(PLATFORM_SCOPES)
    m = validate_manifest(data)
    assert set(m.permissions) == set(PLATFORM_SCOPES)


# --- 署名正準化(F-001) ---------------------------------------------------


def test_signing_payload_excludes_only_signature():
    """正準ペイロードは signature のみを除き、任意フィールドの null は保持する。"""
    priv = Ed25519PrivateKey.generate()
    data = _base_usecase()
    unsigned = validate_manifest(data)
    sig = priv.sign(canonical_signing_payload(unsigned))
    data["signature"] = {
        "algorithm": "ed25519",
        "publicKeyId": "k1",
        "value": base64.b64encode(sig).decode(),
    }
    signed = validate_manifest(data)
    payload = canonical_signing_payload(signed)
    assert b'"signature"' not in payload
    # 署名の有無で正準ペイロードは不変(signature だけを落とすため)。
    assert payload == canonical_signing_payload(unsigned)


def test_minimal_manifest_signed_roundtrip():
    """必須のみ・任意フィールド未指定(=null/既定注入)の最小 manifest でも署名往復する。"""
    priv = Ed25519PrivateKey.generate()
    minimal = {
        "schemaVersion": SCHEMA_VERSION,
        "id": "ns/app",
        "version": "0.0.1",
        "kind": "agent",
        "name": "最小",
        "publisher": "p",
        "jetuse": {"minVersion": "0.1.0"},
        "contributes": {"agent": {"instructions": "x"}},
    }
    m = validate_manifest(minimal)
    sig = priv.sign(canonical_signing_payload(m))
    minimal["signature"] = {
        "algorithm": "ed25519",
        "publicKeyId": "k1",
        "value": base64.b64encode(sig).decode(),
    }
    signed = validate_manifest(minimal)
    assert verify_signature(signed, priv.public_key().public_bytes_raw()) is True


def test_snake_case_field_names_rejected():
    """配布形式は camelCase のみ。snake_case alias は受理しない(F-003)。"""
    data = _base_usecase()
    data["schema_version"] = data.pop("schemaVersion")
    with pytest.raises(ManifestError):
        validate_manifest(data)
