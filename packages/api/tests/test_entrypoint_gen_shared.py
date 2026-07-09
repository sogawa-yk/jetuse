"""entrypoint.sh の ORASEJAPAN プロファイル書き出し(SP3-07 — RM sensitive 変数 → コンテナ env →
~/.oci/config)。鍵材料の受け渡し経路なので実行検査を残す(uvicorn はスタブ)。

fail-closed 契約(review-1 M002): 材料が不完全/不正 base64 なら config を書かず
GEN_SHARED_PROFILE を落として起動を続ける(共有モデルは sign_proxy が 403、API は生存)。
"""

import base64
import os
import stat
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
ENTRYPOINT = REPO / "packages" / "api" / "entrypoint.sh"
PEM = "-----BEGIN PRIVATE KEY-----\nMII_dummy\n-----END PRIVATE KEY-----\n"

FULL_MATERIAL = {
    "GEN_SHARED_PROFILE": "ORASETEST",
    "GEN_SHARED_USER_OCID": "ocid1.user.oc1..u",
    "GEN_SHARED_TENANCY_OCID": "ocid1.tenancy.oc1..t",
    "GEN_SHARED_FINGERPRINT": "aa:bb",
    "GEN_SHARED_REGION": "ap-osaka-1",
    "GEN_SHARED_KEY_PEM_B64": base64.b64encode(PEM.encode()).decode(),
}


def _run(tmp_path, extra_env):
    home = tmp_path / "home"
    home.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    # スタブ uvicorn: exec 時点の実効 env を落とす(fail-closed の unset と鍵 env の非伝播を観測)
    envdump = tmp_path / "envdump.txt"
    (fake_bin / "uvicorn").write_text(
        "#!/bin/sh\n"
        f'echo "GEN_SHARED_PROFILE=${{GEN_SHARED_PROFILE:-}}" > {envdump}\n'
        f'echo "GEN_SHARED_KEY_PEM_B64=${{GEN_SHARED_KEY_PEM_B64:-}}" >> {envdump}\n'
        "exit 0\n")
    (fake_bin / "uvicorn").chmod(0o755)
    env = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "HOME": str(home),
        **extra_env,
    }
    res = subprocess.run(
        ["sh", str(ENTRYPOINT)], env=env, cwd=tmp_path,
        capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    return home / ".oci", envdump.read_text(), res.stdout


def test_writes_profile_when_material_present(tmp_path):
    oci_dir, envdump, _ = _run(tmp_path, dict(FULL_MATERIAL))
    cfg = (oci_dir / "config").read_text()
    assert "[ORASETEST]" in cfg
    assert "user=ocid1.user.oc1..u" in cfg
    assert "fingerprint=aa:bb" in cfg
    assert "tenancy=ocid1.tenancy.oc1..t" in cfg
    assert "region=ap-osaka-1" in cfg
    assert f"key_file={oci_dir}/gen_shared_key.pem" in cfg
    assert (oci_dir / "gen_shared_key.pem").read_text() == PEM
    # 鍵材料は 600(他プロセス/ユーザーから読めない)
    for f in ("config", "gen_shared_key.pem"):
        assert stat.S_IMODE((oci_dir / f).stat().st_mode) == 0o600
    lines = envdump.strip().splitlines()
    assert lines[0] == "GEN_SHARED_PROFILE=ORASETEST"   # 有効材料では profile は生きる
    assert lines[1] == "GEN_SHARED_KEY_PEM_B64="        # 鍵は API プロセスへ伝播させない(m002)


def test_noop_without_material(tmp_path):
    # 未設定 = 何も書かない(共有モデルは 403 のまま)
    oci_dir, envdump, _ = _run(tmp_path, {})
    assert not oci_dir.exists()
    assert envdump.strip().splitlines()[0] == "GEN_SHARED_PROFILE="


def test_partial_material_disables_shared(tmp_path):
    # user OCID 欠落 = 壊れたプロファイルを書かず、profile を落として 403 fail-closed
    env = dict(FULL_MATERIAL)
    del env["GEN_SHARED_USER_OCID"]
    oci_dir, envdump, out = _run(tmp_path, env)
    assert not (oci_dir / "config").exists()
    assert envdump.strip().splitlines() == [
        "GEN_SHARED_PROFILE=", "GEN_SHARED_KEY_PEM_B64="]
    assert "disabled" in out  # WARN を残す(無言でなく)


def test_invalid_base64_disables_shared(tmp_path):
    env = dict(FULL_MATERIAL)
    env["GEN_SHARED_KEY_PEM_B64"] = "%%%not-base64%%%"
    oci_dir, envdump, out = _run(tmp_path, env)
    assert not (oci_dir / "config").exists()
    assert envdump.strip().splitlines() == [
        "GEN_SHARED_PROFILE=", "GEN_SHARED_KEY_PEM_B64="]
    assert "disabled" in out
    # API は起動を続けている(returncode 0 は _run 内で検証済み)
