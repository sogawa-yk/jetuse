"""証跡ログから環境依存の実値を伏せる（CLAUDE.md「OCID・エンドポイント実値をコミットしない」）。

実行ログはそのまま runs/ に置いてコミット対象になるため、
テナンシ/コンパートメント OCID と Object Storage ネームスペースを置換する。
検証の意味を変えない範囲（識別子だけ）に限る＝件数・スコア・エラー本文は触らない。

実行: PYTHONPATH=spikes/spike_m1 .venv/bin/python \
        spikes/spike_m1/redact_evidence.py runs/<run-id>/e2e
"""

import pathlib
import re
import sys

from common import load_env

OCID_RE = re.compile(r"(ocid1\.[a-z]+\.oc1\.[a-z0-9-]*\.?)[a-z0-9]{20,}")
# 環境依存の識別子（証跡としては「あった」ことが分かれば十分で、実値は要らない）
VS_RE = re.compile(r"vs_[a-z]{3}_[a-z0-9]{20,}")
FILE_RE = re.compile(r"file-[a-z]{3}-[0-9a-f-]{30,}")
DBTOKEN_RE = re.compile(r"\b[A-Z0-9]{12,}_(JETUSELOOP2)\b")
# サービスエンドポイントはリージョンをプレースホルダにする（規約: 実値をコミットしない）
REGION_HOST_RE = re.compile(
    r"(objectstorage|inference\.generativeai|generativeai)\.[a-z]{2}-[a-z]+-\d")


# codex-review は証跡を `tail -c 8000` で添付する。日本語ログだとこの切り口が
# マルチバイト文字の途中に落ち、codex が "input is not valid UTF-8" で異常終了する（実機で発生）。
# 末尾に空白を 1〜3 バイト足して切り口を文字境界へずらす（内容は変えない）。
# 先頭に足しても切り口は末尾から数えるので動かない（実際にやって無限ループにした）。
TAIL_BYTES = 8000


def align_tail_boundary(path: pathlib.Path) -> bool:
    original = path.read_bytes()
    data = original
    # UTF-8 は最大 4 バイトなので、3 バイトずらせば必ず境界に乗る（+1 で余裕を見る）
    for _ in range(4):
        if len(data) <= TAIL_BYTES or (data[-TAIL_BYTES] & 0xC0) != 0x80:
            break
        data += b" "
    if data == original:
        return False
    path.write_bytes(data)
    return True


def main() -> None:
    target = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "runs")
    env = load_env()
    ns = env.get("OS_NAMESPACE", "")
    changed = 0
    # 台帳（*-names.json は名前だけの写し）は伏字化しない。ID を壊すと片付けが不能になる。
    skip = {"created-resources.json"}
    for path in sorted(target.rglob("*")):
        if not path.is_file() or path.name in skip:
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            print(f"  skip (not utf-8): {path}")
            continue
        new = OCID_RE.sub(r"\1<REDACTED>", text)
        new = VS_RE.sub("vs_<REDACTED>", new)
        new = FILE_RE.sub("file-<REDACTED>", new)
        new = DBTOKEN_RE.sub(r"<DB_TOKEN>_\1", new)
        new = REGION_HOST_RE.sub(r"\1.<region>", new)
        if ns:
            new = new.replace(ns, "<OS_NAMESPACE>")
        if new != text:
            path.write_text(new)
            changed += 1
            print(f"  redacted {path}")
        if align_tail_boundary(path):
            print(f"  tail 境界を調整: {path}")
    print(f"done ({changed} files redacted)")


if __name__ == "__main__":
    main()
