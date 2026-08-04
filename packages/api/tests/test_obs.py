"""obs.py の送信失敗抑制(PORT-02)。指数バックオフ・N回に1回のサマリ・
認可エラー(401/403)での恒久detachのロジックを直接検証する(実スレッド/実OCI呼び出しは張らない)。
"""

import logging
import time

from jetuse_core import obs


class _Err(Exception):
    def __init__(self, status: int):
        super().__init__(f"status={status}")
        self.status = status


def test_is_auth_error_detects_401_403_only():
    assert obs._is_auth_error(_Err(401)) is True
    assert obs._is_auth_error(_Err(403)) is True
    assert obs._is_auth_error(_Err(500)) is False
    assert obs._is_auth_error(RuntimeError("no status attr")) is False


def test_throttle_backs_off_and_blocks_after_failure():
    t = obs._ShipThrottle("test-ship")
    assert t.blocked() is False
    stop = t.failed(_Err(500))
    assert stop is False
    assert t.fail_count == 1
    assert t.blocked() is True  # 直後はバックオフ窓内


def test_throttle_backoff_grows_with_repeated_failures():
    t = obs._ShipThrottle("test-ship")
    t.failed(_Err(500))
    first_wait = t.retry_after
    t.retry_after = 0.0  # 窓が明けた体でリセットして次の失敗を即座に記録
    t.failed(_Err(500))
    second_wait = t.retry_after
    assert second_wait >= first_wait  # 連続失敗で待ち時間が伸びる(単調非減少)


def test_throttle_ok_resets_state():
    t = obs._ShipThrottle("test-ship")
    t.failed(_Err(500))
    t.ok()
    assert t.fail_count == 0
    assert t.blocked() is False


def test_throttle_auth_error_signals_detach_without_backoff_growth():
    t = obs._ShipThrottle("test-ship")
    stop = t.failed(_Err(401))
    assert stop is True  # worker側はこれを見てスレッドを終了する


def test_retain_buffer_keeps_entries_within_cap():
    # レビュー指摘: バックオフ中もバッファを全破棄しない(障害時の観測欠落防止)
    buf = list(range(10))
    assert obs._retain_buffer(buf, 20) == buf  # 上限未満はそのまま保持


def test_retain_buffer_trims_oldest_when_over_cap():
    buf = list(range(100))
    kept = obs._retain_buffer(buf, 50)
    assert len(kept) == 50
    assert kept == list(range(50, 100))  # 直近(末尾)を優先して残す


def test_log_worker_retries_first_failed_batch_instead_of_dropping_it(monkeypatch):
    """実スレッドでの回帰テスト(レビュー指摘の再発): 送信1回目が失敗しても、当該バッチは
    捨てずにバックオフ明け後の再送に含める(以前は throttle.failed() 直後の無条件
    buf=[] で1回目の失敗バッチだけ確実に失われていた)。"""
    monkeypatch.setattr(obs, "_FLUSH_SECONDS", 0.05)
    monkeypatch.setattr(obs, "_BATCH_MAX", 50)
    calls: list = []
    state = {"failed_once": False}

    class FakeClient:
        def put_logs(self, **kw):
            calls.append(kw["put_logs_details"].log_entry_batches[0].entries)
            if not state["failed_once"]:
                state["failed_once"] = True
                raise RuntimeError("transient 500")

    handler = obs.OciLogHandler("ocid1.log.oc1..test")
    monkeypatch.setattr(handler, "_ensure_client", lambda: FakeClient())
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "hello", None, None)
    handler.emit(record)

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and len(calls) < 2:
        time.sleep(0.02)

    assert len(calls) >= 2, "1回目失敗後、バックオフ明けに再送されるはず"
    assert len(calls[0]) == 1  # 1回目(失敗)にも本来送るはずだった1件が入っている
    assert len(calls[1]) == 1  # 2回目(成功)でも同じ1件が失われず含まれる
