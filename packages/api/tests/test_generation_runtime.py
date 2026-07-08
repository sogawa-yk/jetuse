"""生成 runtime の安全ヘルパー(symlink 拒否・サイズ上限・モデル検証)の単体テスト。

podman/opencode を要する生成本体は E2E が担う。ここは fail-closed な境界(S1/N2/F2)だけを検査。
"""

import pytest

from jetuse_core import generation_runtime as gr


def test_reject_unsafe_symlink(tmp_path):
    (tmp_path / "ok.js").write_text("x")
    (tmp_path / "evil").symlink_to("/home/opc/.env")  # 非信頼生成が張る host への symlink
    with pytest.raises(RuntimeError, match="symlink"):
        gr._reject_unsafe(tmp_path)


def test_reject_unsafe_plain_tree_ok(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "b.js").write_text("y")
    gr._reject_unsafe(tmp_path)  # 例外なし


def test_read_tree_size_cap(tmp_path):
    (tmp_path / "big.bin").write_bytes(b"x" * 2048)
    with pytest.raises(RuntimeError, match="size cap"):
        gr._read_tree(tmp_path, cap=1024)


def test_read_tree_skips_symlinks(tmp_path):
    (tmp_path / "real.js").write_bytes(b"ok")
    (tmp_path / "link.js").symlink_to("/etc/hostname")
    out = gr._read_tree(tmp_path)
    assert set(out) == {"real.js"}  # symlink は読まない


def test_read_tree_file_count_cap(tmp_path, monkeypatch):
    # review-16 B: 大量ファイルはサイズ以前に数で fail-closed(API プロセス枯渇防止)
    monkeypatch.setattr(gr, "_MAX_FILES", 3)
    for i in range(5):
        (tmp_path / f"f{i}.txt").write_bytes(b"x")
    with pytest.raises(RuntimeError, match="file-count cap"):
        gr._read_tree(tmp_path)


def test_read_tree_entry_cap(tmp_path, monkeypatch):
    # review-16 B: rglob 実体化を総エントリ数で打ち切る
    monkeypatch.setattr(gr, "_MAX_TREE_ENTRIES", 2)
    for i in range(5):
        (tmp_path / f"f{i}.txt").write_bytes(b"x")
    with pytest.raises(RuntimeError, match="too many entries"):
        gr._read_tree(tmp_path)


def test_reject_unsafe_detects_fifo(tmp_path):
    # review-16 minor: FIFO は p.is_file()=False で従来素通り → S_ISREG で確実に拒否
    import os
    (tmp_path / "ok.js").write_text("x")
    os.mkfifo(tmp_path / "pipe")
    with pytest.raises(RuntimeError, match="non-regular file"):
        gr._reject_unsafe(tmp_path)


def test_attempt_build_excludes_out_of_src(tmp_path, monkeypatch):
    """review-16 B002: src 外の未検査ファイルは build ツリーに載らない(../evil.js を弾く)。"""
    scaffold = tmp_path / "scaffold"
    (scaffold / "src" / "api").mkdir(parents=True)
    (scaffold / "src" / "App.jsx").write_text("placeholder")
    (scaffold / "src" / "api" / "client.js").write_text("// trusted client")
    (scaffold / "package.json").write_text("{}")
    (scaffold / "index.html").write_text("<html>")
    (scaffold / "vite.config.js").write_text("export default {}")

    def fake_gen(workdir, model_oci_id, timeout_s):
        (workdir / "src" / "App.jsx").write_text("import './ok.js'")
        (workdir / "src" / "ok.js").write_text("export const x=1")
        (workdir / "evil.js").write_text("SECRET")  # src 外(層1未検査)
        return "genlog"

    captured = {}

    def fake_build(build_dir, model_key, timeout_s):
        captured["files"] = sorted(
            str(p.relative_to(build_dir)) for p in build_dir.rglob("*") if p.is_file())
        (build_dir / "dist").mkdir(exist_ok=True)
        (build_dir / "dist" / "index.html").write_bytes(b"<html>built")
        return "buildlog"

    monkeypatch.setattr(gr, "_generate_src", fake_gen)
    monkeypatch.setattr(gr, "_sandboxed_build", fake_build)
    res = gr._attempt({"title": "x"}, "gpt-oss-120b", "openai.gpt-oss-120b",
                      scaffold, gr.get_settings(), gr.time.monotonic() + 900, {})
    files = captured["files"]
    assert "evil.js" not in files                          # src 外は build に載らない
    assert any(f.endswith("App.jsx") for f in files)       # 検証済み src は載る
    assert "demo-plan.json" in files                       # 信頼入力は持ち込む(../demo-plan.json)
    assert "index.html" in res.dist_files


def test_build_frontend_rejects_unknown_model():
    # F2: 未知の generation_model は KeyError でなく明示的な RuntimeError(podman 到達前)
    with pytest.raises(RuntimeError, match="not in MODELS"):
        gr.build_frontend({"title": "x"}, model_key="totally-unknown-model-xyz")


def test_build_frontend_rejects_model_not_in_allowlist(monkeypatch):
    # F2: MODELS にあってもプロキシ allowlist 外なら podman 到達前に明示失敗(不透明な失敗回避)
    from jetuse_core.settings import get_settings
    monkeypatch.setenv("GENERATION_PROXY_URL", "http://proxy:8765/v1")
    monkeypatch.setenv("GENAI_MODEL_ALLOWLIST", "openai.some-other-model-only")
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="allowlist"):
            gr.build_frontend({"title": "x"}, model_key="gpt-oss-120b")
    finally:
        get_settings.cache_clear()


def test_sandboxed_build_isolation_flags(tmp_path, monkeypatch):
    """B001 回帰: build 相は使い捨てコンテナ内(network=none・鍵レス・RO node_modules・ハードキル)。

    ホストで直接ビルドすると生成物のビルド時コード実行でホスト .env 読出し/RCE を許す。
    """
    (tmp_path / "node_modules").mkdir()
    monkeypatch.setattr(gr, "_scaffold_dir", lambda: tmp_path)
    captured = {}

    class _P:
        stdout, stderr = "built", ""

    def _fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return _P()

    monkeypatch.setattr(gr, "_run", _fake_run)
    gr._sandboxed_build(tmp_path / "work", "gpt-oss-120b", 60)
    cmd = captured["cmd"]
    joined = " ".join(cmd)
    assert cmd[:2] == ["podman", "run"]
    assert "--network=none" in cmd                       # egress 遮断(鍵レス egress すら無し)
    assert ".oci" not in joined and "OCI_" not in joined  # OCI 認証を一切渡さない
    assert any("node_modules:ro" in a for a in cmd)       # 共有 node_modules は RO(ホスト非改変)
    assert "timeout" in cmd and "-sKILL" in cmd           # ハードキル(N1/N7)
    assert f"{tmp_path / 'work'}:/work" in joined          # 見えるのは使い捨て workdir のみ
