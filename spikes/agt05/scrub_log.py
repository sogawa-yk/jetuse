"""E2E の実行ログを証跡（`run.log` / `deploy.log`）へ**伏字を通して**書き出す。

伏字の規則は `e2e.scrub` と**同じものを使う**。ログだけ別の手で伏せると、片方に
実エンドポイント・OCID・接続先（`DB_NAME` / `DSN`）が残る
（AGT-05 の review-2 で実際に blocker として指摘された）。

実行:
  env SPIKE_SCHEMA_PREFIX=JETUSE_AGT05 SPIKE_HOME=<秘密の置き場> \
      PYTHONPATH=spikes/ragm02:spikes/agt05:packages/api \
      .venv/bin/python spikes/agt05/scrub_log.py <実行ログのパス>
"""

import pathlib
import sys

from e2e import EVIDENCE, scrub


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: scrub_log.py <実行ログのパス>")
    text = scrub(pathlib.Path(sys.argv[1]).read_text())
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / "run.log").write_text(text)
    # deploy 相当（ADB 起動確認・スキーマ接続・マイグレーション・取り込み・ツール登録）。
    # 既にある deploy.log（`ops/start-adb-if-stopped.sh` の出力）は消さずに前に残す
    deploy = EVIDENCE / "deploy.log"
    head = scrub(deploy.read_text()) if deploy.exists() else ""
    deploy.write_text(head + text.split("== シナリオ1")[0])
    print(f"  wrote {EVIDENCE / 'run.log'} / {deploy}")


if __name__ == "__main__":
    main()
