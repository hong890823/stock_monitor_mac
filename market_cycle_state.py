#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""市场阶段连续状态的轻量持久化。"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Optional

from market_cycle_policy import MarketCyclePhase


def _date_text(value: object) -> str:
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except (TypeError, ValueError) as error:
        raise ValueError("行情日期必须是YYYY-MM-DD") from error


@dataclass(frozen=True)
class MarketCycleState:
    phase: MarketCyclePhase
    phase_start_date: Optional[str]
    phase_age_days: Optional[int]
    divergence_age_days: Optional[int]
    down_age_days: Optional[int]
    last_advanced_date: Optional[str]
    last_evidence: tuple[str, ...] = ()
    phase_switch_evidence: tuple[str, ...] = ()


class MarketCycleStateStore:
    def __init__(self, path: str):
        self.path = path
        self._state: Optional[MarketCycleState] = None
        self._load()

    @property
    def state(self) -> Optional[MarketCycleState]:
        return self._state

    def observe(
        self,
        phase: MarketCyclePhase,
        quote_date: object,
        *,
        finalized: bool,
        evidence: Iterable[str] = (),
    ) -> MarketCycleState:
        quote_day = _date_text(quote_date)
        clean_evidence = tuple(str(item) for item in evidence if str(item).strip())
        current = self._state

        if current is None:
            if finalized:
                current = self._new_state(
                    phase,
                    quote_day,
                    age=1,
                    advanced_date=quote_day,
                    evidence=clean_evidence,
                )
            else:
                current = self._new_state(
                    phase,
                    quote_day,
                    age=0,
                    advanced_date=None,
                    evidence=clean_evidence,
                )
        elif not finalized:
            current = MarketCycleState(
                phase=current.phase,
                phase_start_date=current.phase_start_date,
                phase_age_days=current.phase_age_days,
                divergence_age_days=current.divergence_age_days,
                down_age_days=current.down_age_days,
                last_advanced_date=current.last_advanced_date,
                last_evidence=clean_evidence or current.last_evidence,
                phase_switch_evidence=current.phase_switch_evidence,
            )
        elif current.last_advanced_date is not None and quote_day <= current.last_advanced_date:
            current = MarketCycleState(
                phase=current.phase,
                phase_start_date=current.phase_start_date,
                phase_age_days=current.phase_age_days,
                divergence_age_days=current.divergence_age_days,
                down_age_days=current.down_age_days,
                last_advanced_date=current.last_advanced_date,
                last_evidence=clean_evidence or current.last_evidence,
                phase_switch_evidence=current.phase_switch_evidence,
            )
        elif current.last_advanced_date is None and quote_day == current.phase_start_date:
            current = self._new_state(
                current.phase,
                quote_day,
                age=1,
                advanced_date=quote_day,
                evidence=clean_evidence or current.last_evidence,
                switch_evidence=current.phase_switch_evidence,
            )
        elif current.phase == phase:
            age = (current.phase_age_days or 0) + 1
            current = self._new_state(
                phase,
                current.phase_start_date or quote_day,
                age=age,
                advanced_date=quote_day,
                evidence=clean_evidence or current.last_evidence,
                switch_evidence=current.phase_switch_evidence,
            )
        else:
            current = self._new_state(
                phase,
                quote_day,
                age=1,
                advanced_date=quote_day,
                evidence=clean_evidence or current.last_evidence,
            )

        self._state = current
        self._save()
        return current

    @staticmethod
    def _new_state(
        phase: MarketCyclePhase,
        start_date: Optional[str],
        *,
        age: int,
        advanced_date: Optional[str],
        evidence: Iterable[str] = (),
        switch_evidence: Optional[Iterable[str]] = None,
    ) -> MarketCycleState:
        clean_evidence = tuple(evidence)
        return MarketCycleState(
            phase=phase,
            phase_start_date=start_date,
            phase_age_days=age,
            divergence_age_days=age if phase is MarketCyclePhase.DIVERGENCE else 0,
            down_age_days=age if phase is MarketCyclePhase.DOWN else 0,
            last_advanced_date=advanced_date,
            last_evidence=clean_evidence,
            phase_switch_evidence=(
                tuple(switch_evidence)
                if switch_evidence is not None
                else clean_evidence
            ),
        )

    def _load(self) -> None:
        if self.path == ":memory:":
            return
        try:
            with open(self.path, encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return
        try:
            self._state = MarketCycleState(
                phase=MarketCyclePhase[str(raw["phase"])],
                phase_start_date=raw.get("phase_start_date"),
                phase_age_days=raw.get("phase_age_days"),
                divergence_age_days=raw.get("divergence_age_days"),
                down_age_days=raw.get("down_age_days"),
                last_advanced_date=raw.get("last_advanced_date"),
                last_evidence=tuple(raw.get("last_evidence") or ()),
                phase_switch_evidence=tuple(raw.get("phase_switch_evidence") or ()),
            )
        except (KeyError, TypeError, ValueError):
            self._state = None

    def _save(self) -> None:
        if self.path == ":memory:" or self._state is None:
            return
        state = self._state
        payload = {
            "phase": state.phase.name,
            "phase_start_date": state.phase_start_date,
            "phase_age_days": state.phase_age_days,
            "divergence_age_days": state.divergence_age_days,
            "down_age_days": state.down_age_days,
            "last_advanced_date": state.last_advanced_date,
            "last_evidence": list(state.last_evidence),
            "phase_switch_evidence": list(state.phase_switch_evidence),
        }
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=directory, delete=False
            ) as handle:
                temporary = handle.name
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.replace(temporary, self.path)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
