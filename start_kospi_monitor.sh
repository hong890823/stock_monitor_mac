#!/bin/bash

# KOSPI监控启动脚本 (Mac/Linux)

echo "╔═══════════════════════════════════════════════════════════╗"
echo "║         KOSPI 韩国综合股指监控启动脚本                   ║"
echo "╚═══════════════════════════════════════════════════════════╝"
echo ""

# 检查Python是否安装
if ! command -v python3 &> /dev/null; then
    echo "❌ 错误: 未找到Python3，请先安装Python3"
    exit 1
fi

# 获取脚本所在目录
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# 选择运行模式
echo "请选择运行模式:"
echo "1) 完整版监控（带详细显示）"
echo "2) 简易版监控（后台友好）"
echo "3) 后台运行简易版"
echo ""
read -p "请输入选项 (1/2/3): " choice

case $choice in
    1)
        echo "🚀 启动完整版监控..."
        python3 "$SCRIPT_DIR/kospi_monitor.py"
        ;;
    2)
        echo "🚀 启动简易版监控..."
        python3 "$SCRIPT_DIR/kospi_alert.py"
        ;;
    3)
        echo "🚀 后台启动简易版监控..."
        nohup python3 "$SCRIPT_DIR/kospi_alert.py" > kospi_output.log 2>&1 &
        PID=$!
        echo "✅ 监控已在后台启动 (PID: $PID)"
        echo "📋 查看日志: tail -f kospi_output.log"
        echo "⏹️  停止监控: kill $PID"
        ;;
    *)
        echo "❌ 无效选项"
        exit 1
        ;;
esac
