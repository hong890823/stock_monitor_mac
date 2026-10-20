#!/bin/bash
# 双击即可启动股票监控（带自动重启）

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# 使用stock脚本启动
"$SCRIPT_DIR/stock" start

sleep 2

echo ""
echo "✅ 股票监控已启动！"
echo ""
echo "📍 请查看屏幕顶部菜单栏"
echo ""
echo "💡 程序会自动重启，不用担心崩溃"
echo ""
echo "按任意键关闭此窗口..."
read -n 1
