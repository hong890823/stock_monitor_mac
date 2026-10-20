#!/bin/bash

# A股监控启动脚本 (Mac/Linux)

echo "╔═══════════════════════════════════════════════════════════╗"
echo "║           A股实时数据监控系统                             ║"
echo "╚═══════════════════════════════════════════════════════════╝"
echo ""

# 检查Python是否安装
if ! command -v python3 &> /dev/null; then
    echo "❌ 错误: 未找到Python3，请先安装Python3"
    exit 1
fi

# 获取脚本所在目录
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

echo "🚀 启动A股监控..."
echo ""
python3 "$SCRIPT_DIR/astock_monitor.py"
