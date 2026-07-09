"""フロント生成 runtime(specs/19 §4.1・ADR-0023 の 2 相分離)。

- **生成相(非信頼)**: scaffold + demo-plan.json を使い捨て podman コンテナで opencode に実装させる。
  OCI 認証情報を渡さない(鍵レス S2)。egress は署名プロキシ(host gateway)経由でのみ OCI へ到達。
  CPU/メモリ/PID 上限 + 15 分ハードキル(§4.2 N1/N7)。生成相は `src/` だけを書く(build-free)。
- **信頼ビルド相**: クリーン scaffold の保護原本 + 生成 `src/` を信頼ツールチェーンで `vite build`。
  生成物ツリーに **symlink/非通常ファイルがあれば拒否**(非信頼コンテナがホストの .env/OCI 設定へ
  symlink を張り、復元/ビルドで host ファイルを上書き・読み出す攻撃を fail-closed で遮断 — S1)。
  VITE_DEMO_MODEL(デモ実行時チャットの公開モデルキー = 共用 MODELS。生成モデルとは別 — SP3-06)
  をビルド時定数として焼き込む(F2 — client.js が参照)。

`build_frontend` = builder_generate のシーム(返り値 = GenerationResult)。イメージ(node digest +
opencode 版固定)・プロンプト版を generator に記録(N6 再現性)。

"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from .builder_generate import GenerationResult
from .gen_models import GEN_MODELS, GenModelDef
from .models import DEFAULT_MODEL as DEMO_RUNTIME_MODEL
from .settings import get_settings

logger = logging.getLogger("jetuse.generation_runtime")

# 生成相が変更してはならない保護原本(信頼ビルドは常にクリーン原本を使う)。
_PROTECTED = ("package.json", "package-lock.json", "vite.config.js", "index.html",
              "src/api/client.js")
_SRC_PROTECTED = frozenset({"api/client.js"})  # 層1検査で除外(src/ 相対キー)
_GEN_PROMPT = ("demo-plan.json に従いデモ画面を実装してください。"
               "AGENTS.md の厳守事項(client.js 経由通信・src のみ編集・build しない)に従うこと。")
_PROMPT_VERSION = "1"                 # N6 再現性: プロンプト版数(_GEN_PROMPT + AGENTS.md tied)
_OPENCODE_VERSION = "1.17.15"         # 生成イメージに固定(N6・供給網固定 #15)
# responses 系 provider の版固定(N6・供給網 — review-2 M001)。opencode がコンテナ内で実行時
# npm 取得するため、無指定だと実行日時で実装が変わり再現性が崩れる。SP3-06 スパイクで実証した
# 版に固定し、generator メタ(N6)にも記録する。イメージへの事前導入は residual(Container
# Instance 化タスクの範囲)。chat 系 @ai-sdk/openai-compatible は opencode 本体に同梱 =
# _OPENCODE_VERSION で固定済み。
_AI_SDK_OPENAI = "@ai-sdk/openai@4.0.9"
_NODE_BASE = ("node:22-slim@sha256:"  # ベースイメージ digest 固定(再現性 #15)
              "a149cd71dccd68704a07d4e4ca3e610c27301852b0f556865cfdb6e2856f8bed")
_MAX_BUNDLE_BYTES = 20 * 1024 * 1024  # N2: バンドル合計 ≤20MB(超過は failed)
_MAX_LOG_BYTES = 16 * 1024            # N4: 保存する生成ログの上限(末尾を残す)
_MAX_ATTEMPTS = 2                     # build 失敗(生成非決定性)の再試行回数
_MAX_TREE_ENTRIES = 20000            # 生成物ツリー総エントリ上限(rglob 実体化 DoS の早期打ち切り)
_MAX_FILES = 4000                    # バンドル/src のファイル数上限(大量ファイルの資源枯渇防止)


def _scaffold_dir() -> Path:
    s = get_settings()
    if s.generation_scaffold_dir:
        return Path(s.generation_scaffold_dir)
    return Path(__file__).resolve().parents[3] / "spikes" / "sp3_03_scaffold"


def _base_env() -> dict:
    return {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/home/opc")}


def _run(cmd: list[str], *, cwd: Path | None = None, timeout: int,
         env: dict | None = None, check: bool = True,
         input: str | None = None) -> subprocess.CompletedProcess:
    logger.info("run: %s", " ".join(cmd[:6]))
    return subprocess.run(  # noqa: S603 — 引数は配列(shell 経由なし)
        cmd, cwd=cwd, timeout=timeout, env=env or _base_env(), check=check,
        input=input, capture_output=True, text=True,
    )


def _walk(root: Path):
    """root 配下を列挙。総エントリ数を上限で打ち切る(非信頼生成が大量ファイルを作る DoS 防止)。"""
    n = 0
    for p in root.rglob("*"):
        n += 1
        if n > _MAX_TREE_ENTRIES:
            raise RuntimeError(f"generated tree has too many entries (> {_MAX_TREE_ENTRIES})")
        yield p


def _reject_unsafe(root: Path) -> None:
    """生成物ツリーに symlink/非通常ファイルがあれば拒否(S1 — host ファイル上書き/読出しを遮断)。"""
    import stat as _stat
    for p in _walk(root):
        if p.is_symlink():
            raise RuntimeError(f"generated tree has a symlink (rejected): {p.relative_to(root)}")
        if p.is_dir():
            continue
        # symlink は上で除外済み。残る非通常ファイル(FIFO/デバイス/ソケット)は S_ISREG で確実に検出
        # (p.is_file() は FIFO で False になり従来の判定は素通りだった — review-16 minor)。
        if not _stat.S_ISREG(p.stat().st_mode):
            raise RuntimeError(f"generated tree has a non-regular file: {p.relative_to(root)}")


def _read_tree(root: Path, *, cap: int = _MAX_BUNDLE_BYTES) -> dict[str, bytes]:
    """root 配下の通常ファイルを {相対 posix パス: bytes}。合計/数の上限超過は例外(N2 の有界読み)。

    サイズは **読み込む前に stat で判定**(巨大ファイルを API プロセスへ丸読みしない)。
    ファイル数・総エントリ数も上限(大量/巨大ファイルによる資源枯渇を fail-closed — review-16 B)。
    """
    out: dict[str, bytes] = {}
    total = count = 0
    if not root.is_dir():
        return out
    for p in sorted(_walk(root)):
        if p.is_symlink() or not p.is_file():
            continue
        count += 1
        if count > _MAX_FILES:
            raise RuntimeError(f"bundle exceeds file-count cap ({_MAX_FILES})")
        total += p.stat().st_size  # 読む前にサイズで cap 判定
        if total > cap:
            raise RuntimeError(f"bundle exceeds size cap ({cap} bytes)")
        out[p.relative_to(root).as_posix()] = p.read_bytes()
    return out


def _tail(text: str) -> str:
    return text[-_MAX_LOG_BYTES:] if text else ""


def _remaining(deadline: float) -> int:
    r = int(deadline - time.monotonic())
    if r <= 1:
        raise RuntimeError("generation deadline exceeded")
    return r


def _ensure_image(image: str) -> None:
    """生成コンテナイメージ(node digest + opencode 版固定)を用意する。無ければ build。"""
    if subprocess.run(["podman", "image", "exists", image],  # noqa: S603,S607
                      check=False).returncode == 0:
        return
    containerfile = (
        f"FROM {_NODE_BASE}\nRUN npm i -g opencode-ai@{_OPENCODE_VERSION}\nWORKDIR /work\n")
    logger.info("building generation image %s", image)
    with tempfile.TemporaryDirectory() as ctx:  # 空コンテキスト(COPY 無し)
        _run(["podman", "build", "-t", image, "-f", "-", ctx], timeout=1800,
             input=containerfile)


def _provider_npm(model: GenModelDef) -> str:
    """モデルの api 種別 → opencode provider パッケージ(N6 の generator.provider に記録)。"""
    return _AI_SDK_OPENAI if model.api == "responses" else "@ai-sdk/openai-compatible"


def _opencode_config(model: GenModelDef, proxy_url: str) -> str:
    """OpenCode の provider 設定。api 種別で provider パッケージを切り替える(SP3-06):

    chat = @ai-sdk/openai-compatible(chat/completions のみ話せる — 自テナンシ 120b)。
    responses = @ai-sdk/openai 版固定(responses を話す — gpt-5 系は全てこちら: codex/pro 系は
    chat 自体が 404、gpt-5.5/5.6 系も function tools を chat が拒否。
    実証は docs/verification/SP3-06.md)。
    """
    npm = _provider_npm(model)
    return json.dumps({
        "$schema": "https://opencode.ai/config.json",
        "provider": {"oci": {
            "npm": npm,
            "name": "OCI GenAI (signed proxy)",
            "options": {"baseURL": proxy_url, "apiKey": "dummy"},
            "models": {model.oci_id: {"name": model.oci_id, "tool_call": True,
                                      "limit": {"context": 128000, "output": 32000}}},
        }},
        "model": f"oci/{model.oci_id}",
        "permission": {"edit": "allow", "bash": "allow", "webfetch": "deny"},
    }, ensure_ascii=False)


def _generate_src(workdir: Path, model_oci_id: str, timeout_s: int) -> str:
    """生成相: 使い捨て podman コンテナで opencode に src/ を書かせる。出力の末尾を返す。"""
    s = get_settings()
    name = f"jetuse-gen-{uuid.uuid4().hex[:12]}"
    cmd = [
        "podman", "run", "--rm", "--name", name,
        f"--network={s.generation_container_network}",
        f"--cpus={s.generation_cpus}", f"--memory={s.generation_memory}",
        f"--pids-limit={s.generation_pids_limit}",
        "--security-opt", "no-new-privileges",
        # 鍵レス: OCI 認証(~/.oci)も OCI_* env も渡さない。HOME=/work(opencode 作業域は書込可)
        "-v", f"{workdir}:/work:Z", "-w", "/work", "-e", "HOME=/work",
        s.generation_image,
        "timeout", "-sKILL", str(timeout_s),  # コンテナ内ハードキル(PID1=timeout でコンテナも終了)
        "opencode", "run", "--model", f"oci/{model_oci_id}", _GEN_PROMPT,
    ]
    try:
        p = _run(cmd, timeout=timeout_s + 30)
        return _tail((p.stdout or "") + (p.stderr or ""))
    except subprocess.TimeoutExpired:
        subprocess.run(["podman", "rm", "-f", name],  # noqa: S603,S607
                       check=False, capture_output=True)
        raise RuntimeError(f"generation timed out after {timeout_s}s") from None
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"opencode generation failed: {_tail(e.stderr or '')}") from e


def _sandboxed_build(workdir: Path, demo_model: str, timeout_s: int) -> str:
    """ビルド相を**使い捨てコンテナ内**で行う(S1 — build も非信頼サンドボックス)。出力末尾を返す。

    生成物はビルド時コード実行(postcss.config 等の自動読込)や作業域外 import を仕込みうる。
    ホストで build するとホストの .env/設定を読み・任意コード実行される。よって:
    - `--network=none`・OCI 認証なし(鍵レス)・resource 上限 + timeout(封じ込め)。
    - node_modules は RO bind(ホスト scaffold を書き換えさせない)。vite キャッシュは tmpfs。
    - 見えるのは /work(使い捨て)+ RO node_modules のみ = ホスト FS へ到達不能。
    """
    s = get_settings()
    node_modules = _scaffold_dir() / "node_modules"
    if not node_modules.is_dir():
        raise RuntimeError(
            "scaffold node_modules missing — run `npm ci` at build/deploy time (no online build)")
    name = f"jetuse-build-{uuid.uuid4().hex[:12]}"
    cmd = [
        "podman", "run", "--rm", "--name", name, "--network=none",
        f"--cpus={s.generation_cpus}", f"--memory={s.generation_memory}",
        f"--pids-limit={s.generation_pids_limit}", "--security-opt", "no-new-privileges",
        # label=disable: 共有 RO node_modules を再ラベルせず exec 可に(隔離境界は netns+鍵レス+
        # 使い捨て。SELinux ラベルは境界でない — ホスト scaffold のラベルを書き換えない)。
        "--security-opt", "label=disable",
        "-v", f"{workdir}:/work", "-v", f"{node_modules}:/work/node_modules:ro",
        # vite 6 の書込域(config timestamp = .vite-temp / 依存キャッシュ = .vite)を tmpfs で開ける
        "--tmpfs", "/work/node_modules/.vite", "--tmpfs", "/work/node_modules/.vite-temp",
        "--tmpfs", "/tmp",
        "-w", "/work", "-e", "HOME=/work", "-e", f"VITE_DEMO_MODEL={demo_model}",
        s.generation_image,
        # vite は node 直呼び(.bin/vite shebang exec 回避)。esbuild native は exec 可(label=disable)
        "timeout", "-sKILL", str(timeout_s), "node", "node_modules/vite/bin/vite.js", "build",
    ]
    try:
        p = _run(cmd, timeout=timeout_s + 30)
        return _tail((p.stdout or "") + (p.stderr or ""))
    except subprocess.TimeoutExpired:
        subprocess.run(["podman", "rm", "-f", name],  # noqa: S603,S607
                       check=False, capture_output=True)
        raise RuntimeError(f"build timed out after {timeout_s}s") from None
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"vite build failed: {_tail((e.stderr or '') + (e.stdout or ''))}") from e


def build_frontend(plan: dict, *, model_key: str) -> GenerationResult:
    """プラン → 生成 SPA(src)→ ビルド(dist)。builder_generate のシーム実体。

    build 失敗(生成品質の当たり外れ)は最大 _MAX_ATTEMPTS まで生成からやり直す。全体で単一 deadline
    (N1 = 15 分)を共有し、各相へ残時間を渡す。返り値に生成ログ(N4)と generator メタ(N6)を含む。

    バックエンドは settings.generation_runtime で切替(SP3-08): podman = ローカル開発
    (host podman 近似 — 従来経路のまま)、oci-ci = 生成ごとの使い捨て Container Instance
    (ADR-0023 §1 B' — デプロイ環境の正)。検証・deadline・再試行の枠は両者共通。
    """
    s = get_settings()
    # F2: 設定/入力値の妥当性(未知キーは KeyError でなく明示失敗)。レジストリ = 生成専用
    # (gen_models — プロキシ allowlist と同一の単一真実源。共用 MODELS とは分離 — SP3-06)
    model = GEN_MODELS.get(model_key)
    if model is None:
        raise RuntimeError(f"generation_model '{model_key}' not in generation model registry")
    if not s.generation_proxy_url:  # #13: 環境依存値は .env 必須(fail-fast)
        raise RuntimeError("generation_proxy_url is not configured (.env: GENERATION_PROXY_URL)")
    # 共有テナンシモデルは auth/compartment 未設定なら runtime 到達前に明示失敗(fail-closed —
    # プロキシ側も同条件で 403 するが、不透明に落ちる前にここで止める)
    if model.shared and not (s.gen_shared_profile and s.gen_shared_compartment_ocid):
        raise RuntimeError(
            f"generation_model '{model_key}' requires GEN_SHARED_PROFILE / "
            "GEN_SHARED_COMPARTMENT_OCID (.env)")
    runtime = s.generation_runtime
    if runtime == "oci-ci":
        from . import generation_runtime_ci as _ci

        _ci.check_settings(s)  # subnet/AD/イメージ URL 未配線なら CI 作成前に fail-fast

        def _try(deadline: float, generator: dict) -> GenerationResult:
            return _ci.attempt(plan, model, s, deadline, generator)
    elif runtime == "podman":
        scaffold = _scaffold_dir()
        if not scaffold.is_dir():
            raise RuntimeError(f"scaffold not found: {scaffold}")
        _ensure_image(s.generation_image)  # インフラ準備(キャッシュ)— job deadline の外

        def _try(deadline: float, generator: dict) -> GenerationResult:
            return _attempt(plan, model, scaffold, s, deadline, generator)
    else:
        raise RuntimeError(f"unknown generation_runtime '{runtime}' (podman | oci-ci)")

    deadline = time.monotonic() + s.generation_timeout_s
    generator = {"model": model_key, "prompt_version": _PROMPT_VERSION,
                 "opencode_version": _OPENCODE_VERSION,
                 "provider": _provider_npm(model)}  # N6: 版固定 provider も記録(M001)
    last_err: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            return _try(deadline, generator)
        except RuntimeError as e:
            last_err = e
            logger.warning("generation attempt %d/%d failed: %s",
                           attempt, _MAX_ATTEMPTS, str(e)[:200])
    raise RuntimeError(f"generation failed after {_MAX_ATTEMPTS} attempts: {last_err}")


def _attempt(plan: dict, model: GenModelDef, scaffold: Path,
             s, deadline: float, generator: dict) -> GenerationResult:
    tmp = Path(tempfile.gettempdir())
    work = tmp / f"jetuse-gen-{uuid.uuid4().hex[:12]}"    # 生成相(非信頼)
    build = tmp / f"jetuse-bld-{uuid.uuid4().hex[:12]}"   # ビルド相(クリーン scaffold + 検証 src)
    plan_json = json.dumps(plan, ensure_ascii=False)
    _ex = shutil.ignore_patterns("node_modules", ".git", "dist", "opencode.json")
    try:
        # --- 生成相: scaffold + demo-plan.json で src を生成させる(コンテナ内・鍵レス) ---
        shutil.copytree(scaffold, work, ignore=_ex)
        (work / "demo-plan.json").write_text(plan_json, "utf-8")
        (work / "opencode.json").write_text(
            _opencode_config(model, s.generation_proxy_url), "utf-8")
        gen_log = _generate_src(work, model.oci_id, _remaining(deadline))

        # 非信頼 src を安全確認(symlink/非通常拒否 — S1)。src 内保護原本(client.js)は信頼版へ。
        _reject_unsafe(work / "src")
        for rel in _SRC_PROTECTED:
            trusted = scaffold / "src" / rel
            if trusted.is_file():
                dst = work / "src" / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                if dst.exists() or dst.is_symlink():
                    dst.unlink()  # symlink 追従で host ファイルを上書きしない
                shutil.copy2(trusted, dst)
        src_files = _read_tree(work / "src")  # 層1検査対象 = src のみ(サイズ/数上限)

        # --- ビルド相: クリーン scaffold + **検証済み src だけ**で vite build ---
        # 生成相は /work 全体を書けるが、ここへ持ち込むのは信頼 scaffold + 層1検査済み src + 信頼
        # demo-plan.json のみ。src 外の未検査ファイル(../evil.js 等)は build ツリーに存在せず、
        # それを import する生成コードは build が解決できず失敗する = fail-closed(review-16 B002)。
        shutil.copytree(scaffold, build, ignore=shutil.ignore_patterns(
            "node_modules", ".git", "dist", "opencode.json", "src"))
        (build / "demo-plan.json").write_text(plan_json, "utf-8")  # 信頼入力(server 書込)
        for rel, data in src_files.items():
            dst = build / "src" / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)
        try:
            # VITE_DEMO_MODEL = デモ実行時チャットのモデル(共用 MODELS のキー)。生成モデル
            # (gen_models キー)とは別名前空間 — 混ぜると生成 SPA のチャットが 400(SP3-06)
            build_log = _sandboxed_build(build, DEMO_RUNTIME_MODEL, _remaining(deadline))
            dist_files = _read_tree(build / "dist")
            if "index.html" not in dist_files:
                raise RuntimeError("build produced no index.html")
        except RuntimeError as e:
            # build 失敗でも opencode 生成ログ(N4)を失わない — 失敗理由に添えて再送出。
            raise RuntimeError(f"{e} | opencode: {_tail(gen_log)}") from e
        log = _tail(f"# opencode\n{gen_log}\n\n# build\n{build_log}")
        return GenerationResult(src_files, dist_files, _SRC_PROTECTED, log, generator)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(build, ignore_errors=True)
