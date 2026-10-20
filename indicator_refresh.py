import json
import os
import tempfile
import threading
from datetime import datetime, timedelta


class IndicatorRefreshScheduler:
    MAX_ATTEMPTS = 8
    RETRY_DELAY = timedelta(minutes=90)

    def __init__(self, state_file):
        self.state_file = os.fspath(state_file)
        self._lock = threading.RLock()
        self.state = self._load_state()

    @staticmethod
    def window_date_for(now):
        if now.hour >= 18:
            return now.date().isoformat()
        if now.hour < 6:
            return (now.date() - timedelta(days=1)).isoformat()
        return None

    def can_attempt(self, now):
        with self._lock:
            window_date = self.window_date_for(now)
            if window_date is None:
                return False
            self._sync_window(window_date)
            if self.state["success_at"] or self.state["attempt_count"] >= self.MAX_ATTEMPTS:
                return False
            next_attempt_at = self._parse_timestamp(self.state["next_attempt_at"])
            return next_attempt_at is None or now >= next_attempt_at

    def begin_attempt(self, now):
        with self._lock:
            if not self.can_attempt(now):
                raise ValueError("当前时间不允许发起指标策略刷新")
            self.state["attempt_count"] += 1
            self.state["last_attempt_at"] = self._format_timestamp(now)
            self.state["next_attempt_at"] = None
            self.state["last_error"] = None
            self._save_state()

    def record_success(self, now):
        with self._lock:
            self.state["success_at"] = self._format_timestamp(now)
            self.state["next_attempt_at"] = None
            self.state["last_error"] = None
            self._save_state()

    def record_failure(self, now, error):
        with self._lock:
            self.state["last_error"] = str(error).strip()[:160] or "未知错误"
            if self.state["attempt_count"] < self.MAX_ATTEMPTS:
                self.state["next_attempt_at"] = self._format_timestamp(
                    now + self.RETRY_DELAY
                )
            else:
                self.state["next_attempt_at"] = None
            self._save_state()

    def status(self, now):
        with self._lock:
            window_date = self.window_date_for(now)
            if window_date is None:
                return "日间暂停，等待今晚 18:00"

            self._sync_window(window_date)
            if self.state["success_at"]:
                return "本窗口已成功刷新"
            if self.state["attempt_count"] >= self.MAX_ATTEMPTS:
                return f"本窗口已尝试 {self.MAX_ATTEMPTS} 次，等待下一晚"

            next_attempt_at = self._parse_timestamp(self.state["next_attempt_at"])
            if next_attempt_at is not None and now < next_attempt_at:
                return (
                    f"上次失败，{next_attempt_at.strftime('%H:%M')} 重试"
                    f"（{self.state['attempt_count']}/{self.MAX_ATTEMPTS}）"
                )
            return f"等待刷新（{self.state['attempt_count']}/{self.MAX_ATTEMPTS}）"

    def last_success_at(self):
        with self._lock:
            return self.state["success_at"]

    def last_error(self):
        with self._lock:
            return self.state["last_error"]

    @staticmethod
    def _format_timestamp(value):
        return value.strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _parse_timestamp(value):
        if not value:
            return None
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            return None

    def _sync_window(self, window_date):
        if self.state["window_date"] == window_date:
            return
        self.state = self._default_state(window_date)
        self._save_state()

    def _load_state(self):
        try:
            with open(self.state_file, "r", encoding="utf-8") as state_handle:
                loaded_state = json.load(state_handle)
        except (OSError, json.JSONDecodeError):
            return self._default_state()

        if not isinstance(loaded_state, dict):
            return self._default_state()

        state = self._default_state()
        for key in state:
            if key in loaded_state:
                state[key] = loaded_state[key]
        if not isinstance(state["attempt_count"], int) or state["attempt_count"] < 0:
            state["attempt_count"] = 0
        return state

    @staticmethod
    def _default_state(window_date=None):
        return {
            "window_date": window_date,
            "attempt_count": 0,
            "success_at": None,
            "last_attempt_at": None,
            "next_attempt_at": None,
            "last_error": None,
        }

    def _save_state(self):
        state_dir = os.path.dirname(self.state_file) or "."
        os.makedirs(state_dir, exist_ok=True)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=state_dir,
                delete=False,
            ) as state_handle:
                temp_path = state_handle.name
                json.dump(self.state, state_handle, ensure_ascii=False, indent=2)
                state_handle.flush()
                os.fsync(state_handle.fileno())
            os.replace(temp_path, self.state_file)
        finally:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)
