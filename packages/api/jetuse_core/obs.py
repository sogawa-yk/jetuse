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


def _signer_args() -> dict:
    import oci

    if os.environ.get("AUTH_MODE") == "resource_principal":
        return {
            "config": {"region": get_settings().oci_region},
            "signer": oci.auth.signers.get_resource_principals_signer(),
        }
    return {"config": oci.config.from_file()}


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
        while True:
            timeout = max(0.2, _FLUSH_SECONDS - (time.monotonic() - last))
            try:
                buf.append(self._q.get(timeout=timeout))
            except queue.Empty:
                pass
            if buf and (len(buf) >= _BATCH_MAX or time.monotonic() - last >= _FLUSH_SECONDS):
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
                except Exception:  # noqa: BLE001
                    # 送信失敗はstderrへ1行だけ(無限ループ防止のためloggingは使わない)
                    import sys

                    print("oci log ship failed", file=sys.stderr)
                    self._client = None  # 次回再接続
                buf = []
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
    while True:
        timeout = max(0.2, _FLUSH_SECONDS - (time.monotonic() - last))
        try:
            buf.append(_metrics_q.get(timeout=timeout))
        except queue.Empty:
            pass
        if buf and (len(buf) >= _BATCH_MAX or time.monotonic() - last >= _FLUSH_SECONDS):
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
            except Exception:  # noqa: BLE001
                import sys

                print("oci metric ship failed", file=sys.stderr)
                client = None
            buf = []
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
