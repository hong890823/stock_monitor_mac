#!/bin/bash

# 快速添加股票到菜单栏的 watchlist.json

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
CONFIG_FILE="$SCRIPT_DIR/watchlist.json"

echo "=========================================="
echo "📊 快速添加股票到菜单栏"
echo "=========================================="
echo ""

echo "📋 当前监控的股票:"
python3 - "$CONFIG_FILE" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        watchlist = json.load(handle)
    for item in watchlist:
        print(f"   {item.get('code', '')} - {item.get('name', '')}")
except Exception as error:
    print(f"   无法读取配置: {error}")
PY
echo ""

read -r -p "请输入股票代码 (如 sh600036): " code
read -r -p "请输入显示名称 (如 招行): " name

if [[ ! $code =~ ^(sh|sz)[0-9]{6}$ ]]; then
    echo "❌ 代码格式错误！应为 sh600036 或 sz000001"
    exit 1
fi

if [ -z "$name" ] || [ "${#name}" -gt 40 ]; then
    echo "❌ 显示名称不能为空且不能超过40个字符"
    exit 1
fi

python3 - "$CONFIG_FILE" "$code" "$name" <<'PY'
import json
import os
import sys
import tempfile

config_file, code, name = sys.argv[1:]
try:
    with open(config_file, encoding="utf-8") as handle:
        watchlist = json.load(handle)
    if not isinstance(watchlist, list):
        raise ValueError("配置文件不是列表")
except Exception as error:
    print(f"❌ 读取配置失败: {error}")
    raise SystemExit(1)

if any(item.get("code") == code for item in watchlist):
    print("⚠️  该股票已在列表中")
    raise SystemExit(1)

watchlist.append({"code": code, "name": name, "show_in_bar": True})
config_dir = os.path.dirname(config_file) or "."
fd, temp_file = tempfile.mkstemp(prefix=".watchlist.", suffix=".tmp", dir=config_dir)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(watchlist, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_file, config_file)
except Exception:
    try:
        os.unlink(temp_file)
    except OSError:
        pass
    raise
PY

if [ $? -ne 0 ]; then
    exit 1
fi

echo ""
echo "✅ 已添加: $name ($code)"
echo ""
echo "🔄 正在重启菜单栏应用..."
"$SCRIPT_DIR/stock" restart

echo ""
echo "=========================================="
echo "✅ 完成！请查看菜单栏"
echo "=========================================="
