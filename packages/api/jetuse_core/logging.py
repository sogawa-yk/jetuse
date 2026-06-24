"""構造化ログ(JSON Lines)。CI(stdout)/Functions(stderr)双方でOCI Loggingに乗る前提。"""

import json
import logging
import time


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        extra = getattr(record, "extra_fields", None)
        if extra:
            entry.update(extra)
        return json.dumps(entry, ensure_ascii=False)


def configure(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    # OPS-02: LOG_OCID設定時はOCI Loggingへも送る(マネージドサービス集約)
    try:
        from .obs import attach_oci_logging

        attach_oci_logging()
    except Exception:  # noqa: BLE001
        logging.getLogger("jetuse.obs").exception("oci logging attach failed (stdout only)")


def log_with(logger: logging.Logger, level: int, message: str, **fields) -> None:
    logger.log(level, message, extra={"extra_fields": fields})
