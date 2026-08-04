"""選べるほうのエンジン（VLM）が実機で通ることの確認（PREP-03・付録）。

既定は Document Understanding なので、E2E 本体（`e2e.py`）はすべて DU で走る。
**選択肢として提供した以上、動かないまま提供しない**ので、画像 1 枚だけ VLM で通す
（ページごとに LLM を呼ぶ = コストが掛かるため、確認はこの 1 枚に留める）。

実行: 環境変数は e2e.py と同じ。
  env $E PYTHONPATH=spikes/prep03:spikes/ragm02:packages/api \\
      .venv/bin/python spikes/prep03/vlm_check.py
"""

import json
import sys

from e2e import IMAGE_NAME, OcrCounter, ensure_spike_store, fence, squash, use_task_schema, write
from fixtures import LOT_NUMBER, VERDICT, scan_png


def main() -> None:
    use_task_schema()
    from fastapi.testclient import TestClient
    from service.main import app

    ensure_spike_store()
    ocr = OcrCounter().install()
    client = TestClient(app)
    res = client.post("/api/extract",
                      files={"file": (IMAGE_NAME, scan_png(), "image/png")},
                      data={"ocr_engine": "vlm"})
    chunks = res.json().get("chunks", [])
    text = "\n".join(c["text"] for c in chunks)
    read_ok = squash(LOT_NUMBER) in squash(text) and VERDICT in text
    pages_ok = [c["sheet"] for c in chunks] == ["p.1"]
    engine_ok = [c["engine"] for c in ocr.calls] == ["ocr_vlm"]
    ok = res.status_code == 200 and read_ok and pages_ok and engine_ok

    write("scenario-4.md", f"""# シナリオ4（付録）— 明示指定した VLM エンジンが実機で通る

既定は Document Understanding（シナリオ0〜3 はすべて DU）。利用者が
`ocr_engine=vlm` を明示したときだけ VLM（ビジョン LLM）へ切り替わる。**自動では切り替えない**。

同じ画像 `{IMAGE_NAME}` を `POST /api/extract` へ `ocr_engine=vlm` で渡した:

- HTTP ステータス: **{res.status_code}** / チャンク数: **{len(chunks)}**
- 呼ばれたエンジン: `{[c['engine'] for c in ocr.calls]}`（`ocr_vlm` = VLM 経路）: **{engine_ok}**
- 出典がページ番号: **{pages_ok}**
- 本文を読めている（`{LOT_NUMBER}` と判定 `{VERDICT}`）: **{read_ok}**

{fence(text)}

呼び出しの記録:

{fence(json.dumps(ocr.calls, ensure_ascii=False, indent=2))}

判定: **{'PASS' if ok else 'FAIL'}**

> VLM は**ページごとに LLM を呼ぶ**ので、ページ数に比例してコストが掛かる。
> 既定にしない理由は `docs/verification/PREP-03.md`（エンジン既定の判断根拠）。
""")
    print("PASS" if ok else "FAIL")
    # **失敗を終了コードに出す**（0 のままだと自動実行側が失敗を成功と読む）
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
