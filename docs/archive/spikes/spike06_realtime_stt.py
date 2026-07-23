"""SPIKE-06: リアルタイムSTT (WebSocket) 検証。

16kHz mono PCM (meeting1_16k.wav) をWhisperリアルタイムへストリーミングし、
partial/final の受信イベントを記録する。

実行: .venv/bin/python spikes/spike06_realtime_stt.py
"""
import asyncio
import time
import wave
from pathlib import Path

from oci.config import from_file
from oci_ai_speech_realtime import (RealtimeParameters, RealtimeSpeechClient,
                                    RealtimeSpeechClientListener)

REPO = Path(__file__).resolve().parent.parent
ENV = dict(l.split("=", 1) for l in (REPO / ".env").read_text().splitlines() if "=" in l)
WAV = Path(__file__).resolve().parent / "data" / "meeting1_16k.wav"

events = []


class Listener(RealtimeSpeechClientListener):
    def on_result(self, result):
        tx = result.get("transcriptions", [{}])[0]
        events.append(("result", tx.get("isFinal"), tx.get("transcription", "")[:80]))
        print(f"[result] final={tx.get('isFinal')} text={tx.get('transcription','')[:80]}")

    def on_ack_message(self, ackmessage):
        pass

    def on_connect(self):
        print("[connect] websocket opened")
        events.append(("connect", None, None))

    def on_connect_message(self, connectmessage):
        print(f"[connect_message] {str(connectmessage)[:160]}")
        events.append(("connect_message", None, str(connectmessage)[:160]))

    def on_network_event(self, ackmessage):
        pass

    def on_error(self, error):
        print(f"[error] {error}")
        events.append(("error", None, str(error)[:200]))

    def on_close(self, error_code, error_message):
        print(f"[close] {error_code} {error_message}")
        events.append(("close", error_code, error_message))


async def send_audio(client):
    with wave.open(str(WAV), "rb") as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1
        chunk_frames = 1600  # 100ms
        while True:
            data = w.readframes(chunk_frames)
            if not data:
                break
            await client.send_data(data)
            await asyncio.sleep(0.1)  # 実時間ペースで送る
    await asyncio.sleep(5)  # 最終結果待ち
    client.close()


async def main():
    params = RealtimeParameters()
    params.language_code = "ja"
    params.model_domain = params.MODEL_DOMAIN_GENERIC
    params.model_type = "WHISPER"  # WHISPER_MEDIUMは無効値（実機で確認）
    params.encoding = "audio/raw;rate=16000"
    # WHISPERモードでは shouldIgnoreInvalidCustomizations / finalSilenceThresholdInMs
    # は無効パラメータ（実機で確認）。Noneにして送らない
    params.should_ignore_invalid_customizations = None
    params.final_silence_threshold_in_ms = None

    config = from_file()
    url = f"wss://realtime.aiservice.{ENV['OCI_REGION']}.oci.oraclecloud.com"
    client = RealtimeSpeechClient(
        config=config, realtime_speech_parameters=params, listener=Listener(),
        service_endpoint=url, signer=None,
        compartment_id=ENV["COMPARTMENT_OCID"])
    t0 = time.time()
    await asyncio.gather(client.connect(), send_audio(client))
    print(f"\n総時間: {time.time()-t0:.1f}s / イベント数: {len(events)}")
    finals = [e for e in events if e[0] == "result" and e[1]]
    partials = [e for e in events if e[0] == "result" and not e[1]]
    print(f"partial: {len(partials)}件 / final: {len(finals)}件")
    for e in finals:
        print("final:", e[2])


if __name__ == "__main__":
    asyncio.run(main())
