"""`JETUSE_AGENT_CONFIG`(JSON)を os.environ へ展開する(PORT-03)。

公開スタックの Terraform は、コンテナ設定を **1本の JSON 文字列**として渡す。
1変数ずつ渡せない理由(2026-07-29 実機確定):

- OCI provider は `environment_variables.value` を「JSON として妥当な文字列」しか受け付けないが、
  中身をアンマーシャルせずそのまま API へ送る。API も文字列を verbatim に保存する。
- そのため `jsonencode("us-chicago-1")` を渡すと、コンテナには引用符ごと `"us-chicago-1"` が届く。
- JSON **オブジェクト**なら `{"OCI_REGION":"us-chicago-1"}` がそのまま往復する。

既に環境変数が設定されていればそちらを優先する(ローカル実行・dev 配備の従来どおりの
env 渡しを壊さない)。JSON が壊れているときは黙って無視せず即座に失敗させる
(設定が半分だけ効いた状態で起動すると、原因の分からない権限エラーとして現れるため)。

各ランナーが os.environ を読む前に import されている必要がある。
"""

import json
import os

CONFIG_ENV = "JETUSE_AGENT_CONFIG"


def load(source: str | None = None) -> dict[str, str]:
    """設定 JSON を os.environ へ展開し、適用したキーと値を返す。"""
    raw = source if source is not None else os.environ.get(CONFIG_ENV, "")
    if not raw.strip():
        return {}
    try:
        cfg = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"{CONFIG_ENV} is not valid JSON: {e}") from e
    if not isinstance(cfg, dict):
        raise RuntimeError(f"{CONFIG_ENV} must be a JSON object, got {type(cfg).__name__}")
    applied = {}
    for k, v in cfg.items():
        if v is None:
            continue
        value = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
        os.environ.setdefault(k, value)
        applied[k] = value
    return applied


load()
