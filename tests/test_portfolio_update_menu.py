#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""持仓数量/成本的单股编辑入口测试。"""

import unittest
from types import SimpleNamespace
from unittest import mock

import stock_menubar
from stock_menubar import StockMenuBar


class _FakeMenuItem:
    def __init__(self, title=None, callback=None):
        self.title = title
        self.callback = callback
        self.children = []

    def add(self, item):
        self.children.append(item)

    def clear(self):
        self.children = []


class _FakeRumps:
    MenuItem = _FakeMenuItem


class _StrictFakeMenuItem(_FakeMenuItem):
    def clear(self):
        if not self.children:
            raise RuntimeError("空菜单尚未初始化，不能 clear")
        super().clear()


def position(code="sh600118", name="中国卫星", quantity=1100, cost=88.38):
    return {"code": code, "name": name, "quantity": quantity, "cost": cost}


class PortfolioUpdateMenuTests(unittest.TestCase):
    def test_lists_each_position_and_batch_entry(self):
        app = StockMenuBar.__new__(StockMenuBar)
        app.portfolio = {
            "positions": [
                position(),
                position(code="sz000001", name="平安银行", quantity=0, cost=0),
            ]
        }
        app._portfolio_update_menu_item = _FakeMenuItem("✏️ 修改数量/成本")
        app.show_portfolio_position_update = mock.Mock()
        app.show_portfolio_update = mock.Mock()

        refresh_menu = getattr(app, "_refresh_portfolio_update_menu", None)
        self.assertIsNotNone(refresh_menu)
        with mock.patch.object(stock_menubar, "rumps", _FakeRumps):
            refresh_menu()

        titles = [item.title for item in app._portfolio_update_menu_item.children]
        self.assertEqual(
            titles,
            [
                "中国卫星 (sh600118) · 1,100股 @ 88.38",
                "平安银行 (sz000001) · 0股 @ 0.00",
                "📋 批量修改",
                "🛑 移出监控（保留账本）",
            ],
        )

        app._portfolio_update_menu_item.children[0].callback(None)
        app.show_portfolio_position_update.assert_called_once_with(None, "sh600118")
        app._portfolio_update_menu_item.children[-2].callback(None)
        app.show_portfolio_update.assert_called_once()

    def test_first_refresh_can_initialize_empty_rumps_submenu(self):
        app = StockMenuBar.__new__(StockMenuBar)
        app.portfolio = {"positions": [position()]}
        app._portfolio_update_menu_item = _StrictFakeMenuItem("✏️ 修改数量/成本")
        app.show_portfolio_position_update = mock.Mock()
        app.show_portfolio_update = mock.Mock()

        with mock.patch.object(
            stock_menubar, "rumps",
            SimpleNamespace(MenuItem=_StrictFakeMenuItem),
        ):
            app._refresh_portfolio_update_menu()
            app._refresh_portfolio_update_menu()

        titles = [item.title for item in app._portfolio_update_menu_item.children]
        self.assertEqual(len(titles), 3)
        self.assertEqual(titles[0], "中国卫星 (sh600118) · 1,100股 @ 88.38")


class SinglePositionUpdateTests(unittest.TestCase):
    def _app(self):
        app = StockMenuBar.__new__(StockMenuBar)
        app.portfolio = {"positions": [position(), position(code="sz000001", name="平安银行")]}
        dialog = SimpleNamespace(
            run=lambda: SimpleNamespace(clicked=True, text="520,20.1")
        )
        app._window = mock.Mock(
            return_value=dialog
        )
        app._alert = mock.Mock()
        app._save_portfolio = mock.Mock(return_value=True)
        app._refresh_portfolio_update_menu = mock.Mock()
        app._last_portfolio_analysis_slot = "old"
        app._maybe_refresh_portfolio_analysis = mock.Mock()
        return app

    def test_updates_only_selected_position_and_refreshes(self):
        app = self._app()
        today = stock_menubar.datetime.now().date().isoformat()

        edit_position = getattr(app, "show_portfolio_position_update", None)
        self.assertIsNotNone(edit_position)
        edit_position(None, "SH600118")

        kwargs = app._window.call_args.kwargs
        self.assertEqual(kwargs["title"], "修改中国卫星")
        self.assertEqual(kwargs["default_text"], f"1100,88.38,{today}")
        self.assertEqual(app.portfolio["positions"][0]["quantity"], 520)
        self.assertEqual(app.portfolio["positions"][0]["cost"], 20.1)
        self.assertEqual(app.portfolio["positions"][1]["quantity"], 1100)
        self.assertIsNone(app._last_portfolio_analysis_slot)
        app._save_portfolio.assert_called_once_with()
        app._refresh_portfolio_update_menu.assert_called_once_with()
        app._maybe_refresh_portfolio_analysis.assert_called_once_with()

    def test_invalid_input_keeps_original_values(self):
        app = self._app()
        app._window.return_value.run = lambda: SimpleNamespace(
            clicked=True, text="很多,20.1"
        )

        edit_position = getattr(app, "show_portfolio_position_update", None)
        self.assertIsNotNone(edit_position)
        edit_position(None, "sh600118")

        self.assertEqual(app.portfolio["positions"][0]["quantity"], 1100)
        self.assertEqual(app.portfolio["positions"][0]["cost"], 88.38)
        app._save_portfolio.assert_not_called()
        app._alert.assert_called_once()

    def test_non_finite_input_keeps_original_values(self):
        app = self._app()
        app._window.return_value.run = lambda: SimpleNamespace(
            clicked=True, text="nan,20.1"
        )

        edit_position = getattr(app, "show_portfolio_position_update", None)
        self.assertIsNotNone(edit_position)
        edit_position(None, "sh600118")

        self.assertEqual(app.portfolio["positions"][0]["quantity"], 1100)
        self.assertEqual(app.portfolio["positions"][0]["cost"], 88.38)
        app._save_portfolio.assert_not_called()

    def test_default_text_preserves_full_cost_precision(self):
        app = self._app()
        today = stock_menubar.datetime.now().date().isoformat()
        app.portfolio["positions"][0]["cost"] = 18.1234
        app._window.return_value.run = lambda: SimpleNamespace(clicked=False, text="")

        edit_position = getattr(app, "show_portfolio_position_update", None)
        self.assertIsNotNone(edit_position)
        edit_position(None, "sh600118")

        self.assertEqual(
            app._window.call_args.kwargs["default_text"],
            f"1100,18.1234,{today}",
        )

    def test_default_text_round_trips_float_precision(self):
        app = self._app()
        today = stock_menubar.datetime.now().date().isoformat()
        exact_cost = 0.12345678901234566
        app.portfolio["positions"][0]["cost"] = exact_cost
        app._window.return_value.run = lambda: SimpleNamespace(clicked=False, text="")

        edit_position = getattr(app, "show_portfolio_position_update", None)
        self.assertIsNotNone(edit_position)
        edit_position(None, "sh600118")

        self.assertEqual(
            app._window.call_args.kwargs["default_text"],
            f"1100,{exact_cost!r},{today}",
        )

    def test_save_failure_rolls_back_selected_position(self):
        app = self._app()
        app._save_portfolio.return_value = False

        edit_position = getattr(app, "show_portfolio_position_update", None)
        self.assertIsNotNone(edit_position)
        edit_position(None, "sh600118")

        self.assertEqual(app.portfolio["positions"][0]["quantity"], 1100)
        self.assertEqual(app.portfolio["positions"][0]["cost"], 88.38)
        app._refresh_portfolio_update_menu.assert_not_called()
        self.assertIn("保存失败", app._alert.call_args.kwargs["message"])


class BatchPositionUpdateTests(unittest.TestCase):
    def _app(self):
        app = StockMenuBar.__new__(StockMenuBar)
        app.portfolio = {
            "positions": [
                position(),
                position(code="sz000001", name="平安银行", quantity=100, cost=12.34),
            ]
        }
        app._window = mock.Mock()
        app._alert = mock.Mock()
        app._save_portfolio = mock.Mock(return_value=True)
        app._refresh_portfolio_update_menu = mock.Mock()
        app._maybe_refresh_portfolio_analysis = mock.Mock()
        app._last_portfolio_analysis_slot = "old"
        app._resolve_portfolio_stock = mock.Mock(
            side_effect=lambda keyword: {
                "中国卫星": ("sh600118", "中国卫星"),
                "平安银行": ("sz000001", "平安银行"),
            }[keyword]
        )
        return app

    def _set_input(self, app, text):
        app._window.return_value.run = lambda: SimpleNamespace(clicked=True, text=text)

    def test_batch_preserves_omitted_cost(self):
        app = self._app()
        self._set_input(app, "中国卫星,520；平安银行,300")

        app.show_portfolio_update(None)

        self.assertEqual(app.portfolio["positions"][0]["quantity"], 520)
        self.assertEqual(app.portfolio["positions"][0]["cost"], 88.38)
        self.assertEqual(app.portfolio["positions"][1]["quantity"], 300)
        self.assertEqual(app.portfolio["positions"][1]["cost"], 12.34)
        app._save_portfolio.assert_called_once_with()
        app._refresh_portfolio_update_menu.assert_called_once_with()

    def test_later_invalid_row_leaves_all_positions_unchanged(self):
        app = self._app()
        self._set_input(app, "中国卫星,520；平安银行,-300")

        app.show_portfolio_update(None)

        self.assertEqual(app.portfolio["positions"][0]["quantity"], 1100)
        self.assertEqual(app.portfolio["positions"][0]["cost"], 88.38)
        self.assertEqual(app.portfolio["positions"][1]["quantity"], 100)
        self.assertEqual(app.portfolio["positions"][1]["cost"], 12.34)
        app._save_portfolio.assert_not_called()
        app._refresh_portfolio_update_menu.assert_not_called()

    def test_non_finite_row_leaves_all_positions_unchanged(self):
        app = self._app()
        self._set_input(app, "中国卫星,520；平安银行,nan")

        app.show_portfolio_update(None)

        self.assertEqual(app.portfolio["positions"][0]["quantity"], 1100)
        self.assertEqual(app.portfolio["positions"][1]["quantity"], 100)
        app._save_portfolio.assert_not_called()

    def test_omitted_cost_with_missing_value_is_rejected_atomically(self):
        app = self._app()
        app.portfolio["positions"][1]["cost"] = None
        self._set_input(app, "中国卫星,520；平安银行,300")

        try:
            app.show_portfolio_update(None)
        except Exception as error:
            self.fail(f"批量修改不应抛出异常：{error}")

        self.assertEqual(app.portfolio["positions"][0]["quantity"], 1100)
        self.assertEqual(app.portfolio["positions"][1]["quantity"], 100)
        app._save_portfolio.assert_not_called()

    def test_save_failure_rolls_back_all_batch_changes(self):
        app = self._app()
        app._save_portfolio.return_value = False
        self._set_input(app, "中国卫星,520,20.1；平安银行,300,10.2")

        app.show_portfolio_update(None)

        self.assertEqual(app.portfolio["positions"][0]["quantity"], 1100)
        self.assertEqual(app.portfolio["positions"][0]["cost"], 88.38)
        self.assertEqual(app.portfolio["positions"][1]["quantity"], 100)
        self.assertEqual(app.portfolio["positions"][1]["cost"], 12.34)
        app._refresh_portfolio_update_menu.assert_not_called()
        self.assertIn("保存失败", app._alert.call_args.kwargs["message"])


class PortfolioMutationRefreshTests(unittest.TestCase):
    def _dialog(self, text):
        return SimpleNamespace(run=lambda: SimpleNamespace(clicked=True, text=text))

    def _app(self, positions=None):
        app = StockMenuBar.__new__(StockMenuBar)
        app.portfolio = {"positions": positions or []}
        app._alert = mock.Mock()
        app._save_portfolio = mock.Mock(return_value=True)
        app._refresh_portfolio_update_menu = mock.Mock()
        return app

    def test_add_refreshes_update_menu(self):
        app = self._app()
        app._window = mock.Mock(return_value=self._dialog("特变电工,1000,15.6"))
        app._resolve_portfolio_stock = mock.Mock(
            return_value=("sh600089", "特变电工")
        )

        app.show_portfolio_add(None)

        self.assertEqual(app.portfolio["positions"][0]["code"], "sh600089")
        app._refresh_portfolio_update_menu.assert_called_once_with()

    def test_add_later_invalid_row_changes_nothing(self):
        app = self._app()
        app._window = mock.Mock(
            return_value=self._dialog("特变电工,1000,15.6；中天科技,-1,2")
        )
        app._resolve_portfolio_stock = mock.Mock(
            side_effect=[
                ("sh600089", "特变电工"),
                ("sh600522", "中天科技"),
            ]
        )

        app.show_portfolio_add(None)

        self.assertEqual(app.portfolio["positions"], [])
        app._save_portfolio.assert_not_called()
        app._refresh_portfolio_update_menu.assert_not_called()

    def test_delete_refreshes_update_menu(self):
        app = self._app([position()])
        app._window = mock.Mock(
            side_effect=[
                self._dialog("中国卫星"),
                self._dialog("确认"),
            ]
        )
        app._resolve_portfolio_stock = mock.Mock(
            return_value=("sh600118", "中国卫星")
        )

        app.show_portfolio_delete(None)

        self.assertEqual(app.portfolio["positions"], [])
        app._refresh_portfolio_update_menu.assert_called_once_with()

    def test_delete_save_failure_restores_position(self):
        original = position()
        app = self._app([original])
        app._save_portfolio.return_value = False
        app._window = mock.Mock(
            side_effect=[
                self._dialog("中国卫星"),
                self._dialog("确认"),
            ]
        )
        app._resolve_portfolio_stock = mock.Mock(
            return_value=("sh600118", "中国卫星")
        )

        app.show_portfolio_delete(None)

        self.assertEqual(app.portfolio["positions"], [original])
        app._refresh_portfolio_update_menu.assert_not_called()
        self.assertIn("保存失败", app._alert.call_args.kwargs["message"])

    def test_capital_save_failure_restores_old_value(self):
        app = self._app()
        app.portfolio["total_capital"] = 10000
        app._window = mock.Mock(return_value=self._dialog("20000"))
        app._save_portfolio.return_value = False

        app.show_portfolio_capital_settings(None)

        self.assertEqual(app.portfolio["total_capital"], 10000)
        self.assertIn("保存失败", app._alert.call_args.kwargs["message"])


if __name__ == "__main__":
    unittest.main()
