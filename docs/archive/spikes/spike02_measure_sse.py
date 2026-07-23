"""SPIKE-02: SSE経路計測クライアント。

各イベントの「サーバー送信時刻 vs クライアント到着時刻」の差からバッファリング有無を判定する。

実行: .venv/bin/python spikes/spike02_measure_sse.py <base_url> <path> [label]
例:   ... http://10.0.1.129:8000 "/drip?seconds=60" direct
      ... https://xxx.apigateway.../v1 "/sse/drip?seconds=60" via-apigw
"""
import json
import sys
import time

import httpx


def main(base, path, label):
    url = base.rstrip("/") + path
    print(f"## {label}: GET {url}")
    delays, count, first_skew = [], 0, None
    t_start = time.time()
    try:
        with httpx.Client(timeout=httpx.Timeout(10.0, read=400.0)) as client:
            with client.stream("GET", url) as r:
                print(f"status={r.status_code} headers={{'transfer-encoding': {r.headers.get('transfer-encoding')!r}, 'content-type': {r.headers.get('content-type')!r}}}")
                for line in r.iter_lines():
                    if not line.startswith("data: "):
                        continue
                    arrival = time.time()
                    d = json.loads(line[6:])
                    if "server_time" in d:
                        skew = arrival - d["server_time"]
                        if first_skew is None:
                            first_skew = skew  # 時計ズレ+固定経路遅延の基準
                        delays.append(skew - first_skew)
                        count += 1
                    if d.get("done"):
                        print(f"[done] total={d.get('total')}s")
    except Exception as e:
        elapsed = time.time() - t_start
        print(f"[切断/エラー] {type(e).__name__}: {str(e)[:120]} (経過 {elapsed:.1f}s, 受信 {count}件)")
    if delays:
        import statistics
        print(f"受信イベント: {count}件")
        print(f"相対遅延(秒) 平均={statistics.mean(delays):.3f} 最大={max(delays):.3f} "
              f"P95={sorted(delays)[int(len(delays)*0.95)-1]:.3f}")
        print(f"判定: {'バッファリングなし（逐次配信）' if max(delays) < 2.0 else 'バッファリングの疑い'}")
    print()


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "run")
