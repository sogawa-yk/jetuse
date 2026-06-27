"""DEP-01: 生成デモのコンテナ配備仕様生成の単体テスト。

合成済み `DemoComposition` → container-instance モジュールへ写像できる配備仕様を、代表構成と
fail-closed 境界(未合成・ガバナンス未通過・秘密実値混入・名前衝突・イメージ未指定)で検証する。
"""

import json

import pytest

from jetuse_core.deploy import (
    DEFAULT_APP_PORT,
    ContainerDeploySpec,
    DeploySpecError,
    build_deploy_spec,
    resolve_agent_app_ocid,
)
from jetuse_core.recommend import recommend
from jetuse_core.settings import Settings
from jetuse_core.synth import synthesize

_IMAGE = "kix.ocir.io/exampnamespace/jetuse-demo:latest"
_VAULT_OCID = "ocid1.vaultsecret.oc1.ap-osaka-1.amaaaaaaexamplesecret"
_APP_OCID = "ocid1.datasciencemodeldeployment.oc1.ap-osaka-1.amaaaaaaexampleapp"


def _settings(**over) -> Settings:
    base = dict(hosted_demo_image_url="", oci_region="ap-osaka-1")
    base.update(over)
    return Settings(_env_file=None, **base)


def _answers(**over):
    base = {
        "Q1": "support", "Q2": ["docs"], "Q3": "rag_qa",
        "Q4": "slack", "Q5": "chat_form", "Q6": "sample",
    }
    base.update(over)
    return base


def _composition(**over):
    return synthesize(recommend(_answers(**over)))


def _governance_failing_composition():
    # SBA-A に nl2sql 組込点は無い → governance が disallowed_combination で FAIL。
    # ただし synth 自体は SBA-A を解決できるので composition.ok は True。
    rec = recommend(_answers(Q1="support"))
    rec = rec.model_copy(update={"ai_parts": [*rec.ai_parts, "nl2sql"]})
    return synthesize(rec)


# --- 正常系 ----------------------------------------------------------------


def test_build_spec_from_representative_composition():
    comp = _composition()
    assert comp.ok and "slack" in comp.active_connectors

    spec = build_deploy_spec(comp, settings=_settings(), image_url=_IMAGE)

    assert isinstance(spec, ContainerDeploySpec)
    assert spec.image_url == _IMAGE
    assert spec.app_port == DEFAULT_APP_PORT
    assert spec.sdk == "openai_agents"  # ADR-0009 既定正規化
    assert spec.active_connectors == ("slack",)
    # active コネクタ(slack)の invoke スコープが付与予定スコープに集約される(D5)。
    assert any(s.startswith("platform:connector.invoke") for s in spec.required_scopes)


def test_module_environment_is_deterministic_and_nonsecret():
    comp = _composition()
    spec = build_deploy_spec(comp, settings=_settings(), image_url=_IMAGE)
    env = spec.module_environment()

    assert env == dict(sorted(env.items()))  # 決定的・ソート済み
    assert env["OCI_REGION"] == "ap-osaka-1"
    assert env["JETUSE_ACTIVE_CONNECTORS"] == "slack"
    assert env["JETUSE_PLATFORM_SCOPES"]  # active コネクタ由来で非空


# コンテナ自身の OIDC クライアント資格(短期トークン取得用)。要求してよい秘密の論理名。
_CONTAINER_SECRET = "HOSTED_AGENT_CLIENT_SECRET"


def test_tfvars_has_no_secret_or_vault_ocid():
    comp = _composition()
    spec = build_deploy_spec(
        comp,
        settings=_settings(),
        image_url=_IMAGE,
        required_secrets=[_CONTAINER_SECRET],
    )
    payload = json.loads(spec.render_tfvars_json())
    flat = json.dumps(payload)

    assert payload["image_url"] == _IMAGE
    # 秘密・Vault OCID は tfvars(=state)に一切残さない(DEP-02 が注入)。
    assert "secret_refs" not in payload
    assert "ocid1.vaultsecret" not in flat
    # 要求秘密は仕様メタにのみ論理名で残る。
    assert spec.required_secrets == (_CONTAINER_SECRET,)
    # 非秘密 env(module_environment)に秘密名は現れない。
    assert _CONTAINER_SECRET not in spec.module_environment()


def test_broker_signing_secret_request_is_refused():
    # ADR-0014/D5: L3 コンテナは broker 署名鍵を持たない(短期トークンのみ)。要求は構造的に拒否。
    comp = _composition()
    with pytest.raises(DeploySpecError):
        build_deploy_spec(
            comp, settings=_settings(), image_url=_IMAGE,
            required_secrets=["PLATFORM_BROKER_SECRET"],
        )


def test_sdk_resolution_uses_adr0009_app_ocid():
    comp = _composition()
    settings = _settings(agent_langgraph_app_ocid=_APP_OCID)
    spec = build_deploy_spec(comp, settings=settings, image_url=_IMAGE, sdk="langgraph")

    assert spec.sdk == "langgraph"
    assert spec.agent_app_ocid == _APP_OCID
    assert spec.module_environment()["JETUSE_AGENT_APP_OCID"] == _APP_OCID
    assert resolve_agent_app_ocid("langgraph", settings) == _APP_OCID


def test_generated_env_value_vault_ocid_is_refused():
    # 生成 env(JETUSE_DEMO_APP=app_name 等)に Vault OCID が混入しても最終 env 走査で拒否する。
    comp = _composition()
    comp = comp.model_copy(update={"app_name": f"demo {_VAULT_OCID} x"})
    with pytest.raises(DeploySpecError):
        build_deploy_spec(comp, settings=_settings(), image_url=_IMAGE)


def test_image_url_falls_back_to_settings_default():
    comp = _composition()
    spec = build_deploy_spec(comp, settings=_settings(hosted_demo_image_url=_IMAGE))
    assert spec.image_url == _IMAGE


# --- fail-closed 境界 -------------------------------------------------------


def test_unsynthesized_composition_is_refused():
    comp = _composition(Q1="other")  # 主SBA 未確定 → ok=False
    assert comp.ok is False
    with pytest.raises(DeploySpecError):
        build_deploy_spec(comp, settings=_settings(), image_url=_IMAGE)


def test_internal_governance_gate_always_refuses_failing_composition():
    # ガバナンスは常に内部評価される(呼び出し側に report を渡す口は無い=バイパス/詐称不可)。
    comp = _governance_failing_composition()
    assert comp.ok is True  # 合成自体は成立(SBA-A 解決)
    with pytest.raises(DeploySpecError):
        build_deploy_spec(comp, settings=_settings(), image_url=_IMAGE)


def test_governance_report_param_is_not_accepted():
    # 詐称 report 注入の口を塞いだことの回帰: governance_report キーワードは存在しない。
    comp = _composition()
    with pytest.raises(TypeError):
        build_deploy_spec(
            comp, settings=_settings(), image_url=_IMAGE, governance_report=object()
        )


def test_missing_image_is_refused():
    comp = _composition()
    with pytest.raises(DeploySpecError):
        build_deploy_spec(comp, settings=_settings())  # image 指定なし・既定も空


@pytest.mark.parametrize(
    "image",
    [
        "ghcr.io/sogawa-yk/jetuse-demo:latest",  # OCIR でない
        "docker.io/library/nginx",
        "phx.ocir.io/ns/jetuse-demo:latest",     # 別リージョン OCIR(ADR-0011: ap-osaka 固定)
        "iad.ocir.io/ns/jetuse-demo",
        "kix.ocir.io/onlynamespace",             # repo パスが無い
        "kix.ocir.io/ns//repo",                  # 空セグメント
        "kix.ocir.io/ns/repo/",                  # 末尾スラッシュ
        "just-a-string",
    ],
)
def test_non_aposaka_ocir_image_is_refused(image):
    comp = _composition()
    with pytest.raises(DeploySpecError):
        build_deploy_spec(comp, settings=_settings(), image_url=image)


@pytest.mark.parametrize("bad_sdk", ["select_ai", "agents", "openai", "typo", ""])
def test_unknown_sdk_is_refused(bad_sdk):
    # 未知/typo/非 hosted ランタイムが別 SDK のコンテナ配備へ黙って化けない(fail-closed)。
    comp = _composition()
    with pytest.raises(DeploySpecError):
        build_deploy_spec(comp, settings=_settings(), image_url=_IMAGE, sdk=bad_sdk)


@pytest.mark.parametrize("good_sdk", ["openai_agents", "langgraph", "adk"])
def test_known_sdk_is_accepted(good_sdk):
    comp = _composition()
    spec = build_deploy_spec(comp, settings=_settings(), image_url=_IMAGE, sdk=good_sdk)
    assert spec.sdk == good_sdk


@pytest.mark.parametrize("bad_name", ["bad name", "bad-name", "lower", "1leading"])
def test_required_secret_invalid_name_is_refused(bad_name):
    comp = _composition()
    with pytest.raises(DeploySpecError):
        build_deploy_spec(
            comp, settings=_settings(), image_url=_IMAGE, required_secrets=[bad_name],
        )


def test_required_secret_reserved_key_is_refused():
    comp = _composition()
    with pytest.raises(DeploySpecError):
        build_deploy_spec(
            comp, settings=_settings(), image_url=_IMAGE,
            required_secrets=["JETUSE_AGENT_APP_OCID"],  # 予約キー
        )


def test_valid_required_secret_is_declared_by_name_only():
    comp = _composition()
    spec = build_deploy_spec(
        comp, settings=_settings(), image_url=_IMAGE,
        required_secrets=["HOSTED_AGENT_CLIENT_SECRET"],
    )
    # 論理名だけが仕様に残る(Vault OCID は持たない)。
    assert spec.required_secrets == ("HOSTED_AGENT_CLIENT_SECRET",)


@pytest.mark.parametrize(
    "name",
    ["DB_PASSWORD", "ADB_WALLET_PASSWORD", "DATABASE_URL", "MY_OWN_SECRET", "A"],
)
def test_non_allowlisted_required_secret_is_refused(name):
    # ADR-0014/D5: L3 は許可リスト外の秘密(DB 資格情報等)を要求しない(allowlist, fail-closed)。
    comp = _composition()
    with pytest.raises(DeploySpecError):
        build_deploy_spec(
            comp, settings=_settings(), image_url=_IMAGE, required_secrets=[name]
        )


# --- extra_environment の検証(秘密混入・予約キー・名前形式) ----------------


def test_extra_environment_reserved_key_is_refused():
    comp = _composition()
    with pytest.raises(DeploySpecError):
        build_deploy_spec(
            comp, settings=_settings(), image_url=_IMAGE,
            extra_environment={"OCI_REGION": "us-ashburn-1"},  # 予約キー上書き
        )


def test_extra_environment_secret_looking_key_is_refused():
    # JETUSE_ 名前空間内の資格情報名(hint 経路を確実に通すため接頭辞を付ける)。
    comp = _composition()
    for bad in ("JETUSE_DB_PASSWORD", "JETUSE_SLACK_TOKEN", "JETUSE_MY_SECRET",
                "JETUSE_X_API_KEY", "JETUSE_DATABASE_URL", "JETUSE_ADB_DSN",
                "JETUSE_PG_CONNECTION_STRING", "JETUSE_DB_PASSWD"):
        with pytest.raises(DeploySpecError):
            build_deploy_spec(
                comp, settings=_settings(), image_url=_IMAGE,
                extra_environment={bad: "whatever"},
            )


@pytest.mark.parametrize(
    "value",
    [
        _VAULT_OCID,                       # 値全体が Vault OCID
        f"ref={_VAULT_OCID}",              # 接頭辞付き埋め込み
        f"a,{_VAULT_OCID},b",              # 文中に埋め込み
    ],
)
def test_extra_environment_vault_ocid_value_is_refused(value):
    # 値に Vault OCID が**部分一致でも**含まれれば拒否(秘密は env に置かない=DEP-02)。
    comp = _composition()
    with pytest.raises(DeploySpecError):
        build_deploy_spec(
            comp, settings=_settings(), image_url=_IMAGE,
            extra_environment={"JETUSE_REF": value},
        )


def test_extra_environment_invalid_name_is_refused():
    comp = _composition()
    with pytest.raises(DeploySpecError):
        build_deploy_spec(
            comp, settings=_settings(), image_url=_IMAGE,
            extra_environment={"bad-name": "v"},  # 小文字/ハイフン不可
        )


def test_extra_environment_valid_nonsecret_is_accepted():
    comp = _composition()
    spec = build_deploy_spec(
        comp, settings=_settings(), image_url=_IMAGE,
        extra_environment={"JETUSE_DEMO_TITLE": "FAQ デスク"},
    )
    assert spec.environment_variables["JETUSE_DEMO_TITLE"] == "FAQ デスク"


@pytest.mark.parametrize("key", ["DEMO_TITLE", "DB_PASS", "OPENAI_KEY", "FOO", "X_AUTH"])
def test_extra_environment_outside_namespace_is_refused(key):
    # 追加 env は JETUSE_ 名前空間のみ。任意キー(資格情報キー含む)は構造的に拒否(allowlist)。
    comp = _composition()
    with pytest.raises(DeploySpecError):
        build_deploy_spec(
            comp, settings=_settings(), image_url=_IMAGE, extra_environment={key: "v"}
        )


@pytest.mark.parametrize(
    "key",
    ["JETUSE_DB_PASS", "JETUSE_OPENAI_KEY", "JETUSE_X_TOKEN", "JETUSE_AUTH", "JETUSE_CERT"],
)
def test_extra_environment_credential_named_in_namespace_is_refused(key):
    # 名前空間内でも資格情報名(PASS/KEY/TOKEN/AUTH/CERT 等)は hint で拒否(JETUSE_DB_PASS 等)。
    comp = _composition()
    with pytest.raises(DeploySpecError):
        build_deploy_spec(
            comp, settings=_settings(), image_url=_IMAGE, extra_environment={key: "v"}
        )


# --- リソース範囲検証 -------------------------------------------------------


@pytest.mark.parametrize("port", [0, -1, 70000])
def test_invalid_app_port_is_refused(port):
    comp = _composition()
    with pytest.raises(DeploySpecError):
        build_deploy_spec(comp, settings=_settings(), image_url=_IMAGE, app_port=port)


@pytest.mark.parametrize(
    "ocpus,memory_gb",
    [
        (0, 8), (-1, 8), (8, 0), (8, -4), (9999, 8), (8, 99999),
        (0.5, 8), (8, 2.5),        # 端数は不可(整数刻み)
        (True, 8), (8, False),     # bool を数値として通さない
    ],
)
def test_invalid_resources_are_refused(ocpus, memory_gb):
    comp = _composition()
    with pytest.raises(DeploySpecError):
        build_deploy_spec(
            comp, settings=_settings(), image_url=_IMAGE,
            ocpus=ocpus, memory_gb=memory_gb,
        )


def test_non_aposaka_region_is_refused():
    comp = _composition()
    with pytest.raises(DeploySpecError):
        build_deploy_spec(
            comp, settings=_settings(oci_region="us-phoenix-1"), image_url=_IMAGE
        )


@pytest.mark.parametrize("prefix", ["!!!", "---", "@@@", "  "])
def test_empty_after_sanitize_prefix_is_refused(prefix):
    # 区切り文字のみ等、健全化後に有効文字が残らない prefix は fail-closed。
    comp = _composition()
    with pytest.raises(DeploySpecError):
        build_deploy_spec(comp, settings=_settings(), image_url=_IMAGE, prefix=prefix)
