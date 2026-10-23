#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import unittest

from menu_update_coordinator import MenuUpdateCoordinator, observe_menu_tracking


class MenuUpdateCoordinatorTests(unittest.TestCase):
    def test_update_runs_immediately_when_menu_is_closed(self):
        calls = []
        coordinator = MenuUpdateCoordinator(lambda: calls.append("update"))
        self.assertTrue(coordinator.request_update())
        self.assertEqual(calls, ["update"])

    def test_nested_open_menu_defers_and_coalesces_updates_until_closed(self):
        calls = []
        coordinator = MenuUpdateCoordinator(lambda: calls.append("update"))
        coordinator.begin_tracking()
        coordinator.begin_tracking()
        self.assertFalse(coordinator.request_update())
        self.assertFalse(coordinator.request_update())
        self.assertEqual(calls, [])
        coordinator.end_tracking()
        self.assertEqual(calls, [])
        coordinator.end_tracking()
        self.assertEqual(calls, ["update"])

    def test_manual_refresh_is_not_lost_when_it_closes_menu(self):
        calls = []
        coordinator = MenuUpdateCoordinator(lambda: calls.append("update"))
        coordinator.begin_tracking()
        coordinator.request_update()
        coordinator.end_tracking()
        self.assertTrue(coordinator.request_update())
        self.assertEqual(calls, ["update", "update"])

    def test_notification_observer_defers_updates_until_tracking_ends(self):
        class Center:
            def __init__(self):
                self.callbacks = {}

            def addObserverForName_object_queue_usingBlock_(
                self, name, obj, queue, block,
            ):
                self.callbacks[name] = block
                return f"token-{name}"

        calls = []
        coordinator = MenuUpdateCoordinator(lambda: calls.append("update"))
        center = Center()
        tokens = observe_menu_tracking(coordinator, center, "begin", "end")
        self.assertEqual(tokens, ["token-begin", "token-end"])
        center.callbacks["begin"]("begin")
        center.callbacks["begin"]("nested")
        coordinator.request_update()
        center.callbacks["end"]("nested")
        center.callbacks["end"]("begin")
        self.assertEqual(calls, ["update"])


if __name__ == "__main__":
    unittest.main()
