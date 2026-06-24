"""VOICE-01: 議事録の後処理(分かち書き対策)とプロンプト構築の単体テスト"""

from jetuse_core.minutes import (
    MAX_TRANSCRIPT_CHARS,
    _join_tokens,
    _parse_time,
    _to_utterances,
    build_generation_messages,
)


def test_parse_time_variants():
    assert _parse_time("12.5s") == 12.5
    assert _parse_time("1500ms") == 1.5
    assert _parse_time(3) == 3.0
    assert _parse_time(None) == 0.0


def test_to_utterances_groups_by_speaker():
    tokens = [
        {"token": "本", "startTime": "0.0s", "endTime": "0.2s", "speakerIndex": 0},
        {"token": "日", "startTime": "0.2s", "endTime": "0.4s", "speakerIndex": 0},
        {"token": "は", "startTime": "0.4s", "endTime": "0.5s", "speakerIndex": 0},
        {"token": "賛", "startTime": "1.0s", "endTime": "1.2s", "speakerIndex": 1},
        {"token": "成", "startTime": "1.2s", "endTime": "1.4s", "speakerIndex": 1},
        {"token": "了", "startTime": "2.0s", "endTime": "2.1s", "speakerIndex": 0},
        {"token": "解", "startTime": "2.1s", "endTime": "2.3s", "speakerIndex": 0},
    ]
    utt = _to_utterances(tokens)
    assert [u["speaker"] for u in utt] == [0, 1, 0]
    # 日本語は分かち書きトークンをスペースなしで結合(SPIKE-06)
    assert utt[0]["text"] == "本日は"
    assert utt[1]["text"] == "賛成"
    assert utt[0]["start"] == 0.0
    assert utt[0]["end"] == 0.5


def test_to_utterances_handles_missing_speaker():
    tokens = [{"token": "あ", "startTime": "0s", "endTime": "1s"}]
    utt = _to_utterances(tokens)
    assert utt[0]["speaker"] == 0


def test_join_tokens_keeps_space_between_ascii_words():
    # 「API の 実装 is done」のような混在: ASCII語同士のみスペース維持
    assert _join_tokens(["API", "実装", "は", "done", "now"]) == "API実装はdone now"


def test_build_generation_messages_template_and_truncation():
    utterances = [
        {"speaker": 0, "start": 0.0, "end": 5.0, "text": "あ" * MAX_TRANSCRIPT_CHARS},
        {"speaker": 1, "start": 65.0, "end": 70.0, "text": "切り捨てられる発言"},
    ]
    msgs = build_generation_messages(utterances, "minutes", "meeting.mp3")
    assert msgs[0]["role"] == "system"
    body = msgs[1]["content"]
    assert "議事録" in body
    assert "[00:00] 話者1:" in body
    assert "meeting.mp3" in body
    assert "途中まで" in body  # 打ち切り注記の指示が入る
    assert "切り捨てられる発言" not in body
