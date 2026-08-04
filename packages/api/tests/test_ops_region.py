"""AGT-06: ops/_region.sh のリージョン解決。

`ops/*.sh` はリージョンと OCIR ホストを**別々に**直書きしていたため、リージョンだけ
変えても**イメージは大阪の OCIR を指したまま**になっていた(review-1 B003/B004)。
「配備先を変えたのに片方だけ付いてこない」を二度とやらないよう、対応表と
fail-closed をここで固定する。
"""

import pathlib
import subprocess

REGION_SH = pathlib.Path(__file__).resolve().parents[3] / "ops" / "_region.sh"
OPS = REGION_SH.parent


def sh(script: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", f". {REGION_SH}\n{script}"],
        capture_output=True, text=True,
        cwd=REGION_SH.parents[1],
        env={"PATH": "/usr/bin:/bin", **(env or {})},
    )


def test_ocir_host_matches_region():
    """リージョン → OCIR ホストの対応。ここがずれると pull できないイメージを push する。"""
    for region, host in (("us-chicago-1", "ord.ocir.io"), ("ap-osaka-1", "kix.ocir.io"),
                         ("ap-tokyo-1", "nrt.ocir.io"), ("us-ashburn-1", "iad.ocir.io")):
        r = sh(f'jetuse_ocir_host {region}')
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == host


def test_unsupported_region_fails_closed():
    """未対応リージョンは**推測しない**。黙って既定へ落ちると別リージョンへ配備する。"""
    r = sh('jetuse_ocir_host zz-nowhere-1')
    assert r.returncode != 0
    assert "OCIR_HOST" in r.stderr


def test_ocir_host_can_be_overridden():
    r = sh('jetuse_ocir_host ap-osaka-1', env={"OCIR_HOST": "xyz.ocir.io"})
    assert r.stdout.strip() == "xyz.ocir.io"


def test_region_prefers_env_over_dotenv():
    r = sh('jetuse_region', env={"OCI_REGION": "us-chicago-1"})
    assert r.stdout.strip() == "us-chicago-1"


def test_use_cli_region_exports_for_oci_cli():
    """`REGION` を計算しただけでは oci CLI は既定プロファイルを向いたまま(B003)。"""
    r = sh('jetuse_use_cli_region us-chicago-1; echo "$OCI_CLI_REGION"')
    assert r.stdout.strip() == "us-chicago-1"


def test_region_from_tfvars(tmp_path):
    """tfvars の region が、そのスタックの配備先の正。

    以前は `sed -E ... \\s` で抜いていたが、BSD sed(macOS)は `\\s` を解さず
    **行まるごとをリージョン名として返していた**(静かに壊れる)。
    """
    f = tmp_path / "x.tfvars"
    f.write_text('dev_name = "x"\nregion   = "us-chicago-1"\nadb_user = "A"\n')
    assert sh(f'jetuse_region_from_tfvars {f}').stdout.strip() == "us-chicago-1"
    # 抜いた値が OCIR ホストへそのまま渡せること(壊れていれば fail-closed で落ちる)
    r = sh(f'jetuse_ocir_host "$(jetuse_region_from_tfvars {f})"')
    assert r.returncode == 0 and r.stdout.strip() == "ord.ocir.io"


def test_region_from_tfvars_missing_key_is_empty(tmp_path):
    """region 指定が無い tfvars では空を返し、呼び出し側の既定へ委ねる。"""
    f = tmp_path / "y.tfvars"
    f.write_text('dev_name = "y"\n')
    assert sh(f'jetuse_region_from_tfvars {f}').stdout.strip() == ""
    assert sh('jetuse_region_from_tfvars /no/such/file').returncode == 0


def test_deploy_scripts_pin_the_cli_region():
    """リージョンを解決するスクリプトは、必ず oci CLI も同じリージョンへ固定する。"""
    for name in ("deploy-agent-containers.sh", "redeploy-agent-env.sh",
                 "deploy-hosted-agent.sh", "dev-env-up.sh"):
        text = (OPS / name).read_text()
        assert "jetuse_use_cli_region" in text, f"{name} が CLI リージョンを固定していない"


def test_no_hardcoded_region_or_registry_in_deploy_scripts():
    """リージョン/OCIR の直書きを ops/ に戻さない(_region.sh の対応表だけが持つ)。"""
    for path in OPS.glob("*.sh"):
        if path.name == "_region.sh":
            continue  # 対応表そのもの
        for line in path.read_text().splitlines():
            if line.lstrip().startswith("#"):
                continue  # コメント内の説明は対象外
            assert "kix.ocir.io" not in line, f"{path.name}: OCIR の直書き -> {line.strip()}"
            assert "ap-osaka-1" not in line, f"{path.name}: リージョンの直書き -> {line.strip()}"
