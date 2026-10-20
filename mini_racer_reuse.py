#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""复用 py_mini_racer 的 V8 上下文，修复长驻进程 OOM 崩溃。

akshare 的新浪接口解密（stock_zh_a_daily / stock_hk_daily / stock_us_daily 等）
每次调用都会 `py_mini_racer.MiniRacer()` 新建一个 V8 Isolate。MiniRacer 的
析构依赖引用计数 + 分代 GC，实测自动 GC 不及时回收：

    200 次 create/eval/del 后残留 50 个 Isolate（含 IsolateManager::PumpMessages
    线程），只有手动 gc.collect() 才归零。

程序每半小时扫一次指标（全市场约 5000 只 × 每只一次解密调用），55 小时
（198622 秒）后进程内累积 1258 个泄漏的 V8 线程与 Isolate，创建新 Isolate 时
触发 FatalProcessOutOfMemory → SIGSEGV（2026-10-05 崩溃日志
Python-2026-10-05-182826.ips，崩溃栈顶 MiniRacer::ContextFactory::MakeContext）。

本模块把 MiniRacer 构造替换为进程内唯一的共享上下文：
- 无论调用多少次 new，进程内始终只有 1 个 V8 Isolate；
- eval/execute/call 加锁串行（V8 Isolate 非线程安全，扫描用 4 线程并发）；
- 包装对象随用随丢，销毁/回收不影响共享上下文；close() 为空操作。

用法（在 import akshare 之前）：

    import mini_racer_reuse  # noqa: F401

install() 在 import 时自动执行；重复调用无副作用。
"""

import sys
import threading

import py_mini_racer

_OriginalMiniRacer = py_mini_racer.MiniRacer
_lock = threading.Lock()
_shared_ctx = None


def _ctx():
    """进程内唯一的底层 MiniRacer 上下文（首次使用时创建）。"""
    global _shared_ctx
    if _shared_ctx is None:
        _shared_ctx = _OriginalMiniRacer()
    return _shared_ctx


class ReusedMiniRacer:
    """接口与 py_mini_racer.MiniRacer 一致，但共用一个 V8 上下文。"""

    def __init__(self, *args, **kwargs):
        # akshare 全部是无参构造；参数仅在首次创建底层上下文时生效。
        pass

    def eval(self, *args, **kwargs):
        with _lock:
            return _ctx().eval(*args, **kwargs)

    def execute(self, *args, **kwargs):
        with _lock:
            return _ctx().execute(*args, **kwargs)

    def call(self, *args, **kwargs):
        with _lock:
            return _ctx().call(*args, **kwargs)

    def close(self):
        """共享上下文随进程存活，不随包装对象销毁。"""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def __getattr__(self, name):
        # 其余 API（内存限额、heap_stats、wrap_py_function 等）委托共享上下文。
        return getattr(_ctx(), name)


def install():
    """把 py_mini_racer.MiniRacer 换成 ReusedMiniRacer。

    akshare 两种引用方式都覆盖：
    - `import py_mini_racer; py_mini_racer.MiniRacer()`（调用时查属性）；
    - `from py_mini_racer import MiniRacer`（模块导入时绑定）——
      后者要求本补丁先于 akshare 导入执行，若顺序颠倒，
      则在 sys.modules 里把已绑定的同名属性一并替换。
    """
    if py_mini_racer.MiniRacer is ReusedMiniRacer:
        return
    py_mini_racer.MiniRacer = ReusedMiniRacer
    for module in list(sys.modules.values()):
        if getattr(module, "MiniRacer", None) is _OriginalMiniRacer:
            module.MiniRacer = ReusedMiniRacer


install()
