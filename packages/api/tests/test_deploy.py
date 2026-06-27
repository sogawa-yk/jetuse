"""生成デモの L3 配備仕様生成の単体テスト(DEP-01 / DEP-03 で OKE/K8s 化)。

合成済み `DemoComposition` → **K8s マニフェスト**へ写像できる配備仕様を、代表構成と
fail-closed 境界(未合成・ガバナンス未通過・秘密実値混入・名前衝突・イメージ未指定)で検証する。
"""

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


def test_manifests_have_no_secret_or_vault_ocid():
    comp = _composition()
    spec = build_deploy_spec(
        comp,
        settings=_settings(),
        image_url=_IMAGE,
        required_secrets=[_CONTAINER_SECRET],
    )
    manifests = spec.render_manifests()
    kinds = [m["kind"] for m in manifests]
    flat = spec.render_manifests_yaml()

    # K8s マニフェスト一式(Namespace/ResourceQuota/ServiceAccount/ConfigMap/Deployment/Service)。
    assert kinds == [
        "Namespace",
        "ResourceQuota",
        "ServiceAccount",
        "ConfigMap",
        "Deployment",
        "Service",
    ]
    # 短期トークンの Secret は deploy.py の描画には**含めない**(deploy_inject が別経路で apply)。
    assert "Secret" not in kinds
    # 秘密・Vault OCID はマニフェスト(=committed/state)に一切残さない。
    assert "ocid1.vaultsecret" not in flat
    # 要求秘密は仕様メタにのみ論理名で残る(マニフェストには現れない)。
    assert spec.required_secrets == (_CONTAINER_SECRET,)
    assert _CONTAINER_SECRET not in flat
    # ConfigMap は非秘密 env のみ(module_environment)。秘密名は現れない。
    config_map = next(m for m in manifests if m["kind"] == "ConfigMap")
    assert config_map["data"] == spec.module_environment()
    assert _CONTAINER_SECRET not in config_map["data"]
    # イメージは Deployment の唯一のコンテナに載る。
    deployment = next(m for m in manifests if m["kind"] == "Deployment")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == _IMAGE


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


# ---- K8s(OKE)マニフェスト描画(ADR-0017) ----


def test_render_manifests_namespace_and_naming_are_deterministic():
    comp = _composition()
    spec = build_deploy_spec(
        comp, settings=_settings(), image_url=_IMAGE, prefix="jetuse-demo-faq"
    )
    manifests = spec.render_manifests()
    # namespace = prefix。全リソースが同一 namespace に入る。
    assert spec.namespace == "jetuse-demo-faq"
    for m in manifests:
        if m["kind"] == "Namespace":
            assert m["metadata"]["name"] == "jetuse-demo-faq"
        else:
            assert m["metadata"]["namespace"] == "jetuse-demo-faq"
    # Deployment/Service 名は prefix。ConfigMap/Secret/SA 名はサフィックス規約に従う。
    deployment = next(m for m in manifests if m["kind"] == "Deployment")
    service = next(m for m in manifests if m["kind"] == "Service")
    assert deployment["metadata"]["name"] == "jetuse-demo-faq"
    assert service["metadata"]["name"] == "jetuse-demo-faq"
    assert spec.config_map_name == "jetuse-demo-faq-config"
    assert spec.runtime_config_map_name == "jetuse-demo-faq-runtime"
    assert spec.token_secret_name == "jetuse-demo-faq-platform-token"
    assert spec.service_account_name == "jetuse-demo-faq-sa"


def test_deployment_envfrom_references_secret_when_scopes_present():
    comp = _composition()
    spec = build_deploy_spec(comp, settings=_settings(), image_url=_IMAGE)
    assert spec.needs_platform_injection  # active コネクタ由来のスコープがある
    deployment = next(m for m in spec.render_manifests() if m["kind"] == "Deployment")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    refs = container["envFrom"]
    names = [
        list(r.values())[0]["name"] for r in refs
    ]
    # 静的 ConfigMap ＋ 注入 ConfigMap(base_url)＋ Secret(token)を envFrom 参照する。
    assert spec.config_map_name in names
    assert spec.runtime_config_map_name in names
    assert spec.token_secret_name in names
    # Secret/runtime ConfigMap 参照は存在必須(fail-closed: 未注入のまま起動しない)。
    for r in refs:
        ref = list(r.values())[0]
        if ref["name"] in (spec.runtime_config_map_name, spec.token_secret_name):
            assert ref.get("optional") is False


def test_deployment_envfrom_omits_injection_without_scopes(monkeypatch):
    # スコープを必要としない構成では注入用 Secret/ConfigMap を参照しない(存在しない Secret 参照で
    # Pod 起動不能になるのを防ぐ)。required_scopes が空のケースを直接構築して検証する。
    comp = _composition()
    spec = build_deploy_spec(comp, settings=_settings(), image_url=_IMAGE)
    no_scope = ContainerDeploySpec(
        region=spec.region,
        prefix=spec.prefix,
        image_url=spec.image_url,
        app_port=spec.app_port,
        ocpus=spec.ocpus,
        memory_gb=spec.memory_gb,
        sdk=spec.sdk,
        agent_app_ocid=spec.agent_app_ocid,
        environment_variables=dict(spec.environment_variables),
        required_secrets=(),
        required_scopes=(),
        active_connectors=(),
        sample_app=spec.sample_app,
    )
    assert not no_scope.needs_platform_injection
    deployment = next(m for m in no_scope.render_manifests() if m["kind"] == "Deployment")
    refs = deployment["spec"]["template"]["spec"]["containers"][0]["envFrom"]
    names = [list(r.values())[0]["name"] for r in refs]
    assert names == [no_scope.config_map_name]


def test_pod_least_privilege_and_quota():
    comp = _composition()
    spec = build_deploy_spec(comp, settings=_settings(), image_url=_IMAGE)
    manifests = spec.render_manifests()
    deployment = next(m for m in manifests if m["kind"] == "Deployment")
    pod_spec = deployment["spec"]["template"]["spec"]
    # K8s API トークンを Pod に自動マウントしない(最小権限)。
    assert pod_spec["automountServiceAccountToken"] is False
    sec = pod_spec["containers"][0]["securityContext"]
    assert sec["runAsNonRoot"] is True
    # 非 root UID/GID を明示(イメージ USER が root 既定でも kubelet が検証可能=Pod 拒否を防ぐ)。
    assert sec["runAsUser"] >= 1 and sec["runAsUser"] == sec["runAsGroup"]
    assert sec["allowPrivilegeEscalation"] is False
    assert sec["capabilities"]["drop"] == ["ALL"]
    # namespace 単位の ResourceQuota が付く(越境・暴走抑止)。
    assert any(m["kind"] == "ResourceQuota" for m in manifests)


def test_manifests_yaml_is_multidoc_and_parseable():
    import yaml

    comp = _composition()
    spec = build_deploy_spec(comp, settings=_settings(), image_url=_IMAGE)
    text = spec.render_manifests_yaml()
    docs = list(yaml.safe_load_all(text))
    assert [d["kind"] for d in docs] == [m["kind"] for m in spec.render_manifests()]


def test_tfvars_backward_compat_still_works():
    # ADR-0017: 新規 L3 配備は K8s だが、stage-4 の Container Instances ベースライン(hosted-demo)を
    # 残すため、tfvars 写像 API は後方互換でそのまま機能する(公開シグネチャ不変)。
    import json as _json

    comp = _composition()
    spec = build_deploy_spec(comp, settings=_settings(), image_url=_IMAGE)
    tfvars = spec.to_tfvars()
    assert tfvars["image_url"] == _IMAGE
    assert tfvars["region"] == "ap-osaka-1"
    assert tfvars["environment_variables"] == dict(spec.environment_variables)
    # 秘密・Vault OCID は tfvars(=state)に出さない(従来契約)。
    flat = spec.render_tfvars_json()
    assert "ocid1.vaultsecret" not in flat
    assert _json.loads(flat)["prefix"] == spec.prefix


def test_injection_keys_are_reserved_and_rejected_in_extra_env():
    # 注入経路が所有するキー(base_url/token)を静的 env(extra_environment)に入れさせない。
    # これらが静的 ConfigMap に入ると envFrom で runtime ConfigMap/Secret と衝突しうる。
    from jetuse_core import deploy_inject as di

    comp = _composition()
    for key in (di.PLATFORM_API_BASE_URL_ENV, di.PLATFORM_TOKEN_ENV):
        # deploy.py の予約キーと deploy_inject の定数が一致していること(同期)。
        from jetuse_core import deploy as dep
        assert key in dep._RESERVED_ENV_KEYS
        with pytest.raises(DeploySpecError):
            build_deploy_spec(
                comp, settings=_settings(), image_url=_IMAGE,
                extra_environment={key: "x"},
            )


def test_manifest_envfrom_keys_do_not_collide():
    # 静的 ConfigMap(data)のキーと、注入経路(runtime ConfigMap / Secret)のキーが衝突しない。
    # 衝突すると K8s 実行時に envFrom の上書きが起き、注入契約の fail-closed を迂回する。
    from jetuse_core import deploy_inject as di

    comp = _composition()
    spec = build_deploy_spec(comp, settings=_settings(), image_url=_IMAGE)
    config_map = next(m for m in spec.render_manifests() if m["kind"] == "ConfigMap")
    static_keys = set(config_map["data"])
    injection_keys = {di.PLATFORM_API_BASE_URL_ENV, di.PLATFORM_TOKEN_ENV}
    assert static_keys.isdisjoint(injection_keys)


def test_deployment_carries_required_scopes_annotation():
    comp = _composition()
    spec = build_deploy_spec(comp, settings=_settings(), image_url=_IMAGE)
    deployment = next(m for m in spec.render_manifests() if m["kind"] == "Deployment")
    ann = deployment["metadata"]["annotations"]["jetuse.dev/required-scopes"]
    assert ann == ",".join(spec.required_scopes)


def test_tenant_isolation_namespaces_and_labels_do_not_collide():
    # ADR-0016 §6 / マルチテナンシ: 同一 sample_app を別テナントへ配備しても namespace/Secret/
    # Deployment 名が衝突しない(tenant 非秘密ハッシュを prefix に必ず含める)。
    comp = _composition()
    a = build_deploy_spec(comp, settings=_settings(), image_url=_IMAGE,
                          tenant="ocid1.tenancy.oc1..aaaa-tenant-A")
    b = build_deploy_spec(comp, settings=_settings(), image_url=_IMAGE,
                          tenant="ocid1.tenancy.oc1..aaaa-tenant-B")
    # namespace / Secret / Deployment 名がテナント間で異なる。
    assert a.namespace != b.namespace
    assert a.token_secret_name != b.token_secret_name
    assert a.prefix != b.prefix
    # tenant label が付き、生 OCID は label に出さない(非秘密ハッシュ・<=63)。
    assert a.labels()["jetuse.dev/tenant"] == a.tenant_hash
    assert "ocid1.tenancy" not in a.render_manifests_yaml()
    # 決定的: 同じテナントは同じ namespace に収束(再配備で増やさない)。
    a2 = build_deploy_spec(comp, settings=_settings(), image_url=_IMAGE,
                           tenant="ocid1.tenancy.oc1..aaaa-tenant-A")
    assert a2.namespace == a.namespace


def test_tenant_hash_survives_truncation_with_long_prefix():
    # F-001 回帰: 長い base prefix でも tenant suffix(`-<hash8>`)が切り詰めで欠落せず、
    # 最終 prefix が必ず tenant ハッシュで終わる → 別テナントで namespace/Secret が衝突しない。
    comp = _composition()
    long_prefix = "jetuse-demo-" + ("x" * 60)  # MAX_PREFIX_LEN(40)を大きく超える
    a = build_deploy_spec(comp, settings=_settings(), image_url=_IMAGE,
                          prefix=long_prefix, tenant="ocid1.tenancy.oc1..aaaa-tenant-A")
    b = build_deploy_spec(comp, settings=_settings(), image_url=_IMAGE,
                          prefix=long_prefix, tenant="ocid1.tenancy.oc1..aaaa-tenant-B")
    # 上限内に収まり(<=40)、両テナントとも tenant ハッシュで終端する。
    assert len(a.prefix) <= 40 and len(b.prefix) <= 40
    assert a.prefix.endswith("-" + a.tenant_hash)
    assert b.prefix.endswith("-" + b.tenant_hash)
    # 同一 base・同一 sample_app・長い名前でも namespace/Secret/Deployment が衝突しない。
    assert a.namespace != b.namespace
    assert a.token_secret_name != b.token_secret_name
    assert a.tenant_hash != b.tenant_hash


def test_tenant_whitespace_is_normalized():
    # 前後空白付き tenant は strip 正規化され、空白無しと同じ namespace に収束する(分裂しない)。
    comp = _composition()
    a = build_deploy_spec(comp, settings=_settings(), image_url=_IMAGE,
                          tenant="ocid1.tenancy.oc1..aaaa-tenant-A")
    a_ws = build_deploy_spec(comp, settings=_settings(), image_url=_IMAGE,
                             tenant="  ocid1.tenancy.oc1..aaaa-tenant-A  ")
    assert a.namespace == a_ws.namespace
    assert a.tenant_hash == a_ws.tenant_hash


def test_render_manifests_labels_are_independent_dicts():
    # 各リソースの metadata.labels は独立した dict(共有しない)。1 つを patch しても他に波及しない。
    spec = build_deploy_spec(_composition(), settings=_settings(), image_url=_IMAGE,
                             tenant="ocid1.tenancy.oc1..aaaa-tenant-A")
    manifests = spec.render_manifests()
    labelled = [m for m in manifests if m.get("metadata", {}).get("labels")]
    assert len(labelled) >= 2
    labelled[0]["metadata"]["labels"]["jetuse.dev/patched"] = "x"
    assert all("jetuse.dev/patched" not in m["metadata"]["labels"] for m in labelled[1:])


@pytest.mark.parametrize("bad", ["", "   ", "\t"])
def test_empty_or_whitespace_tenant_rejected(bad):
    # 空/空白の tenant は fail-closed。env 展開ミスで複数テナントが
    # 同一 namespace に集約される事故を防ぐ。
    comp = _composition()
    with pytest.raises(DeploySpecError):
        build_deploy_spec(comp, settings=_settings(), image_url=_IMAGE, tenant=bad)


def test_sanitize_prefix_forces_rfc1035_leading_letter():
    # K8s Service 名は RFC 1035(先頭英字必須)。数字始まり prefix でも Service 名が有効になる。
    comp = _composition()
    spec = build_deploy_spec(comp, settings=_settings(), image_url=_IMAGE, prefix="123-demo")
    assert spec.prefix[0].isalpha()
    service = next(m for m in spec.render_manifests() if m["kind"] == "Service")
    assert service["metadata"]["name"][0].isalpha()
