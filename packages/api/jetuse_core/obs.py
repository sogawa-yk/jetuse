"""可観測性(OPS-02): OCIマネージドサービスへの書き込み。

- ログ: OCI Loggingのカスタムログへ直接ingestion(PutLogs、バッチ・非同期)。
  LOG_OCID 未設定なら何もしない(ローカル開発はstdoutのJSON Linesのみ)
- メトリクス: OCI Monitoringのカスタム名前空間(jetuse_dev)へPostMetricData
  (audit.log_eventから呼ばれ、呼出数・トークン数を機能/モデル次元で記録)

いずれもベストエフォート(失敗してもサービスを止めない・ブロックしない)。
"""

import logging
import os
import queue
import threading
import time
from datetime import UTC, datetime

from .settings import get_settings

_internal = logging.getLogger("jetuse.obs")

_BATCH_MAX = 50
_FLUSH_SECONDS = 5.0
_MAX_BACKOFF_SECONDS = 300.0


def _is_auth_error(e: Exception) -> bool:
    return getattr(e, "status", None) in (401, 403)


class _ShipThrottle:
    """送信失敗の抑制(PORT-02): 指数バックオフ + N回に1回のstderrサマリ。

    恒常的な401/403(認可エラー)はWARNING1回だけ出し、以後リトライしない
    (failed()がTrueを返すのでworker側でスレッドを終了=detachする)。
    """

    def __init__(self, name: str):
        self.name = name
        self.fail_count = 0
        self.retry_after = 0.0

    def blocked(self) -> bool:
        return time.monotonic() < self.retry_after

    def ok(self) -> None:
        self.fail_count = 0
        self.retry_after = 0.0

    def failed(self, e: Exception) -> bool:
        self.fail_count += 1
        if _is_auth_error(e):
            _internal.warning(
                "%s: 認可エラーのため送信を停止します"
                "(AUTH_MODE設定漏れ/IAMポリシー未整備の可能性): %s", self.name, e,
            )
            return True
        if self.fail_count == 1 or self.fail_count % 10 == 0:
            import sys

            print(f"{self.name} failed x{self.fail_count} (backing off)", file=sys.stderr)
        backoff = min(_MAX_BACKOFF_SECONDS, _FLUSH_SECONDS * (2 ** min(self.fail_count, 6)))
        self.retry_after = time.monotonic() + backoff
        return False


def _retain_buffer(buf: list, cap: int) -> list:
    """バックオフ中はバッファを破棄せず保持する(直近capまで、超過分は古いものから捨てる)。

    障害発生中ほど観測データが欠落しては意味がないため、送信を一時停止していても
    キューから引いた分は失わない(全体の上限はself._q自体のmaxsizeで別途effectively bound)。
    """
    return buf[-cap:] if len(buf) > cap else buf


def _signer_args() -> dict:
    import oci

    if os.environ.get("AUTH_MODE") == "resource_principal":
        return {
            "config": {"region": get_settings().oci_region},
            "signer": oci.auth.signers.get_resource_principals_signer(),
        }
    from .genai import load_local_oci_config

    return {"config": load_local_oci_config()}


class OciLogHandler(logging.Handler):
    """OCI Loggingカスタムログへの非同期バッチ送信ハンドラ"""

    def __init__(self, log_ocid: str):
        super().__init__()
        self.log_ocid = log_ocid
        self._q: queue.Queue = queue.Queue(maxsize=2000)
        self._client = None
        self._source = os.environ.get("HOSTNAME", "jetuse-api")
        t = threading.Thread(target=self._worker, daemon=True, name="oci-log-shipper")
        t.start()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._q.put_nowait((record.levelname, self.format(record)))
        except queue.Full:
            pass  # 失っても止めない

    def _ensure_client(self):
        if self._client is None:
            import oci

            self._client = oci.loggingingestion.LoggingClient(**_signer_args())
        return self._client

    def _worker(self) -> None:
        import oci.loggingingestion.models as lm

        buf: list = []
        last = time.monotonic()
        throttle = _ShipThrottle("oci log ship")
        while True:
            timeout = max(0.2, _FLUSH_SECONDS - (time.monotonic() - last))
            try:
                buf.append(self._q.get(timeout=timeout))
            except queue.Empty:
                pass
            if buf and (len(buf) >= _BATCH_MAX or time.monotonic() - last >= _FLUSH_SECONDS):
                if throttle.blocked():
                    buf = _retain_buffer(buf, _BATCH_MAX)
                    last = time.monotonic()
                    continue
                try:
                    now = datetime.now(UTC)
                    entries = [
                        lm.LogEntry(data=data, id=f"{time.time_ns()}-{i}", time=now)
                        for i, (_lvl, data) in enumerate(buf)
                    ]
                    self._ensure_client().put_logs(
                        log_id=self.log_ocid,
                        put_logs_details=lm.PutLogsDetails(
                            specversion="1.0",
                            log_entry_batches=[lm.LogEntryBatch(
                                entries=entries,
                                source=self._source,
                                type="jetuse.app",
                                defaultlogentrytime=now,
                            )],
                        ),
                    )
                    throttle.ok()
                    buf = []
                except Exception as e:  # noqa: BLE001
                    # 失敗した当該バッチも破棄しない(レビュー指摘: バックオフに入る直前の
                    # 1回目の失敗バッチだけ従来 buf=[] で毎回捨てていた)。
                    self._client = None  # 次回再接続
                    if throttle.failed(e):
                        return  # 認可エラー: 以後リトライせずdetach
                    buf = _retain_buffer(buf, _BATCH_MAX)
                last = time.monotonic()


_metrics_q: queue.Queue = queue.Queue(maxsize=2000)
_metrics_thread_started = False
_metrics_lock = threading.Lock()


def post_metric(name: str, value: float, dimensions: dict[str, str]) -> None:
    """カスタムメトリクス送信(非同期バッチ)。失敗しても呼び出し元に影響しない"""
    global _metrics_thread_started
    if not _metrics_thread_started:
        with _metrics_lock:
            if not _metrics_thread_started:
                threading.Thread(
                    target=_metrics_worker, daemon=True, name="oci-metrics-shipper"
                ).start()
                _metrics_thread_started = True
    try:
        _metrics_q.put_nowait((name, value, dimensions, datetime.now(UTC)))
    except queue.Full:
        pass


def _metrics_worker() -> None:
    import oci
    import oci.monitoring.models as mm

    s = get_settings()
    client = None
    buf: list = []
    last = time.monotonic()
    throttle = _ShipThrottle("oci metric ship")
    while True:
        timeout = max(0.2, _FLUSH_SECONDS - (time.monotonic() - last))
        try:
            buf.append(_metrics_q.get(timeout=timeout))
        except queue.Empty:
            pass
        if buf and (len(buf) >= _BATCH_MAX or time.monotonic() - last >= _FLUSH_SECONDS):
            if throttle.blocked():
                buf = _retain_buffer(buf, _BATCH_MAX)
                last = time.monotonic()
                continue
            try:
                if client is None:
                    args = _signer_args()
                    client = oci.monitoring.MonitoringClient(
                        **args,
                        service_endpoint=(
                            f"https://telemetry-ingestion.{s.oci_region}.oraclecloud.com"
                        ),
                    )
                data = [
                    mm.MetricDataDetails(
                        namespace=s.metrics_namespace,
                        compartment_id=s.compartment_ocid,
                        name=name,
                        dimensions={k: (v or "-")[:255] for k, v in dims.items()},
                        datapoints=[mm.Datapoint(timestamp=ts, value=value)],
                    )
                    for name, value, dims, ts in buf
                ]
                client.post_metric_data(
                    post_metric_data_details=mm.PostMetricDataDetails(metric_data=data)
                )
                throttle.ok()
                buf = []
            except Exception as e:  # noqa: BLE001
                client = None
                if throttle.failed(e):
                    return  # 認可エラー: 以後リトライせずdetach
                buf = _retain_buffer(buf, _BATCH_MAX)
            last = time.monotonic()


def attach_oci_logging() -> bool:
    """LOG_OCIDが設定されていればルートロガーへOCI Loggingハンドラを追加"""
    log_ocid = get_settings().log_ocid
    if not log_ocid:
        return False
    from .logging import JsonFormatter

    handler = OciLogHandler(log_ocid)
    handler.setFormatter(JsonFormatter())
    logging.getLogger().addHandler(handler)
    _internal.info("oci logging attached")
    return True
