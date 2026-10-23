#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import threading


class MenuUpdateCoordinator:
    def __init__(self, apply_update):
        self._apply_update = apply_update
        self._lock = threading.Lock()
        self._tracking_depth = 0
        self._pending = False

    def request_update(self):
        with self._lock:
            if self._tracking_depth > 0:
                self._pending = True
                return False
        self._apply_update()
        return True

    def begin_tracking(self):
        with self._lock:
            self._tracking_depth += 1

    def end_tracking(self):
        flush = False
        with self._lock:
            self._tracking_depth = max(0, self._tracking_depth - 1)
            if self._tracking_depth == 0 and self._pending:
                self._pending = False
                flush = True
        if flush:
            self._apply_update()


def observe_menu_tracking(
    coordinator,
    notification_center,
    begin_notification,
    end_notification,
):
    begin_token = notification_center.addObserverForName_object_queue_usingBlock_(
        begin_notification,
        None,
        None,
        lambda _notification: coordinator.begin_tracking(),
    )
    end_token = notification_center.addObserverForName_object_queue_usingBlock_(
        end_notification,
        None,
        None,
        lambda _notification: coordinator.end_tracking(),
    )
    return [begin_token, end_token]
