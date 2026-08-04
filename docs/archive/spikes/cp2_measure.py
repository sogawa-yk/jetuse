"""チェックポイント②向け定点計測（backlog #4）。

API GW経由（=ユーザー体感と同じ経路）で
1) TTFT: モデル別の最初のdelta到達時間
2) 長会話: 短期メモリ(OCI Conversations)方式 vs ステートレス全履歴再送方式の
   input_tokens推移（12ターン、temperature=0）
を計測する。実行: python spikes/cp2_measure.py <APIGW_HOST> <BEARER_TOKEN>
"""

import json
import statistics
import sys
import time

import httpx

HOST, TOKEN = sys.argv[1], sys.argv[2]
BASE = f"https://{HOST}"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

MODELS = ["gpt-oss-120b", "llama-3.3-70b", "gemini-2.5-flash", "gemini-2.5-pro"]

# 連続性のある12ターンの会話（回答は短めに誘導してばらつきを抑える）
TURNS = [
    "社内FAQボットを作りたい。構成案を3行で。",
    "検索はベクトル検索とキーワード検索どちらがいい？2行で。",
    "埋め込みモデルの選定基準を3つ、箇条書きで。",
    "チャンクサイズの目安は？1行で。",
    "FAQが月100件増える場合の更新運用を2行で。",
    "回答に出典を付ける方法を2行で。",
    "ハルシネーション対策を3つ、箇条書きで。",
    "評価指標は何を見るべき？3つ。",
    "最初に作るMVPの範囲を2行で。",
    "リリース後の改善サイクルを2行で。",
    "ところで、この会話の最初の質問は何だった？1行で。",
    "ここまでの会話を3行で要約して。",
]


def stream(body: dict) -> dict:
    """SSEを読み切り {ttft, total, usage, text} を返す"""
    t0 = time.perf_counter()
    ttft = None
    usage = None
    text = []
    with httpx.Client(timeout=120) as c, c.stream(
        "POST", f"{BASE}/api/chat/stream", headers=HEADERS, json=body
    ) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            ev = json.loads(data)
            if "delta" in ev:
                if ttft is None:
                    ttft = time.perf_counter() - t0
                text.append(ev["delta"])
            if "usage" in ev:
                usage = ev["usage"]
            if "error" in ev:
                raise RuntimeError(ev["error"])
    return {
        "ttft": ttft,
        "total": time.perf_counter() - t0,
        "usage": usage or {},
        "text": "".join(text),
    }


def measure_ttft() -> None:
    print("## TTFT（モデル別、3回計測、API GW経由）\n")
    print("| モデル | TTFT中央値 | TTFT範囲 | 全体時間中央値 |")
    print("|---|---|---|---|")
    for m in MODELS:
        ttfts, totals = [], []
        for _ in range(3):
            r = stream({
                "model": m,
                "messages": [{"role": "user", "content": "こんにちは。一言で挨拶して。"}],
                "temperature": 0,
            })
            ttfts.append(r["ttft"])
            totals.append(r["total"])
        print(
            f"| {m} | {statistics.median(ttfts):.2f}s "
            f"| {min(ttfts):.2f}–{max(ttfts):.2f}s "
            f"| {statistics.median(totals):.2f}s |"
        )


def measure_long_conversation() -> None:
    print("\n## 長会話 input_tokens 推移（gpt-oss-120b、12ターン、temperature=0）\n")
    # A: 短期メモリ(サーバー側Conversation)。UIと同じく最新発話のみが実送信される
    conv = httpx.post(
        f"{BASE}/api/conversations",
        headers=HEADERS,
        json={"model": "gpt-oss-120b", "title": "CP2計測(memory)"},
        timeout=30,
    ).json()
    cid = conv["id"]
    mem_rows = []
    history_a = []
    for q in TURNS:
        history_a.append({"role": "user", "content": q})
        r = stream({
            "model": "gpt-oss-120b",
            "messages": history_a,
            "temperature": 0,
            "conversation_id": cid,
        })
        history_a.append({"role": "assistant", "content": r["text"]})
        mem_rows.append(r["usage"])

    # B: ステートレス全履歴再送
    history_b = []
    stateless_rows = []
    for q in TURNS:
        history_b.append({"role": "user", "content": q})
        r = stream({"model": "gpt-oss-120b", "messages": history_b, "temperature": 0})
        history_b.append({"role": "assistant", "content": r["text"]})
        stateless_rows.append(r["usage"])

    print("| ターン | memory input | stateless input | memory out | stateless out |")
    print("|---|---|---|---|---|")
    for i, (a, b) in enumerate(zip(mem_rows, stateless_rows), 1):
        print(
            f"| {i} | {a.get('input_tokens', '?')} | {b.get('input_tokens', '?')} "
            f"| {a.get('output_tokens', '?')} | {b.get('output_tokens', '?')} |"
        )
    sum_a = sum(r.get("input_tokens", 0) for r in mem_rows)
    sum_b = sum(r.get("input_tokens", 0) for r in stateless_rows)
    print(f"\n累計input: memory={sum_a} / stateless={sum_b} （差 {sum_b - sum_a:+d}）")
    out_a = sum(r.get("output_tokens", 0) for r in mem_rows)
    out_b = sum(r.get("output_tokens", 0) for r in stateless_rows)
    print(f"累計output: memory={out_a} / stateless={out_b}")
    # 文脈保持の確認（turn11が最初の質問を言えているか）
    print(f"\nturn11(memory)応答: {history_a[21]['content'][:80]}")
    print(f"turn11(stateless)応答: {history_b[21]['content'][:80]}")
    # 後片付け
    httpx.delete(f"{BASE}/api/conversations/{cid}", headers=HEADERS, timeout=30)
    print("\n(計測用会話は削除済み)")


if __name__ == "__main__":
    measure_ttft()
    measure_long_conversation()
