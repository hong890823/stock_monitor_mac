#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股实时数据监控脚本
功能：监控指数和个股，支持自定义股票代码，跌幅超5%红色预警 + 系统通知
"""

import urllib.request
import re
import time
from datetime import datetime
import sys
import subprocess
import platform
import html

# ANSI颜色代码
class Colors:
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    PURPLE = '\033[95m'
    CYAN = '\033[96m'
    WHITE = '\033[97m'
    BOLD = '\033[1m'
    END = '\033[0m'

# 配置
CHECK_INTERVAL = 15 * 60  # 查询间隔（秒），默认15分钟
DROP_THRESHOLD = 5.0  # 跌幅预警阈值（%）

def _escape_applescript(value):
    """转义通知文本，避免远程数据破坏 AppleScript。"""
    return (
        str(value)[:1000]
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", " ")
        .replace("\n", " ")
    )


def send_notification(title, message):
    """发送系统通知"""
    system = platform.system()
    title = str(title)[:120]
    message = str(message)[:1000]

    try:
        if system == "Darwin":  # macOS
            script = (
                f'display notification "{_escape_applescript(message)}" '
                f'with title "{_escape_applescript(title)}" '
                'sound name "Submarine"'
            )
            subprocess.run(["osascript", "-e", script], check=True)
        elif system == "Linux":
            subprocess.run(["notify-send", title, message], check=True)
        elif system == "Windows":
            safe_title = html.escape(title, quote=True)
            safe_message = html.escape(message, quote=True)
            ps_script = f'''
            [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
            [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null

            $template = @"
            <toast>
                <visual>
                    <binding template="ToastGeneric">
                        <text>{safe_title}</text>
                        <text>{safe_message}</text>
                    </binding>
                </visual>
                <audio src="ms-winsoundevent:Notification.Looping.Alarm"/>
            </toast>
"@

            $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
            $xml.LoadXml($template)
            $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
            [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("A股监控").Show($toast)
            '''
            subprocess.run(["powershell", "-Command", ps_script], check=True)
    except Exception as e:
        print(f"{Colors.YELLOW}[通知] 系统通知发送失败: {e}{Colors.END}")

# 默认监控列表
DEFAULT_WATCHLIST = [
    {"code": "sh000001", "name": "上证指数", "type": "index"},
    {"code": "sz399001", "name": "深证成指", "type": "index"},
    {"code": "sz399006", "name": "创业板指", "type": "index"},
    {"code": "sh600519", "name": "贵州茅台", "type": "stock"},
]

def get_stock_data_sina(code):
    """从新浪财经获取股票数据"""
    try:
        url = f"https://hq.sinajs.cn/list={code}"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
            'Referer': 'https://finance.sina.com.cn'
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as response:
            data = response.read().decode('gbk')

            match = re.search(r'"([^"]+)"', data)
            if match:
                parts = match.group(1).split(',')
                if len(parts) >= 32:
                    is_index = code[2:].startswith('000') or code[2:].startswith('399')

                    if is_index:
                        price = float(parts[3])
                        prev_close = float(parts[2])
                        change = price - prev_close
                        change_pct = (change / prev_close * 100) if prev_close else 0

                        return {
                            'code': code,
                            'name': parts[0],
                            'price': price,
                            'prev_close': prev_close,
                            'change': change,
                            'change_pct': change_pct,
                            'volume': parts[6],
                            'amount': parts[7],
                            'time': parts[31] if len(parts) > 31 else '',
                            'type': 'index'
                        }
                    else:
                        price = float(parts[3])
                        prev_close = float(parts[2])
                        change = price - prev_close
                        change_pct = (change / prev_close * 100) if prev_close else 0

                        return {
                            'code': code,
                            'name': parts[0],
                            'price': price,
                            'prev_close': prev_close,
                            'open': float(parts[1]),
                            'high': float(parts[4]),
                            'low': float(parts[5]),
                            'change': change,
                            'change_pct': change_pct,
                            'volume': float(parts[8]),
                            'amount': float(parts[9]),
                            'time': parts[31] if len(parts) > 31 else '',
                            'type': 'stock'
                        }
    except Exception as e:
        print(f"{Colors.RED}[错误] 获取 {code} 数据失败: {e}{Colors.END}")
    return None

def format_number(num, decimals=2):
    """格式化数字"""
    if abs(num) >= 100000000:
        return f"{num/100000000:.{decimals}f}亿"
    elif abs(num) >= 10000:
        return f"{num/10000:.{decimals}f}万"
    return f"{num:,.{decimals}f}"

def display_stock_data(data, previous_prices):
    """显示股票数据"""
    if not data:
        return

    code = data['code']

    # 判断涨跌颜色
    if data['change_pct'] > 0:
        color = Colors.GREEN
        symbol = "📈"
    elif data['change_pct'] < 0:
        color = Colors.RED
        symbol = "📉"
    else:
        color = Colors.WHITE
        symbol = "➡️"

    print(f"\n{Colors.BOLD}{data['name']}{Colors.END} ({code})")
    print("-" * 40)

    if data['type'] == 'index':
        print(f"   当前点数: {color}{Colors.BOLD}{format_number(data['price'])}{Colors.END}")
        print(f"   涨跌额:   {color}{data['change']:+.2f}{Colors.END}")
        print(f"   涨跌幅:   {color}{symbol} {data['change_pct']:+.2f}%{Colors.END}")
    else:
        print(f"   当前价格: {color}{Colors.BOLD}{data['price']:.2f}{Colors.END} 元")
        print(f"   昨收价:   {data['prev_close']:.2f} 元")
        print(f"   今开价:   {data['open']:.2f} 元")
        print(f"   最高价:   {Colors.GREEN}{data['high']:.2f}{Colors.END} 元")
        print(f"   最低价:   {Colors.RED}{data['low']:.2f}{Colors.END} 元")
        print(f"   涨跌额:   {color}{data['change']:+.2f}{Colors.END} 元")
        print(f"   涨跌幅:   {color}{symbol} {data['change_pct']:+.2f}%{Colors.END}")
        print(f"   成交量:   {format_number(data['volume'], 0)} 手")
        print(f"   成交额:   {format_number(data['amount'])} 元")

    # 与上次查询对比
    if code in previous_prices:
        prev_price = previous_prices[code]
        diff = data['price'] - prev_price
        diff_pct = (diff / prev_price * 100) if prev_price else 0

        print("-" * 40)
        print(f"   较上次:   ", end="")
        if diff > 0:
            print(f"{Colors.GREEN}+{diff:.2f} (+{diff_pct:.2f}%){Colors.END}")
        elif diff < 0:
            print(f"{Colors.RED}{diff:.2f} ({diff_pct:.2f}%){Colors.END}")
            # 跌幅预警
            if abs(diff_pct) >= DROP_THRESHOLD:
                print(f"\n   {Colors.RED}{Colors.BOLD}⚠️  跌幅预警！较上次下跌 {abs(diff_pct):.2f}%{Colors.END}")
                # 发送系统通知
                send_notification(
                    f"⚠️ {data['name']} 跌幅预警",
                    f"{data['name']}较上次查询下跌 {abs(diff_pct):.2f}%\n当前价格: {data['price']:.2f}"
                )
        else:
            print(f"{Colors.WHITE}持平{Colors.END}")

def main():
    """主函数"""
    print(f"{Colors.CYAN}{Colors.BOLD}")
    print("╔═══════════════════════════════════════════════════════════╗")
    print("║           A股实时数据监控系统                             ║")
    print("║           支持指数和个股 | 跌幅超5%红色预警               ║")
    print("╚═══════════════════════════════════════════════════════════╝")
    print(f"{Colors.END}")

    # 获取用户输入的监控列表
    print(f"📋 默认监控列表：")
    for item in DEFAULT_WATCHLIST:
        print(f"   • {item['name']} ({item['code']})")

    watchlist = DEFAULT_WATCHLIST.copy()

    # 检查是否是交互式环境
    if sys.stdin.isatty():
        print(f"\n{Colors.YELLOW}输入要添加的股票代码（格式：sh600519 或 sz000858）{Colors.END}")
        print(f"{Colors.YELLOW}输入 'done' 完成添加，直接回车使用默认列表{Colors.END}")

        while True:
            user_input = input("\n请输入股票代码: ").strip()
            if user_input.lower() == 'done' or user_input == '':
                break

            # 验证格式
            if re.match(r'^(sh|sz)\d{6}$', user_input):
                # 获取股票名称
                data = get_stock_data_sina(user_input)
                if data:
                    watchlist.append({
                        "code": user_input,
                        "name": data['name'],
                        "type": data['type']
                    })
                    print(f"{Colors.GREEN}✅ 已添加: {data['name']} ({user_input}){Colors.END}")
                else:
                    print(f"{Colors.RED}❌ 无法获取该股票数据，请检查代码是否正确{Colors.END}")
            else:
                print(f"{Colors.RED}❌ 格式错误！请使用格式：sh600519 或 sz000858{Colors.END}")

    print(f"\n{Colors.CYAN}📊 开始监控 {len(watchlist)} 只股票/指数{Colors.END}")
    print(f"⚙️  查询间隔: {CHECK_INTERVAL} 秒")
    print(f"⚠️  预警阈值: 跌幅 {DROP_THRESHOLD}%")
    print(f"\n按 Ctrl+C 停止监控\n")

    previous_prices = {}
    check_count = 0

    try:
        while True:
            check_count += 1
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            print(f"\n{'='*60}")
            print(f"🔍 第 {check_count} 次查询 | {now}")
            print(f"{'='*60}")

            current_prices = {}
            notification_lines = []

            for item in watchlist:
                data = get_stock_data_sina(item['code'])
                if data:
                    display_stock_data(data, previous_prices)
                    current_prices[item['code']] = data['price']
                    # 收集通知内容
                    notification_lines.append(f"{data['name']}: {data['price']:.2f} ({data['change_pct']:+.2f}%)")

            previous_prices = current_prices

            # 每次查询都发送系统通知
            if notification_lines:
                notification_msg = "\n".join(notification_lines)
                send_notification("📊 A股数据更新", notification_msg)

            # 等待下次查询
            print(f"\n⏳ 等待 {CHECK_INTERVAL} 秒后进行下次查询...")
            print(f"   (按 Ctrl+C 可随时停止监控)")

            for i in range(CHECK_INTERVAL):
                time.sleep(1)
                if (i + 1) % 10 == 0:
                    remaining = (CHECK_INTERVAL - i - 1)
                    sys.stdout.write(f"\r   剩余: {remaining} 秒...")
                    sys.stdout.flush()

            print()

    except KeyboardInterrupt:
        print(f"\n\n{Colors.YELLOW}⏹️  监控已停止{Colors.END}")
        print(f"📊 共完成 {check_count} 次查询")
        print(f"👋 再见！")
        sys.exit(0)

if __name__ == "__main__":
    main()
