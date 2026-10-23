#!/usr/bin/env python3
"""A股逐笔持仓账本，用于计算A股T+1可卖数量。"""

from __future__ import annotations

import json
import os
import tempfile
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, List, Optional


@dataclass(frozen=True)
class Trade:
    trade_id: str
    side: str
    code: str
    name: str
    trade_date: str
    quantity: int
    price: float
    source: str = "manual"


@dataclass(frozen=True)
class TradeDraft:
    side: str
    code: str
    name: str
    trade_date: str
    quantity: int
    price: float


@dataclass(frozen=True)
class LedgerAdjustment:
    adjustment_id: str
    code: str
    name: str
    effective_date: str
    quantity_delta: int
    cost: float
    reason: str


@dataclass
class _Lot:
    lot_id: str
    code: str
    name: str
    acquired_date: str
    quantity: int
    remaining: int
    price: float
    source: str


class TradeLedger:
    VERSION = 1

    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.trades: List[Trade] = []
        self.adjustments: List[LedgerAdjustment] = []
        self._lots: Dict[str, List[_Lot]] = {}
        self._legacy_codes = set()
        self._reference_costs: Dict[str, float] = {}
        self._inconsistent_codes = set()
        if self.path and self.path.exists():
            self._load()

    def _load(self):
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload.get("version") != self.VERSION:
                raise ValueError("交易账本版本不支持")
            raw_trades = payload.get("trades")
            if not isinstance(raw_trades, list):
                raise ValueError("交易记录结构无效")
            trades = [self._trade_from_dict(item) for item in raw_trades]
            raw_adjustments = payload.get("adjustments", [])
            if not isinstance(raw_adjustments, list):
                raise ValueError("调整记录结构无效")
            adjustments = [self._adjustment_from_dict(item) for item in raw_adjustments]
        except (OSError, ValueError, TypeError, KeyError) as error:
            raise ValueError(f"交易账本读取失败: {error}") from error
        self.trades = []
        self.adjustments = []
        self._lots = {}
        self._legacy_codes = set(payload.get("legacy_codes", []))
        self._reference_costs = {
            str(code).lower(): float(value)
            for code, value in (payload.get("reference_costs") or {}).items()
        }
        raw_lots = payload.get("lots")
        if isinstance(raw_lots, list):
            self._lots = {}
            for item in raw_lots:
                lot = self._lot_from_dict(item)
                self._lots.setdefault(lot.code, []).append(lot)
            self.trades = trades
            self.adjustments = adjustments
        else:
            for trade in trades:
                self._append_trade(trade, save=False)
            for adjustment in adjustments:
                self._apply_adjustment(adjustment, save=False)

    @staticmethod
    def _trade_from_dict(item) -> Trade:
        return Trade(
            trade_id=str(item["trade_id"]),
            side=str(item["side"]),
            code=str(item["code"]).lower(),
            name=str(item.get("name") or item["code"]),
            trade_date=item["trade_date"],
            quantity=int(item["quantity"]),
            price=float(item["price"]),
            source=str(item.get("source") or "manual"),
        )

    @staticmethod
    def _adjustment_from_dict(item) -> LedgerAdjustment:
        return LedgerAdjustment(
            adjustment_id=str(item["adjustment_id"]),
            code=str(item["code"]).lower(),
            name=str(item.get("name") or item["code"]),
            effective_date=item["effective_date"],
            quantity_delta=int(item["quantity_delta"]),
            cost=float(item.get("cost") or 0.0),
            reason=str(item.get("reason") or "manual_adjustment"),
        )

    @staticmethod
    def _lot_from_dict(item) -> _Lot:
        return _Lot(
            lot_id=str(item["lot_id"]),
            code=str(item["code"]).lower(),
            name=str(item.get("name") or item["code"]),
            acquired_date=item["acquired_date"],
            quantity=int(item["quantity"]),
            remaining=int(item["remaining"]),
            price=float(item["price"]),
            source=str(item.get("source") or "manual"),
        )

    @staticmethod
    def _validate_trade(side, code, name, trade_date, quantity, price):
        if side not in {"buy", "sell"}:
            raise ValueError("交易方向必须是买入或卖出")
        normalized_code = str(code or "").lower().strip()
        if not normalized_code:
            raise ValueError("股票代码不能为空")
        try:
            parsed_date = date.fromisoformat(str(trade_date))
        except (TypeError, ValueError) as error:
            raise ValueError("交易日期必须是YYYY-MM-DD") from error
        if int(quantity) != quantity or quantity <= 0:
            raise ValueError("交易数量必须是正整数")
        if float(price) != float(price) or price <= 0:
            raise ValueError("成交价格必须大于0")
        return normalized_code, str(name or normalized_code).strip(), parsed_date.isoformat()

    def record_trade(self, side, code, name, trade_date, quantity, price, source="manual") -> Trade:
        normalized_code, normalized_name, normalized_date = self._validate_trade(
            side, code, name, trade_date, quantity, price
        )
        quantity = int(quantity)
        if side == "sell":
            sellable = self.sellable_quantity(normalized_code, normalized_date)
            if quantity > sellable:
                raise ValueError(f"卖出数量超过当日可卖{sellable}股")
        trade = Trade(
            trade_id=f"{normalized_date}-{normalized_code}-{len(self.trades) + 1}",
            side=side,
            code=normalized_code,
            name=normalized_name,
            trade_date=normalized_date,
            quantity=quantity,
            price=float(price),
            source=str(source or "manual"),
        )
        self._append_trade(trade, save=True)
        return trade

    def record_many(self, drafts: Iterable[TradeDraft]) -> List[Trade]:
        snapshot = self.snapshot_state()
        recorded = []
        try:
            for draft in drafts:
                code, name, trade_date = self._validate_trade(
                    draft.side, draft.code, draft.name, draft.trade_date,
                    draft.quantity, draft.price,
                )
                if draft.side == "sell":
                    sellable = self.sellable_quantity(code, trade_date)
                    if draft.quantity > sellable:
                        raise ValueError(f"卖出数量超过当日可卖{sellable}股")
                trade = Trade(
                    trade_id=f"{trade_date}-{code}-{len(self.trades) + 1}",
                    side=draft.side,
                    code=code,
                    name=name,
                    trade_date=trade_date,
                    quantity=int(draft.quantity),
                    price=float(draft.price),
                    source="manual",
                )
                self._append_trade(trade, save=False)
                recorded.append(trade)
            self._save()
            return recorded
        except Exception:
            self.restore_state(snapshot)
            if self.path and self.path.exists():
                self._load()
            raise

    def snapshot_state(self):
        return (
            list(self.trades),
            list(self.adjustments),
            deepcopy(self._lots),
            set(self._legacy_codes),
            dict(self._reference_costs),
            set(self._inconsistent_codes),
        )

    def restore_state(self, snapshot):
        (
            self.trades,
            self.adjustments,
            self._lots,
            self._legacy_codes,
            self._reference_costs,
            self._inconsistent_codes,
        ) = snapshot

    @staticmethod
    def _validate_adjustment(code, name, effective_date, quantity_delta, cost):
        normalized_code = str(code or "").lower().strip()
        if not normalized_code:
            raise ValueError("股票代码不能为空")
        try:
            parsed_date = date.fromisoformat(str(effective_date))
        except (TypeError, ValueError) as error:
            raise ValueError("生效日期必须是YYYY-MM-DD") from error
        if int(quantity_delta) != quantity_delta:
            raise ValueError("调整数量必须是整数")
        if float(cost) != float(cost) or cost < 0:
            raise ValueError("参考成本不能为负数")
        return (
            normalized_code,
            str(name or normalized_code).strip(),
            parsed_date.isoformat(),
            int(quantity_delta),
            float(cost),
        )

    def record_adjustment(
        self,
        code,
        name,
        effective_date,
        quantity_delta,
        cost,
        reason,
    ) -> LedgerAdjustment:
        normalized_code, normalized_name, normalized_date, delta, normalized_cost = (
            self._validate_adjustment(
                code, name, effective_date, quantity_delta, cost,
            )
        )
        if delta < 0:
            sellable = self.sellable_quantity(normalized_code, normalized_date)
            if -delta > sellable:
                raise ValueError(f"卖出数量超过当日可卖{sellable}股")
        adjustment = LedgerAdjustment(
            adjustment_id=f"adjust-{normalized_date}-{normalized_code}-{len(self.adjustments) + 1}",
            code=normalized_code,
            name=normalized_name,
            effective_date=normalized_date,
            quantity_delta=delta,
            cost=normalized_cost,
            reason=str(reason or "manual_adjustment"),
        )
        self._apply_adjustment(adjustment, save=True)
        return adjustment

    def _apply_adjustment(self, adjustment: LedgerAdjustment, save: bool):
        if adjustment.quantity_delta > 0:
            price = adjustment.cost
            if price <= 0:
                summary = self.position_summary(adjustment.code, adjustment.effective_date)
                price = summary["average_cost"] or 0.000001
            self._lots.setdefault(adjustment.code, []).append(_Lot(
                lot_id=adjustment.adjustment_id,
                code=adjustment.code,
                name=adjustment.name,
                acquired_date=adjustment.effective_date,
                quantity=adjustment.quantity_delta,
                remaining=adjustment.quantity_delta,
                price=price,
                source="adjustment",
            ))
        elif adjustment.quantity_delta < 0:
            remaining_to_remove = -adjustment.quantity_delta
            for lot in self._lots.get(adjustment.code, []):
                if lot.acquired_date >= adjustment.effective_date or lot.remaining <= 0:
                    continue
                consumed = min(lot.remaining, remaining_to_remove)
                lot.remaining -= consumed
                remaining_to_remove -= consumed
                if remaining_to_remove == 0:
                    break
            if remaining_to_remove:
                sellable = self.sellable_quantity(
                    adjustment.code, adjustment.effective_date,
                )
                raise ValueError(f"卖出数量超过当日可卖{sellable}股")
        if adjustment.cost > 0:
            self._reference_costs[adjustment.code] = adjustment.cost
        self.adjustments.append(adjustment)
        self._prune_lots()
        if save:
            self._save()

    def _append_trade(self, trade: Trade, save: bool):
        if trade.side == "buy":
            self._lots.setdefault(trade.code, []).append(_Lot(
                lot_id=trade.trade_id,
                code=trade.code,
                name=trade.name,
                acquired_date=trade.trade_date,
                quantity=trade.quantity,
                remaining=trade.quantity,
                price=trade.price,
                source=trade.source,
            ))
        else:
            remaining_to_sell = trade.quantity
            for lot in self._lots.get(trade.code, []):
                if lot.acquired_date >= trade.trade_date or lot.remaining <= 0:
                    continue
                consumed = min(lot.remaining, remaining_to_sell)
                lot.remaining -= consumed
                remaining_to_sell -= consumed
                if remaining_to_sell == 0:
                    break
            if remaining_to_sell:
                raise ValueError(f"卖出数量超过当日可卖{trade.quantity - remaining_to_sell}股")
        self.trades.append(trade)
        self._prune_lots()
        if save:
            self._save()

    def _prune_lots(self):
        for code in list(self._lots):
            self._lots[code] = [lot for lot in self._lots[code] if lot.remaining > 0]

    def sellable_quantity(self, code, as_of_date) -> int:
        normalized_code = str(code or "").lower().strip()
        normalized_date = date.fromisoformat(str(as_of_date)).isoformat()
        return sum(
            lot.remaining
            for lot in self._lots.get(normalized_code, [])
            if lot.acquired_date < normalized_date
        )

    def position_summary(self, code, as_of_date) -> dict:
        normalized_code = str(code or "").lower().strip()
        normalized_date = date.fromisoformat(str(as_of_date)).isoformat()
        lots = self._lots.get(normalized_code, [])
        quantity = sum(lot.remaining for lot in lots)
        sellable = self.sellable_quantity(normalized_code, normalized_date)
        today_locked = sum(
            lot.remaining for lot in lots if lot.acquired_date == normalized_date
        )
        total_cost = sum(lot.remaining * lot.price for lot in lots)
        average_cost = total_cost / quantity if quantity else 0.0
        if quantity and normalized_code in self._reference_costs:
            average_cost = self._reference_costs[normalized_code]
        consistent = normalized_code not in self._inconsistent_codes
        return {
            "code": normalized_code,
            "quantity": quantity,
            "sellable_quantity": sellable,
            "actionable_sellable_quantity": sellable if consistent else 0,
            "today_locked_quantity": today_locked,
            "next_sellable_quantity": quantity,
            "average_cost": average_cost,
            "consistent": consistent,
        }

    def sync_legacy_positions(self, positions: Iterable[dict]):
        changed = False
        for position in positions or []:
            code = str(position.get("code") or "").lower().strip()
            if not code:
                continue
            expected_quantity = int(position.get("quantity") or 0)
            if (
                code in self._lots
                or any(trade.code == code for trade in self.trades)
                or any(adjustment.code == code for adjustment in self.adjustments)
            ):
                self._legacy_codes.add(code)
                changed = self._reconcile_existing_position(
                    code, expected_quantity
                ) or changed
                continue
            if code in self._legacy_codes:
                self._inconsistent_codes.add(code)
                continue
            if expected_quantity <= 0:
                self._legacy_codes.add(code)
                continue
            trade = Trade(
                trade_id=f"legacy-{code}",
                side="buy",
                code=code,
                name=str(position.get("name") or code),
                trade_date="0001-01-01",
                quantity=expected_quantity,
                price=float(position.get("cost") or 0.000001),
                source="legacy",
            )
            self._append_trade(trade, save=False)
            self._legacy_codes.add(code)
            changed = True
        if changed and self.path:
            self._save()

    def _reconcile_existing_position(self, code: str, expected_quantity: int) -> bool:
        lots = self._lots.get(code, [])
        current_quantity = sum(lot.remaining for lot in lots)
        if current_quantity == expected_quantity:
            self._inconsistent_codes.discard(code)
            return False
        excess = current_quantity - expected_quantity
        if excess <= 0:
            self._inconsistent_codes.add(code)
            return False
        legacy_lots = [lot for lot in lots if lot.source == "legacy"]
        duplicate_pairs = [
            (legacy_lot, other_lot)
            for legacy_lot in legacy_lots
            for other_lot in lots
            if other_lot is not legacy_lot
            and other_lot.remaining == legacy_lot.remaining == excess
            and abs(other_lot.price - legacy_lot.price) < 1e-9
        ]
        if not duplicate_pairs:
            self._inconsistent_codes.add(code)
            return False
        legacy_lot, _ = duplicate_pairs[0]
        self._lots[code] = [lot for lot in lots if lot is not legacy_lot]
        self.trades = [
            trade for trade in self.trades
            if trade.trade_id != legacy_lot.lot_id
        ]
        self._inconsistent_codes.discard(code)
        self._prune_lots()
        return True

    def _save(self):
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": self.VERSION,
            "legacy_codes": sorted(self._legacy_codes),
            "trades": [asdict(trade) for trade in self.trades],
            "adjustments": [asdict(item) for item in self.adjustments],
            "reference_costs": self._reference_costs,
            "lots": [
                asdict(lot)
                for lots in self._lots.values()
                for lot in lots
            ],
        }
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=".trade-ledger.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_path = handle.name
                json.dump(payload, handle, ensure_ascii=False, allow_nan=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
        finally:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)


def parse_trade_text(text, resolver) -> List[TradeDraft]:
    raw_rows = str(text or "").replace("\r", "\n").replace("；", ";").replace("，", ",")
    drafts = []
    for row in raw_rows.replace("\n", ";").split(";"):
        if not row.strip():
            continue
        fields = [field.strip() for field in row.split(",")]
        if len(fields) != 5:
            raise ValueError("每笔交易必须是：方向,代码或名称,日期,数量,价格")
        side_text, keyword, trade_date, quantity_text, price_text = fields
        side_map = {
            "buy": "buy", "买入": "buy", "买": "buy",
            "sell": "sell", "卖出": "sell", "卖": "sell",
        }
        side = side_map.get(side_text.lower())
        if side is None:
            raise ValueError("交易方向必须是买入或卖出")
        resolved = resolver(keyword)
        if not resolved:
            raise ValueError(f"未找到股票: {keyword}")
        code, name = resolved
        try:
            quantity = int(quantity_text)
        except (TypeError, ValueError) as error:
            raise ValueError("交易数量必须是正整数") from error
        try:
            price = float(price_text)
        except (TypeError, ValueError) as error:
            raise ValueError("成交价格必须是数字") from error
        normalized_code, normalized_name, normalized_date = TradeLedger._validate_trade(
            side, code, name, trade_date, quantity, price
        )
        drafts.append(TradeDraft(
            side=side,
            code=normalized_code,
            name=normalized_name,
            trade_date=normalized_date,
            quantity=quantity,
            price=price,
        ))
    return drafts
