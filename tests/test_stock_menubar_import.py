#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""菜单栏模块导入不能在命令行 Python 中触发 AppKit 进程中止。"""

import os
import subprocess
import sys
import unittest


class StockMenuBarImportTests(unittest.TestCase):
    def test_import_does_not_initialize_ns_application(self):
        env = os.environ.copy()
        env["PYTHONPATH"] = os.getcwd()
        result = subprocess.run(
            [sys.executable, "-c", "import stock_menubar; print('import ok')"],
            cwd=os.getcwd(),
            env=env,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("import ok", result.stdout)


if __name__ == "__main__":
    unittest.main()
