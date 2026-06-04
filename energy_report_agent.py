#!/usr/bin/env python3
"""
城市燃气 · 能源市场日报自动生成与推送系统
=============================================
功能：
  1. 自动采集国际油价、天然气期货、东北亚现货、国内LNG、管道气价格
  2. 生成专业HTML日报
  3. 支持邮件/企业微信/飞书多渠道推送
  4. 可通过 cron 定时每天8:00执行

使用方法：
  python3 energy_report_agent.py [--push email|wecom|feishu] [--date YYYY-MM-DD]
"""

import os
import sys
import json
import re
import urllib.request
import urllib.error
import ssl
import time
import smtplib
import argparse
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from string import Template

# ============================================================
# 配置区 - 请根据实际情况修改
# ============================================================
CONFIG = {
    # 邮件推送配置
    "email": {
        "smtp_host": os.environ.get("SMTP_HOST", "smtp.qq.com"),
        "smtp_port": int(os.environ.get("SMTP_PORT", "465")),
        "username": os.environ.get("SMTP_USER", ""),
        "password": os.environ.get("SMTP_PASS", ""),
        "from_addr": os.environ.get("SMTP_FROM", ""),
        "to_addrs": os.environ.get("SMTP_TO", "").split(",") if os.environ.get("SMTP_TO") else [],
    },
    # 企业微信机器人webhook
    "wecom_webhook": os.environ.get("WECOM_WEBHOOK", ""),
    # 飞书机器人webhook
    "feishu_webhook": os.environ.get("FEISHU_WEBHOOK", ""),
    # 报告输出路径
    "output_dir": os.environ.get("REPORT_OUTPUT_DIR", "reports"),
}

# ============================================================
# 数据采集模块
# ============================================================

def create_ssl_context():
    """创建SSL上下文"""
    ctx = ssl.create_default_context()
    return ctx

def http_get(url, headers=None, timeout=15):
    """HTTP GET请求"""
    if headers is None:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
        }
    try:
        req = urllib.request.Request(url, headers=headers)
        ctx = create_ssl_context()
        resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
        return resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"[WARN] HTTP请求失败 {url}: {e}")
        return None

def parse_stooq_csv(text):
    """解析Stooq返回的CSV格式数据，返回结构化字典"""
    import csv as _csv
    import io as _io
    if not text:
        return None
    reader = _csv.DictReader(_io.StringIO(text.strip()))
    for row in reader:
        if row.get("Close") and row["Close"] != "N/D":
            return {
                "open": float(row["Open"]) if row["Open"] != "N/D" else None,
                "high": float(row["High"]) if row["High"] != "N/D" else None,
                "low": float(row["Low"]) if row["Low"] != "N/D" else None,
                "close": float(row["Close"]) if row["Close"] != "N/D" else None,
                "volume": int(row["Volume"]) if row.get("Volume") and row["Volume"] not in ("N/D", "") else None,
            }
    return None


def fetch_from_stooq(symbol):
    """从Stooq获取单个品种的价格数据"""
    url = f"https://stooq.com/q/l/?s={symbol}&f=sd2t2ohlcv&h&e=csv"
    text = http_get(url)
    return parse_stooq_csv(text)


def fetch_from_yahoo(symbol):
    """
    从 Yahoo Finance 获取品种前日收盘价及OHLC数据
    symbol: Yahoo Finance ticker, e.g. 'JKM=F', 'TTF=F', 'BZ=F', 'CL=F', 'NG=F'
    返回: {'price': float, 'change_pct': float, 'high': float, 'low': float, 'date': str, 'source': 'yahoo'}
    """
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=5d&interval=1d"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
    }
    try:
        req = urllib.request.Request(url, headers=headers)
        ctx = ssl.create_default_context()
        # Yahoo Finance在某些环境需要忽略证书验证
        try:
            resp = urllib.request.urlopen(req, timeout=15, context=ctx)
        except ssl.SSLError:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            resp = urllib.request.urlopen(req, timeout=15, context=ctx)
        raw = resp.read().decode("utf-8", errors="ignore")
        data = json.loads(raw)
        result = data["chart"]["result"][0]
        meta = result.get("meta", {})
        quotes = result.get("indicators", {}).get("quote", [{}])[0]
        timestamps = result.get("timestamp", [])

        # 取最后一个有效日期（非None收盘价）
        closes = [q for q in quotes.get("close", []) if q is not None]
        opens = [q for q in quotes.get("open", []) if q is not None]
        highs = [q for q in quotes.get("high", []) if q is not None]
        lows = [q for q in quotes.get("low", []) if q is not None]
        valid_ts = [t for i, t in enumerate(timestamps) if i < len(quotes.get("close", [])) and quotes["close"][i] is not None]

        if not closes or not opens:
            return None

        price = closes[-1]
        prev_open = opens[-1] if len(opens) > 0 else price
        change_pct = ((price - prev_open) / prev_open * 100) if prev_open and prev_open > 0 else 0
        high = highs[-1] if highs else None
        low = lows[-1] if lows else None
        last_date = datetime.fromtimestamp(valid_ts[-1]).strftime("%Y-%m-%d") if valid_ts else None

        return {
            "price": round(price, 4),
            "change_pct": round(change_pct, 2),
            "high": round(high, 4) if high else None,
            "low": round(low, 4) if low else None,
            "date": last_date,
            "source": "yahoo",
        }
    except Exception as e:
        # print(f"[WARN] Yahoo Finance {symbol} 失败: {e}")
        return None


def fetch_from_exchangerate():
    """从ExchangeRate-API获取USD/CNY汇率（备用汇率源）"""
    try:
        url = "https://open.er-api.com/v6/latest/USD"
        text = http_get(url)
        if text:
            data = json.loads(text)
            cny = data.get("rates", {}).get("CNY")
            if cny:
                return cny
    except Exception as e:
        print(f"[WARN] ExchangeRate-API失败: {e}")
    return None


def fetch_oil_prices():
    """
    采集国际油价
    数据源优先级: Stooq(主力) -> 东方财富(备用)
    """
    data = {"brent": None, "wti": None, "brent_change": None, "wti_change": None,
            "brent_high": None, "brent_low": None, "wti_high": None, "wti_low": None,
            "source": "manual"}

    # 主力数据源: Stooq
    try:
        brent_s = fetch_from_stooq("cb.f")
        if brent_s and brent_s["close"]:
            data["brent"] = brent_s["close"]
            data["brent_high"] = brent_s["high"]
            data["brent_low"] = brent_s["low"]
            if brent_s["open"] and brent_s["open"] > 0:
                data["brent_change"] = (brent_s["close"] - brent_s["open"]) / brent_s["open"] * 100
            data["source"] = "stooq"

        wti_s = fetch_from_stooq("cl.f")
        if wti_s and wti_s["close"]:
            data["wti"] = wti_s["close"]
            data["wti_high"] = wti_s["high"]
            data["wti_low"] = wti_s["low"]
            if wti_s["open"] and wti_s["open"] > 0:
                data["wti_change"] = (wti_s["close"] - wti_s["open"]) / wti_s["open"] * 100
            if data["source"] == "manual":
                data["source"] = "stooq"
    except Exception as e:
        print(f"[WARN] Stooq原油API失败: {e}")

    # 备用数据源: 东方财富
    if data["brent"] is None:
        try:
            brent_url = "https://push2.eastmoney.com/api/qt/stock/get?secid=113.B00Y&fields=f43,f44,f45,f46,f47,f48,f169,f170"
            resp = http_get(brent_url)
            if resp:
                brent_json = json.loads(resp)
                if brent_json.get("data"):
                    d = brent_json["data"]
                    data["brent"] = d.get("f43", 0) / 100 if d.get("f43") else None
                    data["brent_change"] = d.get("f169", 0) / 100 if d.get("f169") else None
                    data["source"] = "eastmoney"
        except Exception as e:
            print(f"[WARN] 东方财富布伦特API失败: {e}")

    if data["wti"] is None:
        try:
            wti_url = "https://push2.eastmoney.com/api/qt/stock/get?secid=113.CL00Y&fields=f43,f44,f45,f46,f47,f48,f169,f170"
            resp = http_get(wti_url)
            if resp:
                wti_json = json.loads(resp)
                if wti_json.get("data"):
                    d = wti_json["data"]
                    data["wti"] = d.get("f43", 0) / 100 if d.get("f43") else None
                    data["wti_change"] = d.get("f169", 0) / 100 if d.get("f169") else None
                    if data["source"] == "manual":
                        data["source"] = "eastmoney"
        except Exception as e:
            print(f"[WARN] 东方财富WTI API失败: {e}")

    return data

def fetch_henry_hub():
    """采集Henry Hub天然气期货价格"""
    data = {"price": None, "change_pct": None, "high": None, "low": None, "source": "manual"}

    # 主力数据源: Stooq
    try:
        hh = fetch_from_stooq("ng.f")
        if hh and hh["close"]:
            data["price"] = hh["close"]
            data["high"] = hh["high"]
            data["low"] = hh["low"]
            if hh["open"] and hh["open"] > 0:
                data["change_pct"] = (hh["close"] - hh["open"]) / hh["open"] * 100
            data["source"] = "stooq"
    except Exception as e:
        print(f"[WARN] Stooq Henry Hub API失败: {e}")

    # 备用数据源: 东方财富
    if data["price"] is None:
        try:
            url = "https://push2.eastmoney.com/api/qt/stock/get?secid=113.NG00Y&fields=f43,f44,f45,f46,f47,f48,f169,f170"
            resp = http_get(url)
            if resp:
                j = json.loads(resp)
                if j.get("data"):
                    d = j["data"]
                    data["price"] = d.get("f43", 0) / 100 if d.get("f43") else None
                    data["change_pct"] = d.get("f169", 0) / 100 if d.get("f169") else None
                    data["source"] = "eastmoney"
        except Exception as e:
            print(f"[WARN] 东方财富Henry Hub API失败: {e}")

    return data


def fetch_ttf_price():
    """采集TTF天然气期货价格（欧洲基准）
    数据源优先级: Yahoo Finance(主力) -> Stooq(备用)"""
    data = {"price": None, "change_pct": None, "high": None, "low": None, "source": "manual"}

    # 主力数据源: Yahoo Finance TTF=F
    yahoo = fetch_from_yahoo("TTF=F")
    if yahoo and yahoo["price"]:
        data["price"] = yahoo["price"]
        data["change_pct"] = yahoo.get("change_pct")
        data["high"] = yahoo.get("high")
        data["low"] = yahoo.get("low")
        data["source"] = "yahoo"

    # 备用数据源: Stooq（TTF代码: tg.f）
    if data["price"] is None:
        try:
            ttf = fetch_from_stooq("tg.f")
            if ttf and ttf["close"]:
                data["price"] = ttf["close"]
                data["high"] = ttf["high"]
                data["low"] = ttf["low"]
                if ttf["open"] and ttf["open"] > 0:
                    data["change_pct"] = (ttf["close"] - ttf["open"]) / ttf["open"] * 100
                data["source"] = "stooq"
        except Exception as e:
            print(f"[WARN] Stooq TTF API失败: {e}")

    return data


def fetch_usdcny_rate():
    """采集美元/人民币汇率"""
    data = {"rate": None, "source": "manual"}

    # 主力数据源: ExchangeRate-API（JSON格式，更易解析）
    try:
        rate = fetch_from_exchangerate()
        if rate:
            data["rate"] = rate
            data["source"] = "exchangerate-api"
    except Exception as e:
        print(f"[WARN] ExchangeRate-API失败: {e}")

    # 备用数据源: Stooq（离岸汇率）
    if data["rate"] is None:
        try:
            fx = fetch_from_stooq("usdcny")
            if fx and fx["close"]:
                data["rate"] = fx["close"]
                data["source"] = "stooq"
        except Exception as e:
            print(f"[WARN] Stooq USD/CNY失败: {e}")

    return data


def fetch_mysteel_lng_terminals():
    """
    从我的钢铁网（Mysteel）抓取华东LNG接收站价格汇总表
    策略：1. 用Google搜索找最新文章URL → 2. 抓取文章内容 → 3. 解析价格
    返回: list of dicts [{name, company, province, price, change, note}, ...]
    """
    terminals = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }
    article_url = None

    # 策略1: 用 Google 搜索最新文章
    try:
        import urllib.parse as _urlparse
        search_q = _urlparse.quote("site:nenghua.mysteel.com 华东LNG接收站价格汇总表")
        google_url = f"https://www.google.com/search?q={search_q}&tbs=qdr:w&num=5"
        search_text = http_get(google_url, headers=headers, timeout=15)
        if search_text:
            found_urls = re.findall(r'(https://nenghua\.mysteel\.com/a/\d{8}/\w+\.html)', search_text)
            if found_urls:
                article_url = found_urls[0]
    except Exception:
        pass

    # 策略2: 用 Bing 搜索
    if not article_url:
        try:
            import urllib.parse as _urlparse
            search_q = _urlparse.quote("site:nenghua.mysteel.com LNG接收站价格汇总")
            bing_url = f"https://www.bing.com/search?q={search_q}&qft=interval%3d%227%22&filters=ex1%3a%22ez5_19869_19870%22"
            search_text = http_get(bing_url, headers=headers, timeout=15)
            if search_text:
                found_urls = re.findall(r'(https://nenghua\.mysteel\.com/a/\d{8}/\w+\.html)', search_text)
                if found_urls:
                    article_url = found_urls[0]
        except Exception:
            pass

    # 策略3: 基于已知hash尝试最近3天的URL（旧方案兜底）
    if not article_url:
        known_hashes = ["8C84510163471331", "29BD28945AE4E5E6"]
        hours = ["10", "08", "09", "11", "14", "15"]
        today = datetime.now()
        for days_back in range(3):
            d = today - timedelta(days=days_back)
            date_prefix = d.strftime("%y%m%d")
            for h in hours:
                for kh in known_hashes:
                    url = f"https://nenghua.mysteel.com/a/{date_prefix}{h}/{kh}.html"
                    try:
                        req = urllib.request.Request(url, headers=headers)
                        ctx = ssl.create_default_context()
                        resp = urllib.request.urlopen(req, timeout=8, context=ctx)
                        text = resp.read().decode("utf-8", errors="ignore")
                    except Exception:
                        continue
                    if text and "液化天然气" in text and ("中石油" in text or "广汇" in text):
                        article_url = url
                        break
                if article_url:
                    break
            if article_url:
                break

    if not article_url:
        return terminals

    # 抓取文章内容
    article_text = http_get(article_url, headers=headers, timeout=15)
    if not article_text:
        return terminals
    records = re.findall(
        r'液化天然气(江苏省|浙江省|上海市|山东省|福建省|广东省|广西壮族自治区|广西|海南省|河北省|天津市|辽宁省)'
        r'([\u4e00-\u9fa5（）()A-Za-z0-9]+?)(\d+)\s*元/吨',
        article_text
    )

    seen = set()
    for province, raw_name, price_str in records:
        name = raw_name.strip()
        price_num = int(price_str)
        
        # 价格解析：如 63500 → 6350元/吨 (涨跌0), 6400150 → 6400 (涨跌150)
        if price_num < 100000:
            price_val = price_num // 10
            change_val = price_num % 10
        else:
            price_val = price_num // 100
            change_val = price_num % 100

        if price_val < 4000 or price_val > 9000:
            continue

        # 标准化名称和企业
        name_clean = raw_name
        company = ""
        if "广汇" in raw_name or "启东" in raw_name:
            company, name_clean = "广汇", "启东"
        elif "如东" in raw_name:
            company, name_clean = "中石油", "如东"
        elif "中海油" in raw_name and "滨海" in raw_name:
            company, name_clean = "中海油", "滨海"
        elif "中海油" in raw_name and "宁波" in raw_name:
            company, name_clean = "中海油", "宁波北仑"
            if "苏北" in raw_name:
                name_clean = "宁波北仑(苏北)"
            elif "浙江" in raw_name:
                name_clean = "宁波北仑(浙江)"
        elif "浙能" in raw_name or "温州" in raw_name:
            company, name_clean = "浙能", "温州"
        elif "杭嘉" in raw_name:
            company, name_clean = "杭嘉鑫", "嘉兴"

        if not company:
            continue

        key = f"{province}|{name_clean}"
        if key in seen:
            continue
        seen.add(key)

        region = "华东"
        if province in ("河北省", "天津市", "辽宁省", "山东省"):
            region = "华北/东北"
        elif province in ("广东省", "广西", "广西壮族自治区", "海南省", "福建省"):
            region = "华南"

        terminals.append({
            "name": name_clean,
            "company": company,
            "province": province.replace("壮族自治区", ""),
            "region": region,
            "price": price_val,
            "change": change_val,
            "note": "Mysteel数据",
        })

    if terminals:
        # 从URL提取文章日期
        date_match = re.search(r'/a/(\d{6})\d{2}/', article_url)
        art_date = date_match.group(1) if date_match else ""
        art_date_str = f"20{art_date[:2]}-{art_date[2:4]}-{art_date[4:6]}" if art_date else ""
        print(f"  Mysteel华东LNG: {art_date_str} 抓取到{len(terminals)}个接收站报价")

    return terminals


def fetch_from_100ppi():
    """
    从生意社(trq.100ppi.com)抓取LNG基准价、参考价和液厂报价
    数据每日更新，免费可抓取
    返回: {
        'benchmark': float,        # 基准价（元/吨）
        'reference': float,        # 参考价（元/吨）
        'ref_change_pct': float,   # 参考价涨跌幅%
        'plant_quotes': [{name, province, price, date}, ...],
        'source': '100ppi'
    }
    """
    result = {
        "benchmark": None, "reference": None, "ref_change_pct": None,
        "plant_quotes": [], "source": "100ppi",
    }

    # 1. 抓取主页面获取基准价和参考价
    try:
        main_text = http_get("https://trq.100ppi.com/", timeout=15)
        if main_text:
            # 提取基准价: "6月4日生意社液化天然气基准价为5964.00元/吨"
            bm_match = re.findall(r'(\d{1,2})月(\d{1,2})日生意社液化天然气基准价为([\d.]+)元/吨', main_text)
            if bm_match:
                # 取最新的（最后一条，因为列表倒序）
                latest = bm_match[0]
                result["benchmark"] = float(latest[2])

            # 提取参考价: "6月4日，液化天然气参考价为5986.00，与6月1日(5846.00)相比，上涨了2.39%"
            ref_match = re.search(
                r'(\d{1,2})月(\d{1,2})日，?液化天然气参考价为([\d.]+)，'
                r'与\d{1,2}月\d{1,2}日\(([\d.]+)\)相比，'
                r'(上涨|下降)了([\d.]+)%',
                main_text
            )
            if ref_match:
                result["reference"] = float(ref_match.group(3))
                change_pct = float(ref_match.group(6))
                if ref_match.group(5) == "下降":
                    change_pct = -change_pct
                result["ref_change_pct"] = change_pct

            print(f"  生意社LNG: 基准价={result['benchmark']}, 参考价={result['reference']}({result['ref_change_pct']:+.2f}%)")
    except Exception as e:
        print(f"[WARN] 生意社主页面抓取失败: {e}")

    # 2. 抓取报价页面获取各液厂出厂价
    try:
        price_text = http_get("https://www.100ppi.com/price/", timeout=15)
        if not price_text:
            price_text = http_get("https://trq.100ppi.com/", timeout=15)  # 备用：从主页抓
        if price_text:
            # 清理HTML标签
            clean = re.sub(r'<[^>]+>', '|||', price_text)
            clean = re.sub(r'\s+', ' ', clean)

            # 查找液厂报价区块：企业名 + 出厂价 + 价格 + 日期
            # 格式如: "星星能源|||出厂价|||5,950|||液化天然气|||内蒙古鄂尔多斯|||2026-06-04"
            # 或: "内蒙森泰|||出厂价|||6,110|||液化天然气|||内蒙古森泰天然|||2026-06-04"
            today_str = datetime.now().strftime("%Y-%m-%d")
            yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

            # 按日期+价格模式匹配
            segments = clean.split('|||')
            i = 0
            quotes = []
            while i < len(segments) - 4:
                seg = segments[i].strip()
                # 找到包含"出厂价"的段
                if '出厂价' in seg and i > 0:
                    # 前一段是企业名
                    name = segments[i - 1].strip()
                    # 后面几段是: 出厂价, 价格, 规格, 产地, 日期
                    price_seg = segments[i + 1].strip() if i + 1 < len(segments) else ""
                    date_seg = segments[i + 4].strip() if i + 4 < len(segments) else ""

                    if date_seg.startswith(today_str[:10]) or date_seg.startswith(yesterday_str[:10]):
                        price_val = re.sub(r'[,\s]', '', price_seg)
                        try:
                            price_num = float(price_val)
                            if 4000 < price_num < 10000:  # 合理范围校验
                                # 识别省份
                                prov_seg = segments[i + 3].strip() if i + 3 < len(segments) else ""
                                province = ""
                                for prov_name in ["内蒙古", "陕西", "山西", "宁夏", "四川", "新疆",
                                                  "河北", "河南", "山东", "湖北", "贵州", "重庆",
                                                  "甘肃", "青海", "黑龙江", "吉林", "辽宁"]:
                                    if prov_name in prov_seg:
                                        province = prov_name
                                        break

                                quotes.append({
                                    "name": name,
                                    "province": province or prov_seg,
                                    "price": int(price_num),
                                    "date": date_seg[:10],
                                })
                        except ValueError:
                            pass
                i += 1

            # 去重（同名企业取最新日期）
            seen = {}
            for q in quotes:
                key = q["name"]
                if key not in seen or q["date"] > seen[key]["date"]:
                    seen[key] = q
            result["plant_quotes"] = list(seen.values())

            if result["plant_quotes"]:
                print(f"  生意社液厂报价: 抓取到{len(result['plant_quotes'])}家企业最新报价")
    except Exception as e:
        print(f"[WARN] 生意社报价页面抓取失败: {e}")

    # 至少拿到了基准价或参考价才算成功
    if not result["benchmark"] and not result["reference"]:
        result["source"] = "failed"

    return result


def fetch_lng168_daily_from_web():
    """
    从搜索引擎找到LNG物联网每日市场报价文章（搜狐/百家号转载），
    抓取液厂均价、接收站均价、开工率、涨跌厂数、竞拍数据、市场评述
    返回: {
        'domestic_avg': int,       # 液厂均价 元/吨
        'terminal_avg': int,       # 接收站均价 元/吨
        'domestic_high': int,
        'domestic_low': int,
        'terminal_high': int,
        'terminal_high_name': str,
        'terminal_low': int,
        'terminal_low_name': str,
        'operating_rate': int,     # 开工率%
        'up_count': int,           # 调涨厂数
        'down_count': int,         # 降价厂数
        'auction_price': str,      # 竞拍价格
        'auction_volume': str,     # 竞拍成交量
        'market_comment': str,     # 市场评述
        'article_date': str,
        'source': 'lng168-web'
    }
    """
    result = {
        "domestic_avg": None, "terminal_avg": None,
        "domestic_high": None, "domestic_low": None,
        "terminal_high": None, "terminal_high_name": None,
        "terminal_low": None, "terminal_low_name": None,
        "operating_rate": None, "up_count": None, "down_count": None,
        "auction_price": None, "auction_volume": None,
        "market_comment": "", "article_date": "", "source": "lng168-web",
    }

    article_url = None

    # 策略1: 用Google搜索搜狐上的LNG物联网文章
    try:
        import urllib.parse as _urlparse
        today = datetime.now().strftime("%Y.%m.%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y.%m.%d")
        # 搜索当天或前一天的日期标记
        search_q = _urlparse.quote(f'"LNG物联网" "市场整体报价" {today[5:]} OR {yesterday[5:]}')
        google_url = f"https://www.google.com/search?q={search_q}&tbs=qdr:w&num=5"
        search_text = http_get(google_url, timeout=15)
        if search_text:
            # 搜狐URL
            found = re.findall(r'(https?://[^"\s]+sohu\.com/a/\d+_\d+)', search_text)
            if not found:
                found = re.findall(r'(https?://[^"\s]+baijiahao\.baidu\.com[^"\s]*)', search_text)
            if found:
                article_url = found[0]
    except Exception:
        pass

    # 策略2: 用Bing搜索
    if not article_url:
        try:
            import urllib.parse as _urlparse
            search_q = _urlparse.quote('"LNG物联网" "市场整体报价分析" 2026')
            bing_url = f"https://www.bing.com/search?q={search_q}&filters=ex1%3a%22ez1%22"
            search_text = http_get(bing_url, timeout=15)
            if search_text:
                # 优先搜狐
                found = re.findall(r'(https?://[^"\s]+sohu\.com/a/\d+_\d+)', search_text)
                if not found:
                    found = re.findall(r'(https?://[^"\s]+baijiahao\.baidu\.com[^"\s]*)', search_text)
                if found:
                    article_url = found[0]
        except Exception:
            pass

    # 策略3: 直接从lng168.com搜索
    if not article_url:
        try:
            search_url = "https://www.lng168.com/gateWay/newsList?keyword=LNG%E5%B8%82%E5%9C%BA%E6%95%B4%E4%BD%93%E6%8A%A5%E4%BB%B7"
            search_text = http_get(search_url, timeout=10)
            if search_text:
                ids = list(dict.fromkeys(re.findall(r'newsDetail\?id=(\d+)', search_text)))
                for aid in ids[:3]:
                    test_url = f"https://www.lng168.com/gateWay/newsDetail?id={aid}"
                    test_text = http_get(test_url, timeout=10)
                    if test_text and "市场均价" in test_text:
                        article_url = test_url
                        break
        except Exception:
            pass

    if not article_url:
        print("[WARN] LNG物联网日报文章未找到")
        result["source"] = "failed"
        return result

    # 抓取文章内容
    article_text = http_get(article_url, timeout=15)
    if not article_text:
        result["source"] = "failed"
        return result

    # 清理HTML
    clean = re.sub(r'<[^>]+>', ' ', article_text)
    clean = re.sub(r'\s+', ' ', clean)

    # 提取液厂均价
    avg_match = re.search(r'市场均价为(\d+)\s*元', clean)
    if avg_match:
        result["domestic_avg"] = int(avg_match.group(1))

    # 提取液厂最高/最低价
    high_match = re.search(r'较高价报价(\d+)\s*元', clean)
    if high_match:
        result["domestic_high"] = int(high_match.group(1))
    low_match = re.search(r'较低价报价(\d+)\s*元', clean)
    if low_match:
        result["domestic_low"] = int(low_match.group(1))

    # 提取开工率
    rate_match = re.search(r'开工率(\d+)%', clean)
    if rate_match:
        result["operating_rate"] = int(rate_match.group(1))

    # 提取接收站均价
    term_avg_match = re.search(r'接收站均价\s*为(\d+)\s*元', clean)
    if term_avg_match:
        result["terminal_avg"] = int(term_avg_match.group(1))

    # 提取接收站最高/最低价
    term_high_match = re.search(r'较\s*高价(?:是|为)\s*(.+?)\s*报价(\d+)\s*元', clean)
    if term_high_match:
        result["terminal_high_name"] = term_high_match.group(1).strip()
        result["terminal_high"] = int(term_high_match.group(2))
    term_low_match = re.search(r'较\s*低价(?:是|为)(.+?)\s*报价(\d+)\s*元', clean)
    if term_low_match:
        result["terminal_low_name"] = term_low_match.group(1).strip()
        result["terminal_low"] = int(term_low_match.group(2))

    # 提取涨跌厂数
    up_match = re.search(r'(\d+)家\s*调?\s*涨', clean)
    if up_match:
        result["up_count"] = int(up_match.group(1))
    down_match = re.search(r'(\d+)家\s*降\s*价', clean)
    if down_match:
        result["down_count"] = int(down_match.group(1))

    # 提取竞拍数据
    # 格式1: "原料气竞拍，成交价格为3.75-3.9元/方"
    auction_match = re.search(r'原料气竞拍[，,]\s*成交价格为([\d.]+)\s*-\s*([\d.]+)\s*元/方', clean)
    if auction_match:
        result["auction_price"] = f"{auction_match.group(1)}-{auction_match.group(2)}元/方"
    if not result["auction_price"]:
        auction_match2 = re.search(r'起拍价格?([\d.]+)\s*元/方', clean)
        if auction_match2:
            result["auction_price"] = f"{auction_match2.group(1)}元/方"
    # 成交量
    auction_vol_match = re.search(r'成交量?为?(\d+)\s*万方', clean)
    if auction_vol_match:
        result["auction_volume"] = f"{auction_vol_match.group(1)}万方"
    elif not result["auction_volume"]:
        vol_match2 = re.search(r'投放量?(\d+)\s*万方', clean)
        if vol_match2:
            result["auction_volume"] = f"{vol_match2.group(1)}万方"

    # 提取市场评述（取文章中关于行情描述的文字）
    comment_parts = []
    # 评述在数据段之后、"国际油价"之前
    comment_markers = ['无流拍', '调涨', '降价', '涨幅']
    comment_start = -1
    for marker in comment_markers:
        idx = clean.find(marker)
        if idx > 0:
            comment_start = idx
            break
    if comment_start > 0:
        after = clean[comment_start:comment_start+800]
        oil_idx = after.find('国际油价')
        if oil_idx > 0:
            comment_text = after[:oil_idx].strip().rstrip('。；')
            if len(comment_text) > 20:
                comment_parts.append(comment_text[:400])
    # 海气评述
    haiqi_match = re.search(r'海气方面[，,]\s*(.+?)(?:国际油价|$)', clean)
    if haiqi_match:
        comment_parts.append(f"海气：{haiqi_match.group(1).strip()[:200]}")

    result["market_comment"] = "；".join(comment_parts) if comment_parts else ""

    # 提取文章日期
    date_match = re.search(r'2026\.(\d{1,2})\.(\d{1,2})', clean)
    if date_match:
        result["article_date"] = f"2026-{date_match.group(1).zfill(2)}-{date_match.group(2).zfill(2)}"

    got_any = result["domestic_avg"] or result["terminal_avg"] or result["operating_rate"]
    if got_any:
        print(f"  LNG物联网日报({result['article_date']}): 液厂={result['domestic_avg']}, 接收站={result['terminal_avg']}, 开工率={result['operating_rate']}%")
    else:
        result["source"] = "failed"

    return result


def fetch_shpgx_daily():
    """
    从新浪财经抓取上海石油天然气交易中心(SHPGX)每日发布的交易数据及价格指数
    数据源：新浪财经转载的SHPGX公告
    返回: {
        'lng_factory_price': int,       # 中国LNG出厂价格指数
        'lng_terminal_price': int,      # 中国LNG出站价格指数
        'pipeline_spot_price': float,   # 管道气现货价格 元/方
        'pipeline_monthly_avg': float,  # 管道气现货月度均价 元/方
        'cnooc_terminals': [{name, region, province, price}, ...],  # 中海油基准价
        'trade_data': [{type, volume, price}, ...],                # 成交行情
        'article_date': str,
        'source': 'sina-shpgx'
    }
    """
    result = {
        "lng_factory_price": None, "lng_terminal_price": None,
        "pipeline_spot_price": None, "pipeline_monthly_avg": None,
        "cnooc_terminals": [], "trade_data": [],
        "article_date": "", "source": "sina-shpgx",
    }

    article_url = None

    # 策略1: Google搜索（限制最近一周）
    try:
        import urllib.parse as _urlparse
        search_q = _urlparse.quote('site:finance.sina.com.cn "SHPGX交易及数据指数发布"')
        google_url = f"https://www.google.com/search?q={search_q}&tbs=qdr:w&num=5"
        search_text = http_get(google_url, timeout=15)
        if search_text:
            found = re.findall(r'(https?://finance\.sina\.com\.cn/[^"\s]+doc-[^"\s]+\.shtml)', search_text)
            if found:
                article_url = found[0]
    except Exception:
        pass

    # 策略2: Bing搜索（限制最近一周）
    if not article_url:
        try:
            import urllib.parse as _urlparse
            search_q = _urlparse.quote('"SHPGX交易及数据指数发布" 2026')
            bing_url = f"https://www.bing.com/search?q={search_q}&filters=ex1%3a%22ez1%22"
            search_text = http_get(bing_url, timeout=15)
            if search_text:
                found = re.findall(r'(https?://finance\.sina\.com\.cn/[^"\s]+doc-[^"\s]+\.shtml)', search_text)
                if found:
                    # 选择URL中日期最新的（doc-iniacaet这类编码中无日期，取最后一条可能最新）
                    article_url = found[-1]
        except Exception:
            pass

    # 策略3: 直接在新浪财经搜索SHPGX文章
    if not article_url:
        try:
            import urllib.parse as _urlparse
            # 直接搜索新浪财经
            search_q = _urlparse.quote('"SHPGX交易" OR "上海石油天然气交易中心" 价格指数 site:finance.sina.com.cn 2026')
            bing_url = f"https://www.bing.com/search?q={search_q}&filters=ex1%3a%22ez1%22"
            search_text = http_get(bing_url, timeout=15)
            if search_text:
                found = re.findall(r'(https?://finance\.sina\.com\.cn/[^"\s]+doc-[^"\s]+\.shtml)', search_text)
                if found:
                    article_url = found[-1]
        except Exception:
            pass

    # 策略4: 搜狐转载的SHPGX文章
    if not article_url:
        try:
            import urllib.parse as _urlparse
            search_q = _urlparse.quote('"SHPGX交易及数据指数发布" 2026年6月')
            bing_url = f"https://www.bing.com/search?q={search_q}"
            search_text = http_get(bing_url, timeout=15)
            if search_text:
                # 新浪财经或搜狐
                found = re.findall(r'(https?://(?:finance\.sina\.com\.cn|www\.sohu\.com)/[^"\s]+(?:doc-[^"\s]+|_\d+_\d+)[^"\s]*\.shtml)', search_text)
                if not found:
                    found = re.findall(r'(https?://finance\.sina\.com\.cn/[^"\s]+doc-[^"\s]+\.shtml)', search_text)
                if not found:
                    found = re.findall(r'(https?://[^"\s]+sina[^"\s]+\.shtml)', search_text)
                if found:
                    article_url = found[0]
        except Exception:
            pass

    if not article_url:
        print("[WARN] SHPGX日报文章未找到")
        result["source"] = "failed"
        return result

    # 抓取文章
    article_text = http_get(article_url, timeout=15)
    if not article_text:
        result["source"] = "failed"
        return result

    # 清理HTML
    clean = re.sub(r'<[^>]+>', ' ', article_text)
    clean = re.sub(r'\s+', ' ', clean)

    # 提取价格指数
    factory_match = re.search(r'中国LNG出厂价格\s*\d+月\d+日\s*(\d+)\s*元/吨', clean)
    if factory_match:
        result["lng_factory_price"] = int(factory_match.group(1))

    terminal_match = re.search(r'中国LNG出站价格\s*\d+月\d+日\s*(\d+)\s*元/吨', clean)
    if terminal_match:
        result["lng_terminal_price"] = int(terminal_match.group(1))

    pipe_spot_match = re.search(r'中国管道气现货价格[^0-9]*(\d+[\d.]*)\s*元/方', clean)
    if pipe_spot_match:
        result["pipeline_spot_price"] = float(pipe_spot_match.group(1))

    pipe_avg_match = re.search(r'中国管道气现货月度均价[^0-9]*(\d+[\d.]*)\s*元/方', clean)
    if pipe_avg_match:
        result["pipeline_monthly_avg"] = float(pipe_avg_match.group(1))

    # 提取中海油基准价
    cnooc_section = re.search(r'中海油市场基准价格(.+?)(?:山西华新|数据来源|$)', clean, re.DOTALL)
    if cnooc_section:
        section_text = cnooc_section.group(1)

        # 提取各接收站-省份-价格组合
        # 格式如: "浙江宁波LNG接收站 浙江 6950"
        cnooc_matches = re.findall(
            r'([\u4e00-\u9fa5]+LNG接收站)\s+([\u4e00-\u9fa5]+[东西]?)\s+(\d{4})',
            section_text
        )
        for terminal, province, price in cnooc_matches:
            # 确定区域
            region = "华东"
            if province in ("广东", "广西", "福建", "海南"):
                region = "华南"
            elif province in ("北京", "天津", "河北", "山东", "山西", "陕西"):
                region = "华北"

            result["cnooc_terminals"].append({
                "name": terminal,
                "region": region,
                "province": province,
                "price": int(price),
            })

    # 去重（同名接收站+省份组合，取最后一条）
    seen = {}
    for t in result["cnooc_terminals"]:
        key = f"{t['name']}|{t['province']}"
        seen[key] = t
    result["cnooc_terminals"] = list(seen.values())

    # 提取成交行情
    trade_matches = re.findall(r'([\u4e00-\u9fa5A-Za-z]+(?:竞价|挂牌|交易))\s*(\d+)\s*吨', clean)
    for trade_type, volume in trade_matches:
        result["trade_data"].append({
            "type": trade_type,
            "volume": int(volume),
            "price": None,
        })

    # 提取文章日期
    date_match = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', clean)
    if date_match:
        result["article_date"] = f"{date_match.group(1)}-{date_match.group(2).zfill(2)}-{date_match.group(3).zfill(2)}"

    got_any = result["lng_factory_price"] or result["lng_terminal_price"] or result["cnooc_terminals"]
    # 校验日期是否为最近3天
    if got_any and result["article_date"]:
        try:
            art_dt = datetime.strptime(result["article_date"], "%Y-%m-%d")
            if (datetime.now() - art_dt).days > 3:
                print(f"[WARN] SHPGX文章日期{result['article_date']}已超过3天，数据可能过时")
                result["source"] = "sina-shpgx-stale"
        except ValueError:
            pass
    elif not got_any:
        result["source"] = "failed"

    return result


def fetch_lng_prices():
    """采集国内LNG价格（多数据源自动采集，失败回退手动数据）

    数据源优先级:
    1. LNG物联网日报（搜狐/百家号转载）→ 最全面：液厂均价、接收站均价、开工率等
    2. 生意社（100ppi）→ LNG基准价/参考价 + 液厂具体报价
    3. lng168原始网站 → 备用
    4. SHPGX日报（新浪财经）→ 价格指数 + 中海油基准价
    5. Mysteel → 华东接收站实时价格
    """
    data = {
        "domestic_avg": None, "terminal_avg": None,
        "domestic_high": None, "domestic_low": None,
        "terminal_high": None, "terminal_low": None,
        "terminal_high_name": None, "terminal_low_name": None,
        "operating_rate": None, "up_count": None, "down_count": None,
        "market_summary": "",
        "benchmark": None, "reference": None, "ref_change_pct": None,  # 生意社数据
        "plant_quotes": [],  # 生意社液厂报价
        "auction_price": None, "auction_volume": None,  # 竞拍数据
        "market_comment": "",  # 市场评述
        "shpgx": {},  # SHPGX数据
        "source": "manual",
        # === 各码头接收站价格（元/吨，手动后备） ===
        "terminals": {
            "华东": [
                {"name": "如东", "company": "中石油", "province": "江苏", "price": 6350, "change": 0, "note": "出货平稳"},
                {"name": "启东", "company": "广汇", "province": "江苏", "price": 6310, "change": 0, "note": ""},
                {"name": "滨海", "company": "中海油", "province": "江苏", "price": 6510, "change": 0, "note": "华东价格高地"},
                {"name": "温州", "company": "浙能", "province": "浙江", "price": 6450, "change": 0, "note": ""},
                {"name": "嘉兴", "company": "杭嘉鑫", "province": "浙江", "price": 6670, "change": 0, "note": "华东最高"},
                {"name": "宁波北仑", "company": "中海油", "province": "浙江", "price": "6400~6950", "change": 0, "note": "苏北6400/浙江6950"},
            ],
            "华南": [
                {"name": "北海", "company": "国家管网", "province": "广西", "price": 6600, "change": 0, "note": "出厂价，西南方向主要货源"},
                {"name": "莆田/漳州", "company": "国家管网", "province": "福建", "price": 6600, "change": 0, "note": "⚠ 暂不对外出货"},
                {"name": "惠州", "company": "广东能源", "province": "广东", "price": None, "change": None, "note": "🚫 暂停竞拍（美伊战争影响）"},
                {"name": "潮州华瀛", "company": "华瀛", "province": "广东", "price": None, "change": None, "note": "🚫 暂不外销"},
            ],
            "华北/东北": [
                {"name": "曹妃甸", "company": "中石油", "province": "河北", "price": 6270, "change": "↓小幅回落", "note": "全国最低价"},
                {"name": "天津南港", "company": "中石化", "province": "天津", "price": 6700, "change": 0, "note": "京津冀主要气源"},
                {"name": "天津浮式", "company": "中海油", "province": "天津", "price": 6650, "change": 0, "note": ""},
                {"name": "大连", "company": "中石油", "province": "辽宁", "price": 6550, "change": 0, "note": "东北主力接收站"},
                {"name": "青岛董家口", "company": "中石化", "province": "山东", "price": 6700, "change": 0, "note": ""},
                {"name": "青岛即墨", "company": "国家管网", "province": "山东", "price": 6650, "change": 0, "note": ""},
            ],
        },
    }
    
    # === 数据源1: LNG物联网日报（搜狐/百家号转载）→ 最全面 ===
    lng168_web = fetch_lng168_daily_from_web()
    if lng168_web["source"] != "failed":
        for key in ["domestic_avg", "terminal_avg", "domestic_high", "domestic_low",
                     "terminal_high", "terminal_high_name", "terminal_low", "terminal_low_name",
                     "operating_rate", "up_count", "down_count", "auction_price", "auction_volume",
                     "market_comment"]:
            if lng168_web.get(key) is not None:
                data[key] = lng168_web[key]
        data["source"] = "lng168-web"

    # === 数据源2: 生意社（100ppi）→ LNG基准价/参考价 + 液厂报价 ===
    pppi = fetch_from_100ppi()
    if pppi["source"] != "failed":
        # 生意社基准价/参考价补充（更可靠）
        if pppi.get("benchmark") and not data.get("domestic_avg"):
            data["domestic_avg"] = int(pppi["benchmark"])
        if pppi.get("reference"):
            data["reference"] = pppi["reference"]
            data["ref_change_pct"] = pppi.get("ref_change_pct")
        if pppi.get("benchmark"):
            data["benchmark"] = pppi["benchmark"]
        if pppi.get("plant_quotes"):
            data["plant_quotes"] = pppi["plant_quotes"]
        if data["source"] == "manual":
            data["source"] = "100ppi"
        elif data["source"] == "lng168-web":
            data["source"] = "lng168-web+100ppi"

    # === 数据源3: lng168原始网站（备用，补缺） ===
    if not data["domestic_avg"] and not data["terminal_avg"]:
        try:
            search_url = "https://www.lng168.com/gateWay/newsList?keyword=LNG%E5%B8%82%E5%9C%BA%E6%95%B4%E4%BD%93%E6%8A%A5%E4%BB%B7"
            search_text = http_get(search_url, timeout=10)
            article_ids = list(dict.fromkeys(re.findall(r'newsDetail\?id=(\d+)', search_text or "")))

            for aid in article_ids[:5]:
                article_url = f"https://www.lng168.com/gateWay/newsDetail?id={aid}"
                article_text = http_get(article_url, timeout=10)
                if not article_text:
                    continue
                clean_text = re.sub(r'<[^>]+>', ' ', article_text)
                clean_text = re.sub(r'\s+', ' ', clean_text)

                avg_match = re.search(r'市场均价为(\d+)\s*元', clean_text)
                if avg_match:
                    data["domestic_avg"] = int(avg_match.group(1))
                high_match = re.search(r'较高价报价(\d+)\s*元', clean_text)
                if high_match:
                    data["domestic_high"] = int(high_match.group(1))
                low_match = re.search(r'较低价报价(\d+)\s*元', clean_text)
                if low_match:
                    data["domestic_low"] = int(low_match.group(1))
                rate_match = re.search(r'开工率(\d+)%', clean_text)
                if rate_match:
                    data["operating_rate"] = int(rate_match.group(1))
                term_avg_match = re.search(r'接收站均价\s*为(\d+)\s*元', clean_text)
                if term_avg_match:
                    data["terminal_avg"] = int(term_avg_match.group(1))
                term_high_match = re.search(r'较\s*高价是\s*(.+?)\s*报价(\d+)\s*元', clean_text)
                if term_high_match:
                    data["terminal_high_name"] = term_high_match.group(1).strip()
                    data["terminal_high"] = int(term_high_match.group(2))
                term_low_match = re.search(r'较\s*低价是(.+?)\s*报价(\d+)\s*元', clean_text)
                if term_low_match:
                    data["terminal_low_name"] = term_low_match.group(1).strip()
                    data["terminal_low"] = int(term_low_match.group(2))
                up_match = re.search(r'(\d+)家\s*调\s*涨', clean_text)
                if up_match:
                    data["up_count"] = int(up_match.group(1))
                down_match = re.search(r'(\d+)家\s*降\s*价', clean_text)
                if down_match:
                    data["down_count"] = int(down_match.group(1))

                if data["domestic_avg"] or data["terminal_avg"]:
                    data["source"] = "lng168"
                    print(f"  LNG数据已从lng168更新: 液厂{data['domestic_avg']}元/吨, 接收站{data['terminal_avg']}元/吨")
                    break
        except Exception as e:
            print(f"[WARN] lng168 LNG数据抓取失败: {e}")

    # === 数据源4: SHPGX日报（新浪财经）→ 价格指数 + 中海油基准价 ===
    shpgx_data = fetch_shpgx_daily()
    data["shpgx"] = shpgx_data
    # 只使用非过时的SHPGX数据
    shpgx_is_stale = shpgx_data.get("source", "").endswith("stale")
    if not shpgx_is_stale:
        # 用SHPGX的价格指数补缺
        if shpgx_data.get("lng_factory_price") and not data.get("domestic_avg"):
            data["domestic_avg"] = shpgx_data["lng_factory_price"]
        if shpgx_data.get("lng_terminal_price") and not data.get("terminal_avg"):
            data["terminal_avg"] = shpgx_data["lng_terminal_price"]
        # 用中海油基准价更新接收站价格（覆盖手动后备值）
        if shpgx_data.get("cnooc_terminals"):
            _update_terminals_from_cnooc(data["terminals"], shpgx_data["cnooc_terminals"])
            if data["source"] == "manual":
                data["source"] = "shpgx"
            else:
                data["source"] = data["source"].replace("manual", "shpgx")
    else:
        print(f"[INFO] SHPGX数据已过时({shpgx_data.get('article_date')})，跳过使用")

    # === 数据源5: Mysteel 华东LNG接收站实时价格（最精确的华东数据） ===
    try:
        mysteel_terminals = fetch_mysteel_lng_terminals()
        if mysteel_terminals:
            terminals = data.get("terminals", {})
            for mt in mysteel_terminals:
                region = mt.get("region", "华东")
                if region not in terminals:
                    terminals[region] = []
                found = False
                for existing in terminals.get(region, []):
                    if mt["name"] in existing["name"] or existing["name"] in mt["name"]:
                        # Mysteel数据优先（更精确），但中海油基准价也是好数据
                        # 如果已有中海油数据则保留，Mysteel只在没有时覆盖
                        if existing.get("note", "") != "SHPGX中海油基准价":
                            existing["price"] = mt["price"]
                            existing["change"] = mt["change"]
                            existing["note"] = f"Mysteel实时数据"
                        found = True
                        break
                if not found:
                    terminals.setdefault(region, []).append({
                        "name": mt["name"],
                        "company": mt["company"],
                        "province": mt["province"],
                        "price": mt["price"],
                        "change": mt["change"],
                        "note": mt.get("note", "Mysteel实时数据"),
                    })
            data["terminals"] = terminals
            if data["source"] == "manual":
                data["source"] = "mysteel"
    except Exception as e:
        print(f"[WARN] Mysteel终端数据集成失败: {e}")

    return data


def _update_terminals_from_cnooc(terminals_dict, cnooc_list):
    """用SHPGX中海油基准价更新接收站价格表"""
    # 中海油接收站名称映射
    cnooc_name_map = {
        "浙江宁波LNG接收站": ("宁波北仑", "中海油"),
        "江苏滨海LNG接收站": ("滨海", "中海油"),
        "珠海LNG接收站": ("珠海金湾", "中海油"),
        "粤东接收站": ("粤东惠来", "国家管网"),
        "国网天津LNG接收站": ("天津浮式", "中海油"),
        "北燃南港LNG接收站": ("天津南港", "中石化"),
    }

    # 按接收站+省份聚合价格
    cnooc_by_name = {}
    for c in cnooc_list:
        key = c["name"]
        if key not in cnooc_by_name:
            cnooc_by_name[key] = []
        cnooc_by_name[key].append(c)

    for cnooc_name, entries in cnooc_by_name.items():
        mapped = cnooc_name_map.get(cnooc_name)
        if not mapped:
            continue
        short_name, company = mapped

        # 找到对应region中的接收站
        for region, stations in terminals_dict.items():
            for s in stations:
                if short_name in s["name"] or s["name"] in short_name:
                    # 如果有多个省份不同价格，合并显示
                    prices = [e["price"] for e in entries if e.get("price")]
                    if len(prices) == 0:
                        continue
                    elif len(prices) == 1:
                        s["price"] = prices[0]
                    else:
                        price_min, price_max = min(prices), max(prices)
                        if price_min == price_max:
                            s["price"] = price_min
                        else:
                            s["price"] = f"{price_min}~{price_max}"
                    s["note"] = "SHPGX中海油基准价"
                    break

    # 添加珠海金湾（如果不在已有列表中）
    has_zhuhai = any("珠海" in s["name"] for region_stations in terminals_dict.values() for s in region_stations)
    if not has_zhuhai:
        zhuhai_entries = cnooc_by_name.get("珠海LNG接收站", [])
        if zhuhai_entries:
            prices = [e["price"] for e in zhuhai_entries if e.get("price")]
            if prices:
                terminals_dict.setdefault("华南", []).append({
                    "name": "珠海金湾",
                    "company": "中海油",
                    "province": "广东",
                    "price": f"{min(prices)}~{max(prices)}" if min(prices) != max(prices) else prices[0],
                    "change": 0,
                    "note": "SHPGX中海油基准价",
                })

    # 添加粤东惠来
    has_yuedong = any("粤东" in s["name"] for region_stations in terminals_dict.values() for s in region_stations)
    if not has_yuedong:
        yuedong_entries = cnooc_by_name.get("粤东接收站", [])
        if yuedong_entries:
            prices = [e["price"] for e in yuedong_entries if e.get("price")]
            if prices:
                terminals_dict.setdefault("华南", []).append({
                    "name": "粤东惠来",
                    "company": "国家管网",
                    "province": "广东",
                    "price": prices[0],
                    "change": 0,
                    "note": "SHPGX中海油基准价",
                })

def fetch_pipeline_gas_prices(shpgx_data=None):
    """管道天然气门站价格及交易数据
    新增：从SHPGX日报获取实时价格指数和成交数据
    门站价为月度/季度发布，非日频数据
    """
    data = {
        "provinces": {
            "北京": {"base": 1860, "regulated": 2204, "unregulated": 3162, "peak": 4805},
            "天津": {"base": 1860, "regulated": 2204, "unregulated": 3162, "peak": 4805},
            "河北": {"base": 1840, "regulated": 2180, "unregulated": 3128, "peak": 4805},
            "山西": {"base": 1770, "regulated": 2097, "unregulated": 3009, "peak": 4805},
            "内蒙古": {"base": 1220, "regulated": 1446, "unregulated": 2196, "peak": 4805},
            "辽宁": {"base": 1840, "regulated": 2180, "unregulated": 3128, "peak": 4805},
        },
        "unit": "元/千立方米",
        "update_date": "2026-05-25",
        "source": "我的钢铁网/隆众资讯/各省发改委",
        # === SHPGX实时数据 ===
        "shpgx_lng_factory": None,    # LNG出厂价格指数（日频）
        "shpgx_lng_terminal": None,   # LNG出站价格指数（日频）
        "shpgx_pipe_spot": None,      # 管道气现货价格（月频）
        "shpgx_pipe_monthly": None,   # 管道气现货月度均价（月频）
        "shpgx_trade_data": [],       # 成交行情
        "shpgx_date": "",             # 数据日期
    }

    # 填充SHPGX实时数据（跳过过时数据）
    if shpgx_data and shpgx_data.get("source") not in ("failed",) and not shpgx_data.get("source", "").endswith("stale"):
        data["shpgx_lng_factory"] = shpgx_data.get("lng_factory_price")
        data["shpgx_lng_terminal"] = shpgx_data.get("lng_terminal_price")
        data["shpgx_pipe_spot"] = shpgx_data.get("pipeline_spot_price")
        data["shpgx_pipe_monthly"] = shpgx_data.get("pipeline_monthly_avg")
        data["shpgx_trade_data"] = shpgx_data.get("trade_data", [])
        data["shpgx_date"] = shpgx_data.get("article_date", "")
        data["source"] = f"我的钢铁网/隆众资讯/各省发改委 + SHPGX({shpgx_data.get('article_date', '')})"

    return data

def fetch_jkm_price():
    """东北亚JKM现货价格
    数据源优先级: Yahoo Finance JKM=F(主力) -> Nasdaq Data Link(备用) -> 手动估算"""
    data = {"price": None, "change_pct": None, "high": None, "low": None, "date": None, "source": "manual"}

    # 主力数据源: Yahoo Finance JKM=F
    yahoo = fetch_from_yahoo("JKM=F")
    if yahoo and yahoo["price"]:
        data["price"] = yahoo["price"]
        data["change_pct"] = yahoo.get("change_pct")
        data["high"] = yahoo.get("high")
        data["low"] = yahoo.get("low")
        data["date"] = yahoo.get("date")
        data["source"] = "yahoo"
        return data

    # 备用数据源: Nasdaq Data Link CHRIS/CME_JKM1 (免费注册API key)
    nasdaq_key = os.environ.get("NASDAQ_API_KEY", "")
    if nasdaq_key:
        try:
            url = f"https://data.nasdaq.com/api/v3/datasets/CHRIS/CME_JKM1/data.json?rows=3&api_key={nasdaq_key}"
            nasdaq_text = http_get(url, timeout=15)
            if nasdaq_text:
                nasdaq_data = json.loads(nasdaq_text)
                rows = nasdaq_data.get("dataset_data", {}).get("data", [])
                if rows:
                    latest = rows[0]
                    prev = rows[1] if len(rows) > 1 else latest
                    data["price"] = float(latest[4]) if len(latest) > 4 else float(latest[1])
                    if prev and len(prev) > 4:
                        prev_close = float(prev[4])
                        if data["price"] and prev_close > 0:
                            data["change_pct"] = (data["price"] - prev_close) / prev_close * 100
                    data["date"] = latest[0]
                    data["source"] = "nasdaq"
                    return data
        except Exception as e:
            print(f"[WARN] Nasdaq JKM API失败: {e}")

    return data

def fetch_geopolitical_news():
    """从 Google News RSS 采集地缘政治与能源要闻"""
    news = []
    import urllib.parse as _urlparse

    # 搜索关键词及对应RSS
    rss_queries = [
        ("Iran oil energy Hormuz", "en-US", "US:en"),
        ("Brent WTI crude oil price OPEC", "en-US", "US:en"),
        ("natural gas LNG market Asia", "en-US", "US:en"),
    ]

    seen_titles = set()
    for q, hl, ceid in rss_queries:
        try:
            encoded_q = _urlparse.quote(q)
            url = f"https://news.google.com/rss/search?q={encoded_q}&hl={hl}&gl=US&ceid={ceid}"
            text = http_get(url, timeout=15)
            if not text:
                continue

            # 解析RSS中的item
            items = re.findall(
                r'<item>.*?<title>(.*?)</title>.*?<pubDate>(.*?)</pubDate>.*?<link>(.*?)</link>.*?</item>',
                text, re.DOTALL
            )
            for title_raw, pub_date, link in items:
                title = title_raw.replace('<![CDATA[', '').replace(']]>', '').strip()
                if title in seen_titles or title.startswith('"'):
                    continue
                seen_titles.add(title)

                # 解析日期
                date_str = pub_date.strip()
                try:
                    from email.utils import parsedate_to_datetime
                    dt = parsedate_to_datetime(date_str)
                    date_fmt = dt.strftime("%Y-%m-%d")
                except Exception:
                    date_fmt = date_str[:16]

                # 推断市场影响
                impact = "市场关注中"
                t_lower = title.lower()
                if any(w in t_lower for w in ['surge', 'jump', 'spike', 'soar', 'rally', '大涨', '飙升']):
                    impact = "油价/气价上涨压力"
                elif any(w in t_lower for w in ['drop', 'fall', 'plunge', 'slide', '大跌', '回落']):
                    impact = "油价/气价下行"
                elif any(w in t_lower for w in ['iran', 'hormuz', 'attack', 'strike', 'war', 'conflict']):
                    impact = "地缘风险推升能源价格"
                elif any(w in t_lower for w in ['opec', 'production cut', 'supply']):
                    impact = "供应端变化影响油价"
                elif any(w in t_lower for w in ['demand', 'china', 'inventory']):
                    impact = "需求端变化影响油价"

                news.append({
                    "date": date_fmt,
                    "title": title[:120],
                    "summary": "",
                    "impact": impact,
                    "source": "Google News",
                })
        except Exception as e:
            print(f"[WARN] Google News RSS失败({q[:20]}): {e}")

    # 按日期降序排列，取前8条
    news.sort(key=lambda x: x["date"], reverse=True)
    return news[:8]


def fetch_market_insights(oil_data=None, hh_data=None, ttf_data=None, jkm_data=None, lng_data=None):
    """基于当日实际价格数据动态生成市场洞察"""
    # 获取当前价格，缺失则用默认值
    brent = (oil_data or {}).get("brent") or 95.0
    wti = (oil_data or {}).get("wti") or 93.0
    brent_chg = (oil_data or {}).get("brent_change") or 0
    hh = (hh_data or {}).get("price") or 3.0
    hh_chg = (hh_data or {}).get("change_pct") or 0
    ttf = (ttf_data or {}).get("price") or 50.0
    ttf_chg = (ttf_data or {}).get("change_pct") or 0
    jkm = (jkm_data or {}).get("price") or 19.0
    jkm_chg = (jkm_data or {}).get("change_pct") or 0
    lng_avg = (lng_data or {}).get("domestic_avg") or 6000
    term_avg = (lng_data or {}).get("terminal_avg") or 6800
    op_rate = (lng_data or {}).get("operating_rate") or 47

    # 涨跌方向判断
    def _dir(val, threshold=0.5):
        if val > threshold: return "上涨"
        elif val < -threshold: return "下跌"
        else: return "窄幅波动"

    oil_dir = _dir(brent_chg)
    hh_dir = _dir(hh_chg)
    ttf_dir = _dir(ttf_chg)

    # 原油洞察
    if brent > 100:
        oil_headline = f"布伦特突破{brent:.0f}美元，地缘风险溢价持续攀升"
    elif brent > 90:
        oil_headline = f"布伦特守稳{brent:.0f}美元上方，{oil_dir}格局延续"
    elif brent > 80:
        oil_headline = f"布伦特{brent:.0f}美元附近{oil_dir}，市场多空交织"
    else:
        oil_headline = f"布伦特回落至{brent:.0f}美元，油价承压{oil_dir}"

    insights = {
        "oil": {
            "headline": oil_headline,
            "drivers": [
                (f"📊 布伦特{brent:.2f}美元({brent_chg:+.1f}%)", f"WTI报{wti:.2f}美元({(oil_data or {}).get('wti_change', 0):+.1f}%)，价差{wti-brent:.2f}美元。油价日内{oil_dir}，反映当前市场对供需和地缘风险的定价。"),
                ("🇺🇸🇮🇷 中东局势", "美伊冲突持续影响霍尔木兹海峡航运安全，市场对原油供应中断保持警惕。冲突走向仍是油价短期最大变量。"),
                ("🛢️ OPEC+供应", "沙特等国表态将在必要时释放剩余产能，但实际补偿能力存疑。OPEC+下次会议将讨论产量调整。"),
                ("📉 需求前景", "中国炼厂开工回升支撑需求预期，但全球经济增长放缓限制油价上方空间。EIA库存变化需持续关注。"),
            ],
            "outlook": f"布伦特在{brent-5:.0f}-{brent+5:.0f}美元区间{oil_dir}。若美伊冲突缓和，可能回落至{brent-10:.0f}美元；若局势恶化，有冲击{brent+20:.0f}美元风险。",
            "impact_on_gas": f"布伦特{brent:.0f}美元对应LNG长协JCC挂钩价约{brent*0.15:.1f}美元/MMBtu，预计3-6个月后传导至国内进口成本。",
        },
        "gas_intl": {
            "headline": f"JKM {jkm:.1f}美元{'高位震荡' if jkm > 16 else '区间运行'}，TTF {ttf_dir}至{ttf:.1f}欧元",
            "drivers": [
                (f"🚢 JKM {jkm:.1f}美元({jkm_chg:+.1f}%)", f"东北亚LNG现货{'维持高位' if jkm > 16 else '区间波动'}。霍尔木兹海峡通行风险影响卡塔尔LNG出口，亚洲买家溢价采购替代货源。"),
                (f"🇪🇺 TTF {ttf:.1f}欧元/MWh({ttf_chg:+.1f}%)", f"欧洲天然气{'补库需求支撑价格' if ttf > 45 else '需求疲软价格承压'}。EU库存填充率仍低于去年同期，冬季补库采购将持续。"),
                (f"🇺🇸 Henry Hub {hh:.3f}美元({hh_chg:+.1f}%)", f"美国本土产量高位运行，{'天气转暖需求下降' if hh < 3.5 else '供需偏紧支撑价格'}。HH与JKM价差{abs(jkm-hh):.1f}美元，{'套利窗口吸引货流东移' if jkm-hh > 10 else '套利空间有限'}。"),
                ("🌏 亚太供应", "澳大利亚部分LNG项目进入检修期，减少亚太地区现货供应量。美国墨西哥湾LNG出口终端满负荷运行。"),
            ],
            "outlook": f"JKM短期{'维持{jkm-2:.0f}-{jkm+3:.0f}美元高位' if jkm > 16 else f'{jkm-3:.0f}-{jkm+3:.0f}美元区间运行'}。HH预计在{max(2.0,hh-0.5):.1f}-{hh+0.5:.1f}美元区间。",
            "impact_on_gas": f"JKM {jkm:.1f}美元折合到岸完税价约{jkm*350+500:.0f}元/吨，{'高于' if jkm*350+500 > term_avg else '接近'}国内接收站出站价{term_avg}元/吨，进口利润窗口{'关闭' if jkm*350+500 > term_avg else '微利'}。",
        },
        "lng_domestic": {
            "headline": f"国产液价{lng_avg}元/吨{'盘整' if lng_avg < 6200 else '偏强'}，接收站{term_avg}元/吨{'坚挺' if term_avg > 6500 else '松动'}",
            "drivers": [
                (f"🏭 液厂开工率{op_rate}%", f"全国133家液厂开工率{op_rate}%，{'供应偏紧支撑价格' if op_rate < 50 else '供应充足价格承压'}。西北/华北液厂原料气成本偏高。"),
                (f"📦 接收站均价{term_avg}元/吨", f"进口LNG到岸成本高企，接收站{'维持出站报价坚挺' if term_avg > 6500 else '价格有所松动'}。国产液与接收站价差{term_avg-lng_avg}元/吨。"),
                ("🌡️ 非采暖季需求", "全国大部气温回升，采暖需求消退，下游城燃采购以刚需为主，市场交投清淡。夏季制冷需求对气电有一定支撑。"),
                ("💰 进口成本", f"JKM现货{jkm:.1f}美元/MMBtu，折合到岸完税约{jkm*350+500:.0f}元/吨，{'倒挂风险较大' if jkm*350+500 > term_avg else '进口窗口微开'}。"),
            ],
            "outlook": f"非采暖季国产LNG价格预计在{lng_avg-300}-{lng_avg+200}元/吨区间震荡；接收站价格{'受进口成本支撑维持{term_avg-200}-{term_avg+200}元/吨' if term_avg > 6500 else '有望随JKM回落而松动'}。",
            "impact_on_gas": f"接收站与国产液价差{term_avg-lng_avg}元/吨，城燃企业{'应优先使用管道气合同量，LNG现货仅作调峰补充' if term_avg-lng_avg > 500 else '可适度增加LNG现货采购降低综合成本'}。",
        },
    }
    return insights

# ============================================================
# 报告生成模块
# ============================================================

def generate_terminal_tables(lng_data):
    """生成接收站分区域价格表HTML"""
    terminals = lng_data.get("terminals", {})
    if not terminals:
        return "<p style='color:#999;'>暂无码头价格数据</p>"
    
    region_colors = {
        "华东": "#2980b9",
        "华南": "#27ae60",
        "华北/东北": "#e67e22",
    }
    
    html_parts = []
    for region, stations in terminals.items():
        color = region_colors.get(region, "#666")
        rows = ""
        for s in stations:
            price = s.get("price")
            if price is None:
                price_display = '<span style="color:#e74c3c;">——</span>'
            elif isinstance(price, str):
                price_display = f'<span style="font-weight:700;">{price}</span>'
            else:
                price_display = f'<span style="font-weight:700;">{price:,}</span>'
            
            change = s.get("change")
            if change is None:
                change_display = "——"
            elif isinstance(change, str):
                change_display = f'<span class="tag tag-green">{change}</span>'
            elif change == 0:
                change_display = '<span class="tag tag-green">0</span>'
            else:
                cls = "tag-red" if change > 0 else "tag-green"
                change_display = f'<span class="tag {cls}">{change:+d}</span>'
            
            note = s.get("note", "")
            note_display = f'<span style="color:#e74c3c;">{note}</span>' if "⚠" in note or "🚫" in note else note
            
            rows += f"""<tr><td>{s['name']}</td><td>{s['company']}</td><td>{s['province']}</td><td>{price_display}</td><td>{change_display}</td><td>{note_display}</td></tr>"""
        
        html_parts.append(f"""
    <div style="margin-bottom:18px;">
      <h4 style="font-size:14px;color:{color};margin-bottom:8px;">📍 {region}地区</h4>
      <table>
        <thead><tr><th>接收站</th><th>所属企业</th><th>省份</th><th>报价</th><th>涨跌</th><th>备注</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>""")
    
    return "".join(html_parts)


def _lng_auction_row(lng_data):
    """竞拍数据行"""
    price = lng_data.get("auction_price")
    volume = lng_data.get("auction_volume")
    if price:
        return f'<tr><td><strong>原料气竞拍</strong></td><td>{price}</td><td>—</td><td>—</td><td>{volume or "中石油直供"}</td><td>元/方</td></tr>'
    return '<tr><td><strong>原料气竞拍</strong></td><td colspan="5" style="color:#999;">暂无当日竞拍数据</td></tr>'


def _build_benchmark_section(lng_data):
    """构建生意社基准价/参考价展示区"""
    benchmark = lng_data.get("benchmark")
    reference = lng_data.get("reference")
    ref_change = lng_data.get("ref_change_pct")

    if not benchmark and not reference:
        return ""

    parts = []
    if benchmark:
        parts.append(f'<span style="font-weight:700;color:#2c3e50;">基准价 {int(benchmark):,} 元/吨</span>')
    if reference:
        change_str = f" ({ref_change:+.2f}%)" if ref_change is not None else ""
        parts.append(f'<span style="font-weight:700;color:#2980b9;">参考价 {int(reference):,} 元/吨{change_str}</span>')

    return f"""
    <div style="margin-top:10px;padding:10px 16px;background:#eaf2f8;border:1px solid #aed6f1;border-radius:8px;font-size:13px;">
      📊 <strong>生意社数据：</strong>{" | ".join(parts)} | 数据来源：<a href="https://trq.100ppi.com/" style="color:#2980b9;">生意社</a>
    </div>"""


def _build_plant_quotes_section(lng_data):
    """构建液厂出厂报价表"""
    quotes = lng_data.get("plant_quotes", [])
    if not quotes:
        return ""

    rows = ""
    for q in quotes[:15]:  # 最多展示15家
        price = q.get("price", "—")
        if isinstance(price, (int, float)):
            price_str = f"{int(price):,}"
        else:
            price_str = str(price)
        rows += f"""
        <tr><td>{q.get('name', '—')}</td><td>{q.get('province', '—')}</td><td style="font-weight:700;">{price_str}</td><td>{q.get('date', '—')}</td></tr>"""

    return f"""
    <div style="margin-top:12px;">
      <h5 style="font-size:13px;color:#555;margin-bottom:6px;">🏭 主要液厂出厂报价（生意社）</h5>
      <table>
        <thead><tr><th>企业</th><th>省份</th><th>出厂价（元/吨）</th><th>日期</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
      <div style="font-size:11px;color:#999;margin-top:4px;">数据来源：<a href="https://trq.100ppi.com/bj/index.html" style="color:#2980b9;">生意社-液化天然气报价</a> | 仅展示部分企业，完整报价见源网站</div>
    </div>"""


def _build_market_comment_section(lng_data):
    """构建市场评述HTML"""
    comment = lng_data.get("market_comment", "")
    if not comment:
        return ""
    parts = comment.split("；")
    items = "".join(f'<div style="margin:4px 0;">• {p.strip()}</div>' for p in parts if p.strip())
    return f"""
    <div style="margin-bottom:10px;padding:10px 14px;background:#fffde7;border:1px solid #f9e79f;border-radius:6px;font-size:13px;color:#7d6608;">
      <strong>📝 LNG物联网市场评述：</strong>{items}
    </div>"""


def _build_shpgx_section(pipe_data, report_date):
    """构建SHPGX交易数据HTML区块（动态数据）"""
    shpgx_date = pipe_data.get("shpgx_date", "")
    date_label = f"（{shpgx_date}）" if shpgx_date else f"（{report_date}）"

    # 成交行情
    trade_data = pipe_data.get("shpgx_trade_data", [])
    trade_rows = ""
    if trade_data:
        for t in trade_data:
            trade_rows += f"""
          <tr><td>{t.get('type', '—')}</td><td style="font-weight:700;">{t.get('volume', '—'):,} 吨</td><td>—</td><td></td></tr>"""
    else:
        trade_rows = '<tr><td colspan="4" style="text-align:center;color:#999;">暂无当日成交数据</td></tr>'

    # 价格指数
    lng_factory = pipe_data.get("shpgx_lng_factory")
    lng_terminal = pipe_data.get("shpgx_lng_terminal")
    pipe_spot = pipe_data.get("shpgx_pipe_spot")
    pipe_monthly = pipe_data.get("shpgx_pipe_monthly_avg")

    index_rows = ""
    if lng_factory:
        index_rows += f"""
            <tr><td>中国LNG出厂价格</td><td style="font-weight:700;">{lng_factory:,}</td><td>—</td><td>—</td><td>元/吨</td></tr>"""
    if lng_terminal:
        index_rows += f"""
            <tr><td>中国LNG出站价格</td><td style="font-weight:700;">{lng_terminal:,}</td><td>—</td><td>—</td><td>元/吨</td></tr>"""
    if pipe_spot:
        index_rows += f"""
            <tr style="background:#fffbf5;"><td><strong>🔥 管道气现货价格</strong></td><td style="font-weight:700;color:#e74c3c;">{pipe_spot}</td><td>—</td><td>—</td><td>元/立方米</td></tr>"""
    if pipe_monthly:
        index_rows += f"""
            <tr><td>管道气现货月度均价</td><td style="font-weight:700;">{pipe_monthly}</td><td>—</td><td>—</td><td>元/立方米</td></tr>"""

    if not index_rows:
        index_rows = '<tr><td colspan="5" style="text-align:center;color:#999;">暂无当日价格指数</td></tr>'

    has_real_data = bool(lng_factory or lng_terminal or pipe_spot or trade_data)
    source_badge = '<span class="tag tag-blue">实时数据</span>' if has_real_data else '<span class="tag" style="background:#ffc107;color:#856404;">参考数据</span>'

    return f"""
    <div style="margin-bottom:12px;padding:8px 12px;background:#e8f4fd;border:1px solid #2980b9;border-radius:6px;font-size:13px;color:#1a5276;">
      📡 本节数据来自新浪财经转载的SHPGX每日公告，{source_badge}，数据日期{date_label}
    </div>
    <div style="margin-bottom:18px;">
      <h4 style="font-size:14px;color:#2980b9;margin-bottom:8px;">🏛 上海石油天然气交易中心（SHPGX）{date_label}</h4>
      <table>
        <thead><tr><th>交易品种</th><th>成交量</th><th>成交均价</th><th>备注</th></tr></thead>
        <tbody>{trade_rows}</tbody>
      </table>
      <div style="margin-top:12px;">
        <h5 style="font-size:13px;color:#555;margin-bottom:6px;">📊 SHPGX 价格指数</h5>
        <table>
          <thead><tr><th>指数名称</th><th>最新值</th><th>上期</th><th>变动</th><th>单位</th></tr></thead>
          <tbody>{index_rows}</tbody>
        </table>
      </div>
    </div>"""


def _build_auction_section(lng_data):
    """构建竞拍数据HTML区块"""
    auction_price = lng_data.get("auction_price")
    auction_volume = lng_data.get("auction_volume")
    market_comment = lng_data.get("market_comment", "")

    # 竞拍数据行
    auction_rows = ""
    if auction_price:
        auction_rows += f"""
          <tr style="background:#fffbf5;">
            <td><strong>🔥 原料气竞拍</strong></td><td>最新</td><td style="font-weight:700;color:#e74c3c;">{auction_price}</td><td>{auction_volume or '—'}</td><td>全部成交</td></tr>"""
    else:
        auction_rows = '<tr><td colspan="5" style="text-align:center;color:#999;">暂无当日竞拍数据（可能非竞拍日）</td></tr>'

    # 市场评述
    comment_html = ""
    if market_comment:
        # 按分号分割，每段一行
        parts = market_comment.split("；")
        comment_items = "".join(f"<div>• {p.strip()}</div>" for p in parts if p.strip())
        comment_html = f"""
      <div style="margin-top:10px;padding:10px 14px;background:#fffde7;border:1px solid #f9e79f;border-radius:6px;font-size:12px;color:#7d6608;">
        <strong>📝 LNG物联网市场评述：</strong>{comment_items}
      </div>"""

    return f"""
    <div style="margin-bottom:18px;">
      <h4 style="font-size:14px;color:#e67e22;margin-bottom:8px;">🏛 原料气竞拍 & LNG物联网市场评述</h4>
      <table>
        <thead><tr><th>竞拍品种</th><th>日期</th><th>成交价</th><th>成交量</th><th>备注</th></tr></thead>
        <tbody>{auction_rows}</tbody>
      </table>
      {comment_html}
    </div>"""


def _pipe_spot_str(pipe_data):
    """管道气现货价格文本"""
    spot = pipe_data.get("shpgx_pipe_spot")
    if spot:
        return f"<strong>{spot}元/方</strong>"
    return "约4.39元/方（参考值）"


def _pipe_vs_gate_ratio(pipe_data):
    """管道气现货价 vs 门站价倍数"""
    spot = pipe_data.get("shpgx_pipe_spot")
    if spot:
        ratio = spot / 2.2
        return f"{ratio:.1f}倍"
    return "两倍"


def _auction_insight(lng_data):
    """竞拍数据洞察"""
    auction_price = lng_data.get("auction_price")
    if auction_price:
        return f"最新原料气竞拍成交价{auction_price}，{lng_data.get('auction_volume', '')}，反映上游气源成本持续高企。"
    return "延长石油靖边等竞拍数据需关注各交易中心公告获取最新信息。"


def generate_html_report(report_date, oil_data, hh_data, jkm_data, lng_data, pipe_data, news_data, insights_data=None, ttf_data=None, fx_data=None):
    """生成完整的HTML日报"""
    
    if insights_data is None:
        insights_data = {}
    oil_insight = insights_data.get("oil", {})
    gas_insight = insights_data.get("gas_intl", {})
    lng_insight = insights_data.get("lng_domestic", {})
    
    # 国际油价
    brent = oil_data.get("brent") or 95.31
    wti = oil_data.get("wti") or 94.59
    brent_chg = oil_data.get("brent_change") or 2.6
    wti_chg = oil_data.get("wti_change") or 2.7
    brent_high = oil_data.get("brent_high")
    brent_low = oil_data.get("brent_low")
    wti_high = oil_data.get("wti_high")
    wti_low = oil_data.get("wti_low")
    
    # 国际天然气
    hh_price = hh_data.get("price") or 3.079
    hh_chg = hh_data.get("change_pct") or -0.52
    hh_high = hh_data.get("high")
    hh_low = hh_data.get("low")
    
    # TTF
    ttf_price = (ttf_data or {}).get("price") or 58.50
    ttf_chg = (ttf_data or {}).get("change_pct") or 1.2
    ttf_source = (ttf_data or {}).get("source", "manual")

    # 汇率
    usdcny = (fx_data or {}).get("rate") or 6.8240
    fx_source = (fx_data or {}).get("source", "manual")

    # JKM
    jkm = jkm_data.get("price") or 19.04
    jkm_chg = jkm_data.get("change_pct") or 13.6
    jkm_source = jkm_data.get("source", "manual")
    jkm_date = jkm_data.get("date", "")
    
    # 国内LNG
    lng_domestic = lng_data.get("domestic_avg") or 5963
    lng_terminal = lng_data.get("terminal_avg") or 6780
    lng_dom_high = lng_data.get("domestic_high") or 6550
    lng_dom_low = lng_data.get("domestic_low") or 5700
    lng_term_high = lng_data.get("terminal_high") or 7750
    lng_term_high_name = lng_data.get("terminal_high_name") or "国网广西北海"
    lng_term_low = lng_data.get("terminal_low") or 6270
    lng_term_low_name = lng_data.get("terminal_low_name") or "河北曹妃甸"
    lng_op_rate = lng_data.get("operating_rate") or 47
    lng_source = lng_data.get("source", "manual")
    lng_up = lng_data.get("up_count")
    lng_down = lng_data.get("down_count")
    
    lng_change_note = ""
    if lng_up is not None and lng_down is not None:
        lng_change_note = f"今日{lng_up}涨{lng_down}降"
    
    brent_high_str = f"{brent_high:.2f}" if brent_high else "—"
    brent_low_str = f"{brent_low:.2f}" if brent_low else "—"
    wti_high_str = f"{wti_high:.2f}" if wti_high else "—"
    wti_low_str = f"{wti_low:.2f}" if wti_low else "—"
    
    oil_source = oil_data.get("source", "manual")
    hh_source = hh_data.get("source", "manual")
    
    # 管道气省份表
    provinces = pipe_data.get("provinces", {})
    pipe_rows = ""
    for name, p in provinces.items():
        pipe_rows += f"""
        <tr><td>{name}</td><td>{p['base']:,}</td><td>{p['regulated']:,}</td><td>{p['unregulated']:,}</td><td>{p['peak']:,}</td><td>元/千立方米</td></tr>"""
    
    # 新闻摘要
    news_items = ""
    for n in news_data[:5]:
        news_items += f"""
        <tr><td>{n['date']}</td><td><strong>{n['title']}</strong></td><td>{n['impact']}</td><td>{n['source']}</td></tr>"""
    
    # 构建HTML
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>能源市场日报 | {report_date}</title>
<style>
  :root {{ --primary: #1a3a5c; --accent: #e74c3c; --green: #27ae60; --bg: #f5f6fa; --card-bg: #ffffff; --text: #2c3e50; --text-light: #7f8c8d; --border: #dfe6e9; }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; background: var(--bg); color: var(--text); line-height: 1.6; }}
  .header {{ background: linear-gradient(135deg, #1a3a5c 0%, #2c5f8a 100%); color: white; padding: 36px 48px; border-bottom: 4px solid var(--accent); }}
  .header h1 {{ font-size: 28px; font-weight: 700; }}
  .header .subtitle {{ font-size: 14px; opacity: 0.85; margin-top: 6px; }}
  .header .date {{ font-size: 13px; opacity: 0.7; margin-top: 4px; }}
  .container {{ max-width: 1100px; margin: 0 auto; padding: 24px 20px; }}
  .summary-bar {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 24px; }}
  .summary-card {{ background: var(--card-bg); border-radius: 10px; padding: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); border-left: 4px solid var(--primary); }}
  .summary-card.warn {{ border-left-color: var(--accent); }}
  .summary-card .label {{ font-size: 12px; color: var(--text-light); }}
  .summary-card .value {{ font-size: 24px; font-weight: 700; margin: 4px 0; }}
  .summary-card .change {{ font-size: 13px; }}
  .summary-card .change.up {{ color: var(--accent); }}
  .summary-card .change.down {{ color: var(--green); }}
  section {{ margin-bottom: 24px; }}
  .section-title {{ font-size: 18px; font-weight: 700; color: var(--primary); border-bottom: 2px solid var(--border); padding-bottom: 10px; margin-bottom: 16px; }}
  table {{ width: 100%; border-collapse: collapse; background: var(--card-bg); border-radius: 8px; overflow: hidden; box-shadow: 0 1px 4px rgba(0,0,0,0.04); }}
  th {{ background: #eef2f7; color: var(--primary); font-weight: 600; font-size: 13px; text-align: left; padding: 12px 16px; }}
  td {{ padding: 10px 16px; font-size: 14px; border-bottom: 1px solid var(--border); }}
  tr:last-child td {{ border-bottom: none; }}
  .tag {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }}
  .tag-red {{ background: #fde8e8; color: #c0392b; }}
  .tag-green {{ background: #e8f8f0; color: #27ae60; }}
  .tag-blue {{ background: #e8f0fe; color: #2980b9; }}
  .alert-banner {{ background: linear-gradient(135deg, #e74c3c, #c0392b); color: white; padding: 16px 24px; border-radius: 8px; margin-bottom: 16px; display: flex; align-items: center; gap: 12px; font-size: 14px; font-weight: 600; }}
  .analysis-box {{ background: #fffbf5; border: 1px solid #f0c78e; border-radius: 10px; padding: 24px; margin-top: 8px; }}
  .analysis-box h3 {{ color: #e67e22; font-size: 16px; margin-bottom: 12px; }}
  .analysis-box p {{ font-size: 14px; margin-bottom: 10px; text-indent: 2em; }}
  .key-points {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px; margin-top: 16px; }}
  .key-point {{ background: var(--card-bg); border-left: 3px solid #e67e22; padding: 12px 16px; border-radius: 4px; font-size: 13px; }}
  .risk-level {{ padding: 4px 12px; border-radius: 12px; font-size: 12px; font-weight: 700; background: #fde8e8; color: #c0392b; }}
  .footer {{ text-align: center; padding: 20px; font-size: 12px; color: var(--text-light); border-top: 1px solid var(--border); margin-top: 32px; }}
  .data-source {{ font-size: 11px; color: #bbb; float: right; }}
</style>
</head>
<body>
<div class="header">
  <h1>⚡ 能源市场日报</h1>
  <div class="subtitle">城市燃气 · 市场决策参考</div>
  <div class="date">📅 {report_date} | 数据截至 08:00 CST <span class="data-source">油:{oil_source} | 气:{hh_source} | TTF:{ttf_source} | JKM:{jkm_source} | 汇率:{fx_source} | LNG:{lng_source} | 管道气:{pipe_data.get('source', 'manual')[:30]}</span></div>
</div>
<div class="container">
  <div class="alert-banner">
    <span>🚨</span><span>地缘风险预警：美伊冲突持续，霍尔木兹海峡局势反复，国际油价高位震荡</span>
  </div>
  <div class="summary-bar">
    <div class="summary-card warn"><div class="label">布伦特原油</div><div class="value">{brent:.2f}</div><div class="change up">▲ +{brent_chg}% | 美元/桶</div></div>
    <div class="summary-card warn"><div class="label">WTI原油</div><div class="value">{wti:.2f}</div><div class="change up">▲ +{wti_chg}% | 美元/桶</div></div>
    <div class="summary-card"><div class="label">Henry Hub天然气</div><div class="value">{hh_price:.3f}</div><div class="change {'down' if hh_chg < 0 else 'up'}">{'▼' if hh_chg < 0 else '▲'} {abs(hh_chg)}% | 美元/MMBtu</div></div>
    <div class="summary-card"><div class="label">国内LNG出厂均价</div><div class="value">{lng_domestic:,}</div><div class="change down">元/吨 | 开工率{lng_op_rate}%</div></div>
  </div>

  <section>
    <div class="section-title">🛢️ 一、国际原油市场</div>
    <table>
      <thead><tr><th>品种</th><th>最新价</th><th>涨跌幅</th><th>日内高</th><th>日内低</th><th>单位</th></tr></thead>
      <tbody>
        <tr><td><strong>布伦特原油 (ICE)</strong></td><td style="color:#e74c3c;font-weight:700;">{brent:.2f}</td><td><span class="tag tag-red">+{brent_chg:.2f}%</span></td><td>{brent_high_str}</td><td>{brent_low_str}</td><td>美元/桶</td></tr>
        <tr><td><strong>WTI原油 (NYMEX)</strong></td><td style="color:#e74c3c;font-weight:700;">{wti:.2f}</td><td><span class="tag tag-red">+{wti_chg:.2f}%</span></td><td>{wti_high_str}</td><td>{wti_low_str}</td><td>美元/桶</td></tr>
        <tr><td><strong>WTI-Brent价差</strong></td><td>{wti-brent:.2f}</td><td><span class="tag tag-blue">—</span></td><td>—</td><td>—</td><td>美元/桶</td></tr>
      </tbody>
    </table>
    <div style="margin-top:14px;padding:16px 20px;background:#fff8f0;border:1px solid #f0c78e;border-radius:10px;">
      <div style="font-size:15px;font-weight:700;color:#e67e22;margin-bottom:10px;">🔍 原油市场洞察</div>
      <div style="font-size:14px;font-weight:600;color:#c0392b;margin-bottom:8px;">{oil_insight.get('headline', '美伊冲突主导油价走向')}</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px;">
        {"".join(f'<div style="background:#fff;padding:10px 14px;border-radius:6px;border-left:3px solid #e67e22;font-size:13px;"><strong>{d[0]}</strong><br><span style="color:#555;">{d[1]}</span></div>' for d in oil_insight.get('drivers', []))}
      </div>
      <div style="font-size:13px;color:#555;padding:8px 0;border-top:1px dashed #e0d5c5;"><strong>📈 前景展望：</strong>{oil_insight.get('outlook', '短期高位震荡')}</div>
      <div style="font-size:13px;color:#c0392b;margin-top:6px;"><strong>⚡ 对城燃影响：</strong>{oil_insight.get('impact_on_gas', '油价上涨推升LNG长协价格')}</div>
    </div>
  </section>

  <section>
    <div class="section-title">🔥 二、国际天然气期货 &amp; 现货</div>
    <table>
      <thead><tr><th>品种</th><th>最新价</th><th>涨跌幅</th><th>备注</th><th>单位</th></tr></thead>
      <tbody>
        <tr><td><strong>Henry Hub (NYMEX)</strong></td><td>{hh_price:.3f}</td><td><span class="tag {'tag-green' if hh_chg < 0 else 'tag-red'}">{hh_chg:+.2f}%</span></td><td>北美供需平衡</td><td>美元/MMBtu</td></tr>
        <tr><td><strong>TTF (荷兰)</strong></td><td>{ttf_price:.2f}</td><td><span class="tag {'tag-green' if ttf_chg < 0 else 'tag-red'}">{ttf_chg:+.2f}%</span></td><td>欧洲库存偏低</td><td>欧元/兆瓦时</td></tr>
        <tr><td><strong>JKM东北亚现货</strong></td><td>{jkm:.2f}</td><td><span class="tag tag-red">+{jkm_chg}%</span></td><td>{'地缘溢价维持高位' if jkm_source == 'manual' else f'{jkm_source}实时数据'}</td><td>美元/MMBtu</td></tr>
        <tr><td><strong>中国LNG到岸价 (DES)</strong></td><td>18.50</td><td><span class="tag tag-red">+10.8%</span></td><td>跟随JKM联动</td><td>美元/MMBtu</td></tr>
      </tbody>
    </table>
    <div style="margin-top:14px;padding:16px 20px;background:#f0f7ff;border:1px solid #a8c8e8;border-radius:10px;">
      <div style="font-size:15px;font-weight:700;color:#2980b9;margin-bottom:10px;">🔍 天然气市场洞察</div>
      <div style="font-size:14px;font-weight:600;color:#1a5276;margin-bottom:8px;">{gas_insight.get('headline', 'JKM高位震荡，TTF受欧洲补库支撑')}</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px;">
        {"".join(f'<div style="background:#fff;padding:10px 14px;border-radius:6px;border-left:3px solid #2980b9;font-size:13px;"><strong>{d[0]}</strong><br><span style="color:#555;">{d[1]}</span></div>' for d in gas_insight.get('drivers', []))}
      </div>
      <div style="font-size:13px;color:#555;padding:8px 0;border-top:1px dashed #c5d5e5;"><strong>📈 前景展望：</strong>{gas_insight.get('outlook', 'JKM短期维持高位')}</div>
      <div style="font-size:13px;color:#c0392b;margin-top:6px;"><strong>⚡ 对城燃影响：</strong>{gas_insight.get('impact_on_gas', 'JKM高位推升进口成本')}</div>
    </div>
  </section>

  <section>
    <div class="section-title">🏭 三、国内LNG市场总览</div>
    <table>
      <thead><tr><th>类别</th><th>均价</th><th>最高价</th><th>最低价</th><th>备注</th><th>单位</th></tr></thead>
      <tbody>
        <tr><td><strong>国产液厂</strong> (133家)</td><td>{lng_domestic:,}</td><td>{lng_dom_high:,}</td><td>{lng_dom_low:,}</td><td>开工率{lng_op_rate}%，{lng_change_note}</td><td>元/吨</td></tr>
        <tr><td><strong>接收站均价</strong> (19家)</td><td>{lng_terminal:,}</td><td>{lng_term_high:,}（{lng_term_high_name}）</td><td>{lng_term_low:,}（{lng_term_low_name}）</td><td>进口成本高企</td><td>元/吨</td></tr>
        {_lng_auction_row(lng_data)}
      </tbody>
    </table>
    {_build_benchmark_section(lng_data)}
    {_build_plant_quotes_section(lng_data)}
    <div style="margin-top:14px;padding:16px 20px;background:#f5fff5;border:1px solid #b8d4be;border-radius:10px;">
      <div style="font-size:15px;font-weight:700;color:#27ae60;margin-bottom:10px;">🔍 国内LNG市场洞察</div>
      {_build_market_comment_section(lng_data)}
      <div style="font-size:14px;font-weight:600;color:#1e8449;margin-bottom:8px;">{lng_insight.get('headline', '国产液价低位盘整，接收站价格坚挺')}</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px;">
        {"".join(f'<div style="background:#fff;padding:10px 14px;border-radius:6px;border-left:3px solid #27ae60;font-size:13px;"><strong>{d[0]}</strong><br><span style="color:#555;">{d[1]}</span></div>' for d in lng_insight.get('drivers', []))}
      </div>
      <div style="font-size:13px;color:#555;padding:8px 0;border-top:1px dashed #c5ddd0;"><strong>📈 前景展望：</strong>{lng_insight.get('outlook', '非采暖季低位震荡')}</div>
      <div style="font-size:13px;color:#c0392b;margin-top:6px;"><strong>⚡ 对城燃影响：</strong>{lng_insight.get('impact_on_gas', '优先使用管道气合同量')}</div>
    </div>
  </section>

  <!-- 进口LNG接收站（码头）价格明细 -->
  <section>
    <div class="section-title">🚢 三-B、全国主要LNG接收站（码头）进口价格明细</div>
    <div style="margin-bottom:12px;font-size:13px;color:#666;">📅 数据日期：{report_date} | 单位：元/吨（槽批自提出站价） | 来源：我的钢铁网/行业参考价 | <span style="color:#e74c3c;">华东数据为实时抓取，华南/华北为参考价</span></div>
    {generate_terminal_tables(lng_data)}
    <div style="margin-top:12px;padding:14px 18px;background:#fffbf5;border:1px solid #f0c78e;border-radius:8px;font-size:13px;">
      <strong>📌 进口LNG码头市场洞察：</strong><br>
      ① <strong>华东价格坚挺：</strong>主要接收站报价{lng_terminal-200:,}~{lng_terminal+200:,}元/吨，与国产液价差{lng_terminal-lng_domestic}元/吨，进口成本支撑明显。<br>
      ② <strong>进口成本倒挂风险：</strong>JKM约{jkm:.1f}美元/MMBtu，折合到岸完税成本约{jkm*350+500:.0f}元/吨，{'高于' if jkm*350+500 > lng_terminal else '接近'}码头出站价，进口窗口{'关闭' if jkm*350+500 > lng_terminal else '微利'}。<br>
      ③ <strong>数据说明：</strong>华东接收站价格为我的钢铁网实时数据；华南/华北价格为行业参考价，建议关注各交易中心公告获取最新报价。
    </div>
  </section>

  <section>
    <div class="section-title">📡 四、管道天然气交易市场动态</div>
    {_build_shpgx_section(pipe_data, report_date)}
    {_build_auction_section(lng_data)}
    <div style="padding:14px 18px;background:#f4faf7;border:1px solid #b8d4be;border-radius:8px;font-size:13px;">
      <strong>📌 管道气市场洞察：</strong><br>
      ① 管道气现货{_pipe_spot_str(pipe_data)}，是管制气门站价（~2.2元/方）的<strong>{_pipe_vs_gate_ratio(pipe_data)}</strong>。充分落实年度合同量是控成本的核心。<br>
      ② {_auction_insight(lng_data)}
    </div>
  </section>

  <section>
    <div class="section-title">🌍 五、地缘政治要闻 &amp; 美伊冲突进展</div>
    <div style="margin-bottom:14px;padding:14px 18px;background:#fde8e8;border:1px solid #e74c3c;border-radius:8px;">
      <div style="font-size:14px;font-weight:700;color:#c0392b;margin-bottom:6px;">🚨 核心关注：美伊冲突局势图</div>
      <div style="font-size:13px;color:#555;">
        <strong>当前态势：</strong>美军持续打击伊朗沿海军事设施，霍尔木兹海峡航运反复中断。美伊「边打边谈」——军事升级与外交谈判交替推进，市场在风险溢价与和平预期之间剧烈摇摆。<br>
        <strong>关键节点：</strong>霍尔木兹海峡每日通过约2,100万桶原油（占全球海运量21%）及约3.5万亿立方英尺LNG（占全球贸易20%）。海峡封锁将直接冲击亚洲能源供应。<br>
        <strong>最新进展：</strong>伊朗议会已授权政府可在必要时封锁海峡；卡塔尔LNG船队通行多次延迟；沙特承诺释放剩余产能但实际补偿能力存疑。
      </div>
    </div>
    <table>
      <thead><tr><th>日期</th><th>事件</th><th>市场影响</th><th>来源</th></tr></thead>
      <tbody>{news_items}</tbody>
    </table>
    <div style="margin-top:14px;padding:16px 20px;background:#fffbf5;border:1px solid #f0c78e;border-radius:10px;">
      <div style="font-size:15px;font-weight:700;color:#e67e22;margin-bottom:10px;">🔍 地缘风险情景分析</div>
      <div style="display:grid;grid-template-columns:repeat(3, 1fr);gap:12px;">
        <div style="background:#e8f8f0;padding:12px 16px;border-radius:8px;border-top:3px solid #27ae60;">
          <div style="font-size:13px;font-weight:700;color:#27ae60;margin-bottom:6px;">🟢 和平情景</div>
          <div style="font-size:12px;color:#555;">美伊30天内达成停火协议，霍尔木兹全面恢复通航。<br><strong>影响：</strong>布伦特回落至80-85美元，JKM跌至12-14美元/MMBtu，LNG进口成本大幅下降。</div>
        </div>
        <div style="background:#fffbf5;padding:12px 16px;border-radius:8px;border-top:3px solid #e67e22;">
          <div style="font-size:13px;font-weight:700;color:#e67e22;margin-bottom:6px;">🟡 僵持情景（当前）</div>
          <div style="font-size:12px;color:#555;">边打边谈，海峡间歇性受阻，冲突有限升级。<br><strong>影响：</strong>布伦特90-100美元震荡，JKM维持18-22美元，进口LNG成本持续高位。</div>
        </div>
        <div style="background:#fde8e8;padding:12px 16px;border-radius:8px;border-top:3px solid #e74c3c;">
          <div style="font-size:13px;font-weight:700;color:#c0392b;margin-bottom:6px;">🔴 恶化情景</div>
          <div style="font-size:12px;color:#555;">冲突扩大为地面战争，霍尔木兹长期封锁。<br><strong>影响：</strong>布伦特冲击120-200美元，JKM挑战30+美元，国内气价全面倒挂，需启动应急保供预案。</div>
        </div>
      </div>
    </div>
  </section>

  <section>
    <div class="section-title">📊 六、市场分析</div>
    <div class="analysis-box">
      <h3>美伊冲突对城市燃气行业的影响评估</h3>
      <div style="margin:8px 0;"><span style="font-weight:600;">风险等级：</span><span class="risk-level">⚠ 高度风险</span></div>
      <p><strong>核心逻辑：</strong>霍尔木兹海峡是卡塔尔LNG出口的咽喉通道。卡塔尔占全球LNG贸易量约20%，一旦海峡通行受阻，亚洲LNG买家将被迫转向澳大利亚、美国墨西哥湾等替代货源，推高全球LNG现货价格。自2月28日冲突爆发以来，JKM现货价格累计涨幅已超过80%。</p>
      <p><strong>传导路径：</strong>油价上涨→挂钩JCC的LNG长协价格滞后3-6个月跟涨→进口LNG成本攀升→接收站出站价格高企→城市燃气企业采购成本压力加大。当前布伦特站稳95美元上方，将推动2026年下半年LNG长协价格显著上升。</p>
      <p><strong>对城市燃气企业影响：</strong>非居民用气价格倒挂风险加大。当前各省门站管制气价格（约2.0-2.2元/立方米）与LNG现货折算价（约4.5-5.0元/立方米）形成巨大价差。建议企业加快落实非居民用气顺价机制，同时充分利用管道气合同量，减少高价LNG现货采购。</p>
      <div class="key-points">
        <div class="key-point"><strong>▶ 短期策略：</strong>关注6月美伊谈判进展，若协议达成JKM可能快速回落至15美元以下，可在此区间锁定现货。</div>
        <div class="key-point"><strong>▶ 中期策略：</strong>最大化管道气合同量，非采暖季加快储气库注气节奏，降低冬季对高价现货的依赖。</div>
        <div class="key-point"><strong>▶ 风险提示：</strong>若霍尔木兹持续封锁至年底，布伦特可能冲击200美元，JKM可能挑战30美元。需做好极端情景预案。</div>
        <div class="key-point"><strong>▶ 政策关注：</strong>密切跟踪各省非采暖季价格调整窗口，及时启动非居民用气顺价程序。</div>
      </div>
    </div>
  </section>

</div>
<div class="footer">
  <p>本报告由能源市场日报自动生成系统产出 | 数据来源：ICE、NYMEX、Yahoo Finance、EIA、ICE ENDEX、S&P Global Platts、我的钢铁网、LNG物联网、生意社(100ppi)、新浪财经SHPGX、隆众资讯、各省发改委</p>
  <p>声明：本报告仅供内部决策参考，不构成投资建议 | 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')} CST</p>
</div>
</body>
</html>"""
    return html

def generate_text_summary(report_date, oil_data, hh_data, jkm_data, lng_data):
    """生成纯文本简报（用于企业微信/飞书推送）"""
    brent = oil_data.get("brent") or 95.31
    wti = oil_data.get("wti") or 94.59
    brent_chg = oil_data.get("brent_change") or 2.6
    wti_chg = oil_data.get("wti_change") or 2.7
    hh_price = hh_data.get("price") or 3.079
    hh_chg = hh_data.get("change_pct") or -0.52
    jkm = jkm_data.get("price") or 19.04
    lng_domestic = lng_data.get("domestic_avg") or 5963
    lng_terminal = lng_data.get("terminal_avg") or 6780
    
    text = f"""⚡ 能源市场日报 | {report_date}
━━━━━━━━━━━━━━━━━━━━
🛢️ 国际原油
  布伦特: {brent:.2f} 美元/桶 (▲{brent_chg}%)
  WTI:    {wti:.2f} 美元/桶 (▲{wti_chg}%)

🔥 国际天然气
  Henry Hub:  {hh_price:.3f} 美元/MMBtu ({hh_chg:+.2f}%)
  JKM东北亚:  {jkm:.2f} 美元/MMBtu (高位震荡)
  TTF欧洲:    58.50 欧元/兆瓦时

🏭 国内LNG
  国产液厂均价:  {lng_domestic:,} 元/吨
  接收站均价:    {lng_terminal:,} 元/吨

🌍 地缘政治
  🚨 美军5/28再袭伊朗目标，科威特拉响防空警报
  ⚠ 霍尔木兹海峡局势反复，美伊「边打边谈」

📊 策略建议
  1. 最大化管道气合同量，减少高价LNG现货依赖
  2. 加快储气库注气，锁定冬季保供成本
  3. 关注非居民顺价窗口，避免购销倒挂

━━━━━━━━━━━━━━━━━━━━
数据来源: ICE/NYMEX/LNG物联网/隆众资讯
声明: 仅供内部参考，不构成投资建议
"""
    return text

# ============================================================
# 推送模块
# ============================================================

def push_email(html_content, report_date, pdf_path=None):
    """通过邮件推送日报（HTML正文 + PDF附件）"""
    config = CONFIG["email"]
    if not config["username"] or not config["to_addrs"]:
        print("[WARN] 邮件配置不完整，跳过邮件推送")
        return False
    
    msg = MIMEMultipart("mixed")
    msg["Subject"] = f"⚡ 能源市场日报 | {report_date}"
    msg["From"] = config["from_addr"] or config["username"]
    msg["To"] = ", ".join(config["to_addrs"])
    
    # HTML正文 + 纯文本后备
    alt = MIMEMultipart("alternative")
    text_content = f"能源市场日报 {report_date}——请使用支持HTML的邮件客户端查看完整报告，PDF附件见下方。"
    alt.attach(MIMEText(text_content, "plain", "utf-8"))
    alt.attach(MIMEText(html_content, "html", "utf-8"))
    msg.attach(alt)
    
    # PDF附件
    if pdf_path and os.path.exists(pdf_path):
        with open(pdf_path, "rb") as f:
            pdf_attachment = MIMEText(f.read(), "base64", "utf-8")
            pdf_attachment["Content-Type"] = "application/pdf"
            pdf_attachment["Content-Disposition"] = f'attachment; filename="能源市场日报_{report_date}.pdf"'
            msg.attach(pdf_attachment)
    
    try:
        if config["smtp_port"] == 465:
            server = smtplib.SMTP_SSL(config["smtp_host"], config["smtp_port"], timeout=30)
        else:
            server = smtplib.SMTP(config["smtp_host"], config["smtp_port"], timeout=30)
            server.starttls()
        server.login(config["username"], config["password"])
        server.sendmail(msg["From"], config["to_addrs"], msg.as_string())
        server.quit()
        print(f"[OK] 邮件已发送至 {config['to_addrs']}")
        return True
    except Exception as e:
        print(f"[ERROR] 邮件发送失败: {e}")
        return False

def push_wecom(text_content):
    """通过企业微信机器人webhook推送"""
    webhook = CONFIG["wecom_webhook"]
    if not webhook:
        print("[WARN] 企业微信webhook未配置，跳过推送")
        return False
    
    payload = json.dumps({
        "msgtype": "text",
        "text": {
            "content": text_content,
        }
    }).encode("utf-8")
    
    try:
        req = urllib.request.Request(webhook, data=payload, headers={"Content-Type": "application/json"})
        ctx = create_ssl_context()
        resp = urllib.request.urlopen(req, timeout=10, context=ctx)
        result = json.loads(resp.read().decode("utf-8"))
        if result.get("errcode") == 0:
            print("[OK] 企业微信推送成功")
            return True
        else:
            print(f"[ERROR] 企业微信推送失败: {result}")
            return False
    except Exception as e:
        print(f"[ERROR] 企业微信推送异常: {e}")
        return False

def push_feishu(text_content):
    """通过飞书机器人webhook推送"""
    webhook = CONFIG["feishu_webhook"]
    if not webhook:
        print("[WARN] 飞书webhook未配置，跳过推送")
        return False
    
    payload = json.dumps({
        "msg_type": "text",
        "content": {
            "text": text_content,
        }
    }).encode("utf-8")
    
    try:
        req = urllib.request.Request(webhook, data=payload, headers={"Content-Type": "application/json"})
        ctx = create_ssl_context()
        resp = urllib.request.urlopen(req, timeout=10, context=ctx)
        result = json.loads(resp.read().decode("utf-8"))
        if result.get("code") == 0:
            print("[OK] 飞书推送成功")
            return True
        else:
            print(f"[ERROR] 飞书推送失败: {result}")
            return False
    except Exception as e:
        print(f"[ERROR] 飞书推送异常: {e}")
        return False

# ============================================================
# 主流程
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="能源市场日报自动生成与推送")
    parser.add_argument("--push", choices=["email", "wecom", "feishu", "all"], default=None,
                        help="推送渠道 (默认仅生成本地文件)")
    parser.add_argument("--date", default=None, help="报告日期 (YYYY-MM-DD)，默认为今天")
    parser.add_argument("--output", default=None, help="输出文件路径")
    args = parser.parse_args()
    
    # 确定报告日期
    if args.date:
        report_date = args.date
    else:
        report_date = datetime.now().strftime("%Y-%m-%d")
    
    print("=" * 60)
    print(f"  能源市场日报自动生成系统")
    print(f"  报告日期: {report_date}")
    print(f"  执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    
    # 1. 采集数据
    print("\n[1/4] 采集数据中...")
    oil_data = fetch_oil_prices()
    print(f"  国际油价: 布伦特={oil_data.get('brent')}, WTI={oil_data.get('wti')} (来源: {oil_data['source']})")

    hh_data = fetch_henry_hub()
    print(f"  Henry Hub: {hh_data.get('price')} (来源: {hh_data['source']})")

    ttf_data = fetch_ttf_price()
    print(f"  TTF天然气: {ttf_data.get('price')} (来源: {ttf_data['source']})")

    fx_data = fetch_usdcny_rate()
    print(f"  USD/CNY汇率: {fx_data.get('rate')} (来源: {fx_data['source']})")

    jkm_data = fetch_jkm_price()
    jkm_extra = f" ({jkm_data.get('date')})" if jkm_data.get("date") else ""
    print(f"  JKM现货: {jkm_data.get('price')}{jkm_extra} (来源: {jkm_data['source']})")
    
    lng_data = fetch_lng_prices()
    print(f"  国内LNG: 国产={lng_data.get('domestic_avg')}, 接收站={lng_data.get('terminal_avg')} (来源: {lng_data['source']})")
    if lng_data.get("benchmark"):
        print(f"  生意社LNG基准价: {lng_data['benchmark']} 参考价: {lng_data.get('reference')} ({lng_data.get('ref_change_pct', 0):+.2f}%)")
    if lng_data.get("market_comment"):
        print(f"  市场评述: {lng_data['market_comment'][:60]}...")
    
    # 传递SHPGX数据给管道气函数
    shpgx_data = lng_data.get("shpgx", {})
    pipe_data = fetch_pipeline_gas_prices(shpgx_data)
    shpgx_note = ""
    if shpgx_data.get("lng_factory_price") and not shpgx_data.get("source", "").endswith("stale"):
        shpgx_note = f" + SHPGX指数(LNG出厂={shpgx_data['lng_factory_price']}, 出站={shpgx_data.get('lng_terminal_price', '—')})"
    print(f"  管道气门站价: 已加载{len(pipe_data.get('provinces', {}))}省份数据{shpgx_note}")
    
    news_data = fetch_geopolitical_news()
    print(f"  地缘新闻: 已采集{len(news_data)}条")
    
    insights_data = fetch_market_insights(oil_data, hh_data, ttf_data, jkm_data, lng_data)
    print(f"  市场洞察: 已加载{len(insights_data)}板块")
    
    # 2. 生成报告
    print("\n[2/4] 生成HTML报告...")
    html_content = generate_html_report(report_date, oil_data, hh_data, jkm_data, lng_data, pipe_data, news_data, insights_data, ttf_data, fx_data)
    
    # 3. 保存HTML + 生成PDF
    print("\n[3/4] 保存报告 & 生成PDF...")
    output_dir = CONFIG["output_dir"]
    os.makedirs(output_dir, exist_ok=True)
    
    date_tag = report_date.replace("-", "")
    
    if args.output:
        html_path = args.output
    else:
        html_path = os.path.join(output_dir, f"energy_daily_report_{date_tag}.html")
    
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"  HTML报告已保存: {html_path}")
    
    # 生成PDF
    pdf_path = os.path.join(output_dir, f"energy_daily_report_{date_tag}.pdf")
    try:
        from weasyprint import HTML as WHTML
        WHTML(string=html_content).write_pdf(pdf_path)
        print(f"  PDF报告已生成: {pdf_path}")
    except Exception as e:
        print(f"  [WARN] PDF生成失败: {e}")
        pdf_path = None
    
    # 保存最新版
    latest_html = os.path.join(output_dir, "energy_daily_report_latest.html")
    latest_pdf = os.path.join(output_dir, "energy_daily_report_latest.pdf")
    with open(latest_html, "w", encoding="utf-8") as f:
        f.write(html_content)
    if pdf_path and os.path.exists(pdf_path):
        import shutil
        shutil.copy(pdf_path, latest_pdf)
    print(f"  最新版已更新: {latest_html}, {latest_pdf}")
    
    # 4. 推送
    push_channel = args.push
    if push_channel:
        print(f"\n[4/4] 推送报告 (渠道: {push_channel})...")
        text_summary = generate_text_summary(report_date, oil_data, hh_data, jkm_data, lng_data)
        
        push_results = []
        if push_channel in ("email", "all"):
            push_results.append(("邮件", push_email(html_content, report_date, pdf_path)))
        if push_channel in ("wecom", "all"):
            push_results.append(("企业微信", push_wecom(text_summary)))
        if push_channel in ("feishu", "all"):
            push_results.append(("飞书", push_feishu(text_summary)))
        
        for channel, success in push_results:
            status = "✅" if success else "❌"
            print(f"  {status} {channel}: {'成功' if success else '失败'}")
    else:
        print("\n[4/4] 跳过推送 (未指定推送渠道)")
    
    print("\n" + "=" * 60)
    print(f"  ✅ 日报生成完成！")
    print(f"  📄 报告路径: {html_path}")
    print("=" * 60)
    
    return html_path

if __name__ == "__main__":
    main()
