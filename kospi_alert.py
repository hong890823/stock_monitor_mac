#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KOSPI 简易监控版本 - 后台运行友好
输出简洁，适合日志记录
"""

import urllib.request
import re
import time
from datetime import datetime
import json
import os

# 配置
CHECK_INTERVAL = 15 * 60  # 15分钟
DROP_THRESHOLD = 5.0      # 跌幅预警阈值
LOG_FILE = "kospi_log.json"
KOSPI_URL = "https://finance.naver.com/sise/sise_index.naver?code=KOSPI"

def get_kospi_data():
    """获取KOSPI数据"""
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'}
        req = urllib.request.Request(KOSPI_URL, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as response:
            html = response.read().decode('euc-kr')

            now_match = re.search(r'id="now_value"[^>]*>([\d,]+\.\d+)</em>', html)
            if not now_match:
                return None

            now_value = float(now_match.group(1).replace(',', ''))

            change_match = re.search(
                r'id="change_value_and_rate"[^>]*>'
                r'<span>([\d,]+\.\d+)</span>\s*([+-]?[\d.]+)%',
                html
            )

            change_value = float(change_match.group(1).replace(',', '')) if change_match else 0
            change_rate = float(change_match.group(2)) if change_match else 0

            return {
                'price': now_value,
                'change': change_value,
                'change_rate': change_rate,
                'timestamp': datetime.now().isoformat()
            }

    except Exception as e:
        return None

def load_previous():
    """加载上次记录"""
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, 'r') as f:
                data = json.load(f)
                return data.get('price')
        except:
            pass
    return None

def save_current(price):
    """保存当前记录"""
    with open(LOG_FILE, 'w') as f:
        json.dump({'price': price, 'timestamp': datetime.now().isoformat()}, f)

def main():
    print("KOSPI监控启动...")

    while True:
        data = get_kospi_data()

        if data:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            previous_price = load_previous()

            # 输出当前数据
            print(f"[{now}] KOSPI: {data['price']:,.2f} | 涨跌: {data['change_rate']:+.2f}%", end="")

            # 检查跌幅
            if previous_price:
                diff_rate = ((data['price'] - previous_price) / previous_price) * 100
                if diff_rate < -DROP_THRESHOLD:
                    # 红色预警（使用ANSI转义码）
                    print(f"\n\033[91m\033[1m⚠️ 跌幅预警: 较上次下跌 {abs(diff_rate):.2f}%\033[0m", end="")
                print(f" | 变化: {diff_rate:+.2f}%", end="")

            print()  # 换行

            # 保存当前价格
            save_current(data['price'])

        # 等待下次查询
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n监控停止")
