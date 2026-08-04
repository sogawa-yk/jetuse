"""エージェントコンテナの設定受け渡し(PORT-03)。

公開スタックは環境変数を1本の JSON(`JETUSE_AGENT_CONFIG`)で渡す。OCI provider が
`environment_variables.value` を JSON としてしか受け付けないのに中身をそのまま送るため、
スカラーを個別に渡すと引用符ごとコンテナへ届いて壊れる(2026-07-29 実機確定)。
その展開ロジックをここで固定する。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agent-containers"))

import agent_env  # noqa: E402


def test_load_expands_json_object_into_environ(monkeypatch):
    monkeypatch.delenv("OCI_REGION", raising=False)
    monkeypatch.delenv("SEMSTORE_OCID", raising=False)
    applied = agent_env.load('{"OCI_REGION": "us-chicago-1", "SEMSTORE_OCID": "ocid1.x"}')
    assert applied == {"OCI_REGION": "us-chicago-1", "SEMSTORE_OCID": "ocid1.x"}
    # 引用符が値に混入していないこと(これが混入するのが provider の罠)。
    import os
    assert os.environ["OCI_REGION"] == "us-chicago-1"


def test_existing_environment_wins(monkeypatch):
    # ローカル実行や dev 配備の従来どおりの env 渡しを壊さない。
    monkeypatch.setenv("OCI_REGION", "ap-osaka-1")
    agent_env.load('{"OCI_REGION": "us-chicago-1"}')
    import os
    assert os.environ["OCI_REGION"] == "ap-osaka-1"


def test_empty_config_is_a_noop(monkeypatch):
    assert agent_env.load("") == {}
    assert agent_env.load("   ") == {}


@pytest.mark.parametrize("bad", ['{"a": ', '["a"]', '"just-a-string"'])
def test_malformed_config_fails_loudly(bad):
    # 半分だけ設定が効いた状態で起動すると、原因の分からない権限エラーとして現れる。
    with pytest.raises(RuntimeError):
        agent_env.load(bad)
