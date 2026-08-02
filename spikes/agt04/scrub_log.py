"""E2E の実行ログを証跡（`run.log` / `deploy.log`）へ**伏字を通して**書き出す。

伏字の規則は `e2e.scrub` と**同じものを使う**。ログだけ別の手で伏せると、片方に
実エンドポイントや OCID が残る（実際に、解決後 IP の URL が残ったことがある）。

実行:
  env SPIKE_SCHEMA_PREFIX=JETUSE_AGT04 SPIKE_HOME=<秘密の置き場> \
      PYTHONPATH=spikes/ragm02:spikes/agt04:packages/api \
      .venv/bin/python spikes/agt04/scrub_log.py <実行ログのパス>
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
    # deploy 相当（スキーマ接続・マイグレーション適用・取り込み・ツール登録）の抜粋
    (EVIDENCE / "deploy.log").write_text(text.split("== シナリオ1")[0])
    print(f"  wrote {EVIDENCE / 'run.log'} / {EVIDENCE / 'deploy.log'}")


if __name__ == "__main__":
    main()
