#!/bin/bash

# 监控系统管理脚本

show_status() {
    echo "=========================================="
    echo "📊 监控系统运行状态"
    echo "=========================================="
    echo ""

    # 检查KOSPI监控
    if pgrep -f "kospi_monitor.py" > /dev/null; then
        KOSPI_PID=$(pgrep -f "kospi_monitor.py")
        echo "✅ KOSPI监控: 运行中 (PID: $KOSPI_PID)"
    else
        echo "❌ KOSPI监控: 未运行"
    fi

    # 检查A股监控
    if pgrep -f "astock_monitor.py" > /dev/null; then
        ASTOCK_PID=$(pgrep -f "astock_monitor.py")
        echo "✅ A股监控: 运行中 (PID: $ASTOCK_PID)"
    else
        echo "❌ A股监控: 未运行"
    fi

    echo ""
    echo "📈 最新数据:"
    echo ""

    # 显示KOSPI最新数据
    if [ -f "kospi_output.log" ]; then
        echo "KOSPI:"
        tail -10 kospi_output.log | grep -E "当前价格" | tail -1 | sed 's/\x1b\[[0-9;]*m//g' | sed 's/.*当前价格:/   价格:/'
    fi

    # 显示A股最新数据
    if [ -f "astock_output.log" ]; then
        echo ""
        echo "A股:"
        tail -20 astock_output.log | grep -E "(上证|深证|创业|茅台)" | tail -4 | sed 's/\x1b\[[0-9;]*m//g'
    fi

    echo ""
    echo "=========================================="
    echo "⏰ 查询间隔: 15分钟"
    echo "🔔 通知: 每次查询推送系统通知"
    echo "=========================================="
}

start_monitors() {
    echo "🚀 启动监控系统..."

    # 检查是否已在运行
    if pgrep -f "kospi_monitor.py" > /dev/null; then
        echo "⚠️  KOSPI监控已在运行"
    else
        nohup python3 kospi_monitor.py > kospi_output.log 2>&1 &
        echo "✅ KOSPI监控已启动"
    fi

    if pgrep -f "astock_monitor.py" > /dev/null; then
        echo "⚠️  A股监控已在运行"
    else
        nohup python3 astock_monitor.py > astock_output.log 2>&1 &
        echo "✅ A股监控已启动"
    fi
}

stop_monitors() {
    echo "⏹️  停止监控系统..."

    if pgrep -f "kospi_monitor.py" > /dev/null; then
        pkill -f "kospi_monitor.py"
        echo "✅ KOSPI监控已停止"
    else
        echo "ℹ️  KOSPI监控未在运行"
    fi

    if pgrep -f "astock_monitor.py" > /dev/null; then
        pkill -f "astock_monitor.py"
        echo "✅ A股监控已停止"
    else
        echo "ℹ️  A股监控未在运行"
    fi
}

show_logs() {
    echo "📋 选择要查看的日志:"
    echo "1) KOSPI日志"
    echo "2) A股日志"
    echo "3) 退出"
    read -p "请选择 (1/2/3): " choice

    case $choice in
        1)
            echo "📋 KOSPI实时日志 (按 Ctrl+C 退出):"
            tail -f kospi_output.log
            ;;
        2)
            echo "📋 A股实时日志 (按 Ctrl+C 退出):"
            tail -f astock_output.log
            ;;
        3)
            exit 0
            ;;
        *)
            echo "❌ 无效选项"
            ;;
    esac
}

# 主菜单
echo "╔═══════════════════════════════════════════════════════════╗"
echo "║           监控系统管理工具                               ║"
echo "╚═══════════════════════════════════════════════════════════╝"
echo ""
echo "请选择操作:"
echo "1) 查看状态"
echo "2) 启动监控"
echo "3) 停止监控"
echo "4) 查看日志"
echo "5) 退出"
echo ""
read -p "请输入选项 (1-5): " option

case $option in
    1)
        show_status
        ;;
    2)
        start_monitors
        ;;
    3)
        stop_monitors
        ;;
    4)
        show_logs
        ;;
    5)
        echo "👋 再见！"
        exit 0
        ;;
    *)
        echo "❌ 无效选项"
        exit 1
        ;;
esac
