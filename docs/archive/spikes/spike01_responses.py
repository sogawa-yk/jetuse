"""SPIKE-01: OCI Responses API 基礎検証。

検証項目:
  1. ベースURL 2系統（/openai/v1, /20231130/actions/v1）の疎通
  2. モデル列挙 (GET /models)
  3. 非ストリーミング応答 + usage 取得（Responses API / Chat Completions 両方）
  4. SSEストリーミング + TTFT・総レイテンシ計測（各モデル×3回）

実行: .venv/bin/python spikes/spike01_responses.py
"""
import json
import sys
import time

from common import BASE_URL_ACTIONS, BASE_URL_OPENAI, make_client

CANDIDATE_MODELS = [
    "openai.gpt-oss-120b",
    "cohere.command-a-03-2025",
    "google.gemini-2.5-flash",
    "google.gemini-2.5-pro",
    "meta.llama-3.3-70b-instruct",
]

PROMPT = "日本の四国地方にある県を4つ、それぞれ一言の特徴付きで簡潔に挙げてください。"
STREAM_RUNS = 3


def section(title):
    print(f"\n{'=' * 70}\n## {title}\n{'=' * 70}", flush=True)


def test_base_urls():
    section("1. ベースURL疎通 (GET /models)")
    results = {}
    for name, url in [("openai/v1", BASE_URL_OPENAI), ("20231130/actions/v1", BASE_URL_ACTIONS)]:
        try:
            client = make_client(url)
            models = client.models.list()
            ids = sorted(m.id for m in models.data)
            results[name] = ids
            print(f"[OK] {name}: {len(ids)} models")
        except Exception as e:
            results[name] = None
            print(f"[NG] {name}: {type(e).__name__}: {e}")
    return results


def test_model_listing(model_ids):
    section("2. モデル列挙")
    if model_ids:
        for mid in model_ids:
            mark = " <- 候補" if mid in CANDIDATE_MODELS else ""
            print(f"  - {mid}{mark}")
        missing = [m for m in CANDIDATE_MODELS if m not in model_ids]
        if missing:
            print(f"\n[WARN] /models に出てこない候補: {missing}")


def test_responses_nonstream(client):
    section("3a. Responses API 非ストリーミング + usage")
    ok = []
    for model in CANDIDATE_MODELS:
        try:
            t0 = time.perf_counter()
            resp = client.responses.create(model=model, input=PROMPT)
            dt = time.perf_counter() - t0
            text = resp.output_text or ""
            usage = resp.usage
            print(f"[OK] {model}: {dt:.2f}s, out={len(text)}chars, "
                  f"usage(in={usage.input_tokens}, out={usage.output_tokens}, total={usage.total_tokens})")
            print(f"     先頭: {text[:80].replace(chr(10), ' ')}")
            ok.append(model)
        except Exception as e:
            msg = str(e).replace("\n", " ")[:200]
            print(f"[NG] {model}: {type(e).__name__}: {msg}")
    return ok


def test_chat_completions(client):
    section("3b. Chat Completions 互換 (参考)")
    for model in CANDIDATE_MODELS:
        try:
            t0 = time.perf_counter()
            resp = client.chat.completions.create(
                model=model, messages=[{"role": "user", "content": PROMPT}])
            dt = time.perf_counter() - t0
            u = resp.usage
            print(f"[OK] {model}: {dt:.2f}s, usage(in={u.prompt_tokens}, out={u.completion_tokens})")
        except Exception as e:
            msg = str(e).replace("\n", " ")[:200]
            print(f"[NG] {model}: {type(e).__name__}: {msg}")


def test_streaming(client, models):
    section(f"4. Responses API ストリーミング TTFT計測（{STREAM_RUNS}回/モデル）")
    table = []
    for model in models:
        ttfts, totals, events_seen = [], [], set()
        for i in range(STREAM_RUNS):
            try:
                t0 = time.perf_counter()
                ttft = None
                with client.responses.stream(model=model, input=PROMPT) as stream:
                    for event in stream:
                        events_seen.add(event.type)
                        if ttft is None and event.type == "response.output_text.delta":
                            ttft = time.perf_counter() - t0
                    final = stream.get_final_response()
                total = time.perf_counter() - t0
                ttfts.append(ttft)
                totals.append(total)
                if i == 0:
                    u = final.usage
                    print(f"  {model} run1: TTFT={ttft:.2f}s total={total:.2f}s "
                          f"usage(in={u.input_tokens}, out={u.output_tokens})")
            except Exception as e:
                msg = str(e).replace("\n", " ")[:150]
                print(f"  [NG] {model} run{i+1}: {type(e).__name__}: {msg}")
                break
        if ttfts and all(t is not None for t in ttfts):
            table.append({
                "model": model,
                "ttft_avg": sum(ttfts) / len(ttfts),
                "ttft_min": min(ttfts),
                "total_avg": sum(totals) / len(totals),
                "runs": len(ttfts),
            })
            print(f"  -> events: {sorted(events_seen)[:6]} ...")
    section("4結果. TTFT/レイテンシ表 (Markdown)")
    print("| モデル | TTFT平均(s) | TTFT最小(s) | 総時間平均(s) | 回数 |")
    print("|---|---|---|---|---|")
    for r in table:
        print(f"| {r['model']} | {r['ttft_avg']:.2f} | {r['ttft_min']:.2f} "
              f"| {r['total_avg']:.2f} | {r['runs']} |")
    return table


def test_streaming_chat(client, models):
    section(f"5. Chat Completions ストリーミング TTFT計測（{STREAM_RUNS}回/モデル）")
    table = []
    for model in models:
        ttfts, totals = [], []
        for i in range(STREAM_RUNS):
            try:
                t0 = time.perf_counter()
                ttft = None
                stream = client.chat.completions.create(
                    model=model, messages=[{"role": "user", "content": PROMPT}],
                    stream=True, stream_options={"include_usage": True})
                usage = None
                for chunk in stream:
                    if chunk.usage:
                        usage = chunk.usage
                    if ttft is None and chunk.choices and chunk.choices[0].delta.content:
                        ttft = time.perf_counter() - t0
                total = time.perf_counter() - t0
                if ttft is None:
                    raise RuntimeError("no content delta received")
                ttfts.append(ttft)
                totals.append(total)
                if i == 0:
                    u = f"(in={usage.prompt_tokens}, out={usage.completion_tokens})" if usage else "(usage欠落)"
                    print(f"  {model} run1: TTFT={ttft:.2f}s total={total:.2f}s usage{u}")
            except Exception as e:
                msg = str(e).replace("\n", " ")[:150]
                print(f"  [NG] {model} run{i+1}: {type(e).__name__}: {msg}")
                break
        if ttfts:
            table.append({"model": model, "ttft_avg": sum(ttfts) / len(ttfts),
                          "ttft_min": min(ttfts), "total_avg": sum(totals) / len(totals),
                          "runs": len(ttfts)})
    print("\n| モデル | TTFT平均(s) | TTFT最小(s) | 総時間平均(s) | 回数 |")
    print("|---|---|---|---|---|")
    for r in table:
        print(f"| {r['model']} | {r['ttft_avg']:.2f} | {r['ttft_min']:.2f} "
              f"| {r['total_avg']:.2f} | {r['runs']} |")
    return table


def main():
    url_results = test_base_urls()
    model_ids = url_results.get("openai/v1") or url_results.get("20231130/actions/v1")
    test_model_listing(model_ids)
    base = BASE_URL_OPENAI if url_results.get("openai/v1") else BASE_URL_ACTIONS
    client = make_client(base)
    ok_models = test_responses_nonstream(client)
    test_chat_completions(client)
    test_streaming(client, ok_models)
    chat_ok = ["openai.gpt-oss-120b", "google.gemini-2.5-flash",
               "google.gemini-2.5-pro", "meta.llama-3.3-70b-instruct"]
    test_streaming_chat(client, chat_ok)
    print("\n[done] SPIKE-01 完了")


if __name__ == "__main__":
    sys.exit(main())
