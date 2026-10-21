#!/usr/bin/env python3
"""Suppress identical observation reminders until their context is reset."""

from __future__ import annotations

from typing import Any, Dict, Hashable


class AlertRepeatGuard:
    def __init__(self) -> None:
        self._last: Dict[str, Hashable] = {}

    def accept(self, code: str, signature: Any) -> bool:
        key = str(code)
        current = self._last.get(key)
        if current == signature:
            return False
        self._last[key] = signature
        return True

    def clear(self, code: str) -> None:
        self._last.pop(str(code), None)
