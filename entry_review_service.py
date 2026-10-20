"""独立队列和线程承接买点复盘，不参与选股和通知决策。"""

from datetime import datetime, timedelta
import json
import os
from pathlib import Path
from queue import Empty, Full, Queue
import tempfile
import threading

from entry_review import DEFAULT_POLICY, STRATEGY_VERSION, validate_event
from entry_review_store import EntryReviewStore, encode, event_key


def atomic_text(path, text):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".entry-review-", delete=False) as handle:
            temporary = handle.name
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


class EntryReviewService:
    def __init__(self, root, fetch_history, *, autostart=True, queue_size=256, clock=datetime.now):
        self.db_path = Path(root) / "entry_review.sqlite3"
        self.report_path = Path(root) / "_entry_review.txt"
        self.config_path = Path(root) / "entry_review_config.json"
        self.fetch_history = fetch_history
        self.clock = clock
        self.enabled = True
        self.last_error = None
        self.dropped = 0
        self._queue = Queue(maxsize=queue_size)
        self._pending = None
        self._store = None
        self._dirty = True
        self._thread = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._manual = threading.Event()
        self._next_review = None
        self._retry_after = None
        self.last_review_at = None
        if self.config_path.exists():
            try:
                config = json.loads(self.config_path.read_text(encoding="utf-8"))
                if not isinstance(config, dict) or type(config.get("enabled")) is not bool:
                    raise ValueError("enabled必须为布尔值")
                self.enabled = config["enabled"]
            except Exception as error:
                self.enabled = False
                self.last_error = f"复盘配置读取失败：{error}"
        if autostart:
            self.start()

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="entry-review", daemon=True)
            self._thread.start()

    @property
    def status(self):
        state = "已开启" if self.enabled else "已暂停"
        text = f"买点复盘{state}｜待落盘{self._queue.qsize() + int(self._pending is not None)}"
        if self.last_review_at:
            text += f"｜最近复盘{self.last_review_at}"
        if self.dropped:
            text += f"｜队列未接收{self.dropped}项"
        if self.last_error:
            text += f"｜{self.last_error}"
        return text

    def _enqueue(self, item):
        if not self.enabled:
            return False
        try:
            self._queue.put_nowait(item)
            self._wake.set()
            return True
        except Full:
            self.dropped += 1
            return False

    def record(self, alert):
        if not self.enabled:
            return False
        try:
            event = dict(alert)
            event.setdefault("strategy_version", STRATEGY_VERSION)
            event.setdefault("policy", DEFAULT_POLICY)
            event = json.loads(encode(event))
            validate_event(event)
            return self._enqueue(("event", event))
        except Exception as error:
            self.last_error = f"买点事件未接收：{error}"
            return False

    def delivery(self, alert, sent):
        event = dict(alert, strategy_version=alert.get("strategy_version", STRATEGY_VERSION))
        return self._enqueue(("delivery", (event_key(event), bool(sent), alert["at"])))

    def set_enabled(self, enabled):
        atomic_text(self.config_path, encode({"enabled": bool(enabled)}) + "\n")
        self.enabled = bool(enabled)
        self._next_review = None
        self._wake.set()

    def request_review(self):
        if self.enabled:
            self._manual.set()
            self._wake.set()

    def _ensure_store(self):
        if self._store is None:
            self._store = EntryReviewStore(self.db_path)

    def _write_report(self):
        atomic_text(self.report_path, self.status + "\n\n" + self._store.report())
        self._dirty = False

    def process_pending(self):
        if not self.enabled:
            return False
        try:
            self._ensure_store()
            for unused in range(64):
                if self._pending is None:
                    try:
                        self._pending = self._queue.get_nowait()
                    except Empty:
                        break
                kind, payload = self._pending
                if kind == "event":
                    self._store.record_event(payload)
                else:
                    try:
                        self._store.record_delivery(*payload)
                    except ValueError as error:
                        self.last_error = str(error)
                self._pending = None
                self._queue.task_done()
                self._dirty = True
            if self.last_error and self.last_error.startswith("复盘落盘失败"):
                self.last_error = None
            if self._dirty:
                self._write_report()
            self._retry_after = None
            return True
        except Exception as error:
            self.last_error = f"复盘落盘失败（后台重试）：{error}"
            self._retry_after = self.clock() + timedelta(seconds=5)
            return False

    @staticmethod
    def _off_market(now):
        minute = now.hour * 60 + now.minute
        return now.weekday() < 5 and (minute < 540 or minute >= 920)

    def _history(self, code):
        data = self.fetch_history(code, data_length=240)
        return data.to_dict("records") if hasattr(data, "to_dict") else data

    def _refresh(self, now, manual=False):
        pending = [row for row in self._store.pending_events()
                   if manual or not row["attempted_at"]
                   or now >= datetime.fromisoformat(row["attempted_at"]) + timedelta(minutes=90)]
        errors = []
        if pending:
            try:
                benchmark = self._history("sh000001")
            except Exception as error:
                self.last_error = f"复盘指数日线暂缺：{error}"
                self._write_report()
                return
            cache = {}
            for row in pending[:8]:
                if self._stop.is_set() or not self.enabled or (not manual and not self._off_market(self.clock())):
                    break
                try:
                    code = row["event"]["code"]
                    if code not in cache:
                        cache[code] = self._history(code)
                    self._store.review(row["key"], cache[code], benchmark, now)
                except Exception as error:
                    errors.append(f"{row['event']['code']}：{error}")
                    self._store.record_error(row["key"], error, now)
        self.last_error = "；".join(errors[:3]) or None
        self.last_review_at = now.strftime("%m-%d %H:%M")
        remaining = self._store.pending_events()
        if remaining:
            next_due = min(datetime.fromisoformat(row["attempted_at"]) + timedelta(minutes=90)
                           if row["attempted_at"] else now for row in remaining)
            self._next_review = max(now + timedelta(minutes=1), next_due)
        self._write_report()

    def tick(self, now=None):
        now = now or self.clock()
        if not self.enabled or (self._retry_after and now < self._retry_after):
            return
        if not self.process_pending():
            return
        manual = self._manual.is_set()
        if manual or (self._off_market(now) and (self._next_review is None or now >= self._next_review)):
            self._manual.clear()
            self._next_review = now + timedelta(minutes=90)
            try:
                self._refresh(now, manual)
            except Exception as error:
                self.last_error = f"买点后台复盘失败：{error}"

    def _run(self):
        try:
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception as error:
                    self.last_error = f"复盘线程异常：{error}"
                self._wake.wait(1)
                self._wake.clear()
        finally:
            if self._store is not None:
                self._store.close()
                self._store = None

    def close(self):
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
        elif self._store is not None:
            self._store.close()
            self._store = None
