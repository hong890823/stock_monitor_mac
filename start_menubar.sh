#!/bin/bash

# 菜单栏股票监控启动脚本

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

echo "🚀 启动菜单栏股票监控..."
"$SCRIPT_DIR/stock" start
