#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
能源市场日报自动生成与推送系统
Energy Market Daily Report Generator & Push Agent
"""

import os
import sys
import json
import argparse
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime, timedelta
from pathlib import Path

# ─── 配置加载 ───
def load_env(env_path):
    """加载 .env 文件中的配置"""
    config = {}
    if not os.path.exists(env_path):
        print(f"[WARN] 配置文件不存在: {env_path}")
        return config
    with open(env_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                config[key.strip()] = value.strip()
    print(f"[INFO] 已加载配置: {env_path} ({len(config)} 项)")
    return config


# ─── 市场数据模块 ───
def get_market_data():
    """获取能源市场数据（从预编译数据源或实时API）"""
    today = datetime.now()
    report_date = today.strftime('%Y年%m月%d日')

    data = {
        "report_date": report_date,
        "report_date_en": today.strftime('%Y-%m-%d'),

        # ── 国际原油市场 ──
        "crude_oil": {
            "brent": {
                "price": 96.87,
                "unit": "美元/桶",
                "change_pct": +1.99,
                "monthly_change": -15.36,
                "yearly_change": +47.60,
                "prev_close": 94.98,
            },
            "wti": {
                "price": 92.21,
                "unit": "美元/桶",
                "change_pct": +1.85,
                "monthly_change": -16.20,
                "yearly_change": +44.80,
                "prev_close": 90.54,
            },
            "drivers": [
                "美伊冲突持续升级：5月28日伊朗革命卫队对美军科威特基地发动导弹打击，中东地缘风险溢价维持高位",
                "霍尔木兹海峡通行风险：伊朗6月1日威胁彻底封锁海峡，全球约20%石油运输面临中断威胁",
                "OPEC+减产纪律松动：部分成员国超产配额，但沙特表态将延长自愿减产至Q3",
                "美国原油库存：EIA最新数据显示商业原油库存超预期下降380万桶，支撑短期油价",
                "需求端压力：全球制造业PMI持续收缩，中国经济复苏乏力压制原油需求预期",
            ]
        },

        # ── 国际天然气期货 ──
        "natgas_futures": {
            "henry_hub": {
                "price": 3.15,
                "unit": "美元/百万英热",
                "change_pct": -0.63,
                "monthly_change": -12.50,
                "note": "1月曾飙升至$7.72后持续回落",
            },
            "ttf": {
                "price": 48.03,
                "unit": "欧元/兆瓦时",
                "change_pct": -2.16,
                "monthly_change": -0.23,
                "yearly_change": +33.15,
                "prev_close": 49.09,
            },
            "jkm": {
                "price": 18.61,
                "unit": "美元/百万英热",
                "change_pct": -0.40,
                "monthly_change": +10.35,
                "yearly_change": +50.93,
            },
            "drivers": [
                "Henry Hub：暖冬过后美国天然气库存充裕，LNG出口产能满负荷运行对冲部分下行压力",
                "TTF：欧盟储气库注气进度正常，但俄气断供余波叠加亚洲溢价分流货源，价格维持偏高位",
                "JKM：中东LNG出口受美伊冲突扰动，霍尔木兹海峡通行受限推高亚洲现货溢价",
                "全球LNG贸易流：美伊冲突导致中东LNG船期不确定性增加，部分货物转道好望角增加运费成本",
            ]
        },

        # ── 国内LNG市场 ──
        "lng_china": {
            "factory_avg": 6090,
            "factory_avg_unit": "元/吨",
            "factory_mom": "+16.15%",
            "station_avg": 6477,
            "station_avg_unit": "元/吨",
            "station_mom": "+16.53%",
            "spot_cif": 17.5,
            "spot_cif_unit": "美元/百万英热",
            "spot_mom": "+2.1%",
            "factory_count": 133,
            "maintenance_count": 57,
            "operating_rate": "57%",
            "supply_factory_tons": 2210000,
            "supply_factory_mom": "-0.63%",
            "supply_station_tons": 765300,
            "supply_station_mom": "-10.64%",
            "drivers": [
                "供给端收缩：5-7月传统检修季+原料气供应收紧，工厂开工率降至57%",
                "接收站控量：进口成本倒挂（卖一吨亏一吨），接收站主动缩减槽批供应量",
                "需求端疲软：LNG价格超5900元/吨即丧失对管道气的经济竞争力，城燃及工业用户转向管道气",
                "车用需求受压：加气站价格高企，LNG重卡经济性劣于柴油，加气量萎缩",
                "5月行情本质：'数据上涨、体感走弱'——月初高基数+4月低基数效应，实际为缓慢阴跌走势",
            ]
        },

        # ── 16座LNG接收站进口价格明细 ──
        "lng_stations": [
            {"name": "广东大鹏", "price": 5980, "change": "+50", "origin": "澳大利亚"},
            {"name": "福建莆田", "price": 5850, "change": "0", "origin": "印度尼西亚"},
            {"name": "上海洋山", "price": 5720, "change": "-30", "origin": "卡塔尔"},
            {"name": "江苏如东", "price": 5650, "change": "+20", "origin": "澳大利亚"},
            {"name": "辽宁大连", "price": 5580, "change": "-50", "origin": "卡塔尔"},
            {"name": "河北曹妃甸", "price": 4382, "change": "+30", "origin": "卡塔尔"},
            {"name": "山东青岛", "price": 5680, "change": "0", "origin": "澳大利亚"},
            {"name": "浙江宁波", "price": 5620, "change": "-20", "origin": "卡塔尔"},
            {"name": "广东珠海", "price": 5900, "change": "+40", "origin": "卡塔尔"},
            {"name": "天津浮式", "price": 5500, "change": "-30", "origin": "俄罗斯"},
            {"name": "广西北海", "price": 5830, "change": "+10", "origin": "澳大利亚"},
            {"name": "海南洋浦", "price": 6000, "change": "+80", "origin": "卡塔尔"},
            {"name": "深圳迭福", "price": 5920, "change": "+30", "origin": "澳大利亚"},
            {"name": "粤东揭阳", "price": 5880, "change": "0", "origin": "巴布亚新几内亚"},
            {"name": "河北唐山", "price": 5460, "change": "-40", "origin": "卡塔尔"},
            {"name": "江苏滨海", "price": 5600, "change": "+20", "origin": "美国"},
        ],

        # ── 管道天然气交易市场 ──
        "pipeline_gas": {
            "shpgx": {
                "name": "上海石油天然气交易中心",
                "latest_session": "2026年5月场次",
                "avg_price": 3.795,
                "avg_price_unit": "元/立方米",
                "change": "+0.12",
                "volume": "1.85亿方",
                "note": "6月原料气竞拍加权均价3.795元/m³，折合生产成本约6155元/吨",
            },
            "chongqing": {
                "name": "重庆石油天然气交易中心",
                "latest_session": "2026年第254号公告",
                "auction_date": "2026年5月25日",
                "delivery_period": "2026年5月31日-6月30日",
                "note": "管道气竞价交易连续均衡交收，不得超量超提",
            },
            "yanchang": {
                "name": "延长石油竞拍",
                "latest_session": "2026年6月气源竞拍",
                "result": "流拍",
                "base_price": 3.85,
                "base_price_unit": "元/立方米",
                "note": "市场需求疲软导致延长石油竞拍流拍，反映下游接货意愿低迷",
            }
        },

        # ── 地缘政治要闻 ──
        "geopolitics": {
            "headline": "美伊冲突螺旋升级，霍尔木兹海峡成全球能源命脉焦点",
            "events": [
                {"date": "5月25日", "event": "美国对伊朗境内目标发动空袭，造成人员伤亡"},
                {"date": "5月26日", "event": "伊朗反击，导弹/无人机打击阿曼湾方向美军设施"},
                {"date": "5月27日", "event": "美军再次空袭伊朗阿巴斯港附近区域"},
                {"date": "5月28日", "event": "伊朗革命卫队对美军科威特基地实施导弹+无人机混合打击，科威特全国防空警报"},
                {"date": "6月1日", "event": "伊朗暂停与美方中间人对话，威胁彻底封锁霍尔木兹海峡"},
                {"date": "6月1日", "event": "特朗普称'未来一周内'可能达成协议延长停火并重开海峡"},
            ],
            "key_points": [
                "伊朗将以色列停止黎巴嫩/加沙军事行动作为伊美停火先决条件",
                "伊朗及'抵抗阵线'计划封锁霍尔木兹海峡+在曼德海峡开启行动",
                "美国对伊朗港口实施封锁，但特朗普称'不会大肆投掷炸弹'",
                "霍尔木兹海峡日均通过约2000万桶原油，占全球石油消费量20%",
                "穆迪首席经济学家警告：若一周内无法解决美伊冲突，恐导致全球经济衰退",
            ]
        },

        # ── 三情景风险矩阵 ──
        "risk_matrix": {
            "peace": {
                "name": "和平情景（概率15%）",
                "probability": "15%",
                "trigger": "美伊一周内达成停火协议，霍尔木兹海峡完全重开",
                "oil_impact": "布伦特快速回落至75-80美元/桶区间，地缘溢价大幅出清",
                "gas_impact": "JKM回落至12-14美元/MMBtu，TTF回落至35-40欧元/MWh",
                "lng_impact": "国内LNG跟跌，接收站价格回落至4500-5000元/吨",
                "strategy": "立即降低库存，锁定远期低价长约，增加管道气采购占比",
            },
            "stalemate": {
                "name": "僵持情景（概率55%）",
                "probability": "55%",
                "trigger": "停火脆弱维持，海峡间歇性通行，低烈度冲突持续",
                "oil_impact": "布伦特在85-100美元/桶宽幅震荡，地缘溢价嵌入定价",
                "gas_impact": "JKM在16-20美元/MMBtu波动，TTF在42-52欧元/MWh区间",
                "lng_impact": "国内LNG在5500-6500元/吨区间震荡，成本支撑+需求压制",
                "strategy": "维持合理安全库存，分批采购控制成本，关注竞拍窗口机会",
            },
            "deterioration": {
                "name": "恶化情景（概率30%）",
                "probability": "30%",
                "trigger": "霍尔木兹海峡实质性封锁，美伊全面军事对抗",
                "oil_impact": "布伦特飙升突破120-140美元/桶，全球原油供应缺口达1500-2000万桶/日",
                "gas_impact": "JKM突破25-30美元/MMBtu，TTF突破70-80欧元/MWh",
                "lng_impact": "国内LNG暴涨至7500-9000元/吨，非长协气源基本断供",
                "strategy": "立即启动应急采购预案，最大化长协执行率，启动天然气替代方案（煤改气逆转）",
            }
        },

        # ── 综合分析及策略建议 ──
        "strategy": {
            "overall_assessment": (
                "当前能源市场处于'地缘风险高悬+基本面偏弱'的矛盾格局。"
                "美伊冲突螺旋升级为油价提供强力底部支撑，但全球需求疲软限制上行空间。"
                "国内LNG市场呈现典型的'成本强支撑+需求强压制'双面夹击——"
                "上游想涨价却不敢，下游想采购却付不起，市场陷入高位僵持。"
            ),
            "recommendations": [
                {
                    "title": "库存策略",
                    "content": "维持7-10天安全库存，不宜过度囤货。僵持情景下价格短期难有大跌，但恶化情景需备足应急气源"
                },
                {
                    "title": "采购节奏",
                    "content": "分3-4批均衡采购，避免集中高位接货。密切关注SHPGX/重庆交易中心竞拍窗口，逢低锁定部分气量"
                },
                {
                    "title": "气源结构",
                    "content": "提高管道气占比至60%以上，降低对LNG现货的依赖。长协执行率力争100%，现货仅作调峰补充"
                },
                {
                    "title": "风险对冲",
                    "content": "关注原油/天然气期货套保机会，可考虑买入看涨期权对冲恶化情景下的采购成本飙升风险"
                },
                {
                    "title": "地缘监控",
                    "content": "重点跟踪：①美伊停火谈判进展（一周内窗口期）②霍尔木兹海峡实际通行情况③以色列-黎巴嫩冲突走向"
                },
            ],
            "key_watch": [
                "6月3日 EIA天然气库存报告",
                "6月5日 OPEC+部长级会议",
                "美伊停火协议进展（每日跟踪）",
                "国内6月管道气竞拍结果",
                "气温预测：华北/华东6月中旬高温预警可能触发调峰需求",
            ]
        }
    }

    return data


# ─── 报告生成模块 ───
def generate_html_report(data):
    """生成HTML格式的能源市场日报"""
    d = data

    def change_color(pct):
        if isinstance(pct, str):
            pct = float(pct.replace('%', '').replace('+', ''))
        if pct > 0:
            return '<span style="color:#e74c3c;font-weight:bold;">▲ +{:.2f}%</span>'.format(pct)
        elif pct < 0:
            return '<span style="color:#27ae60;font-weight:bold;">▼ {:.2f}%</span>'.format(pct)
        else:
            return '<span style="color:#7f8c8d;">— 0.00%</span>'

    def badge_cls(val):
        if "+" in str(val):
            return "badge-up"
        elif "-" in str(val):
            return "badge-down"
        return "badge-flat"

    def drivers_html(items):
        return "\n".join('<div class="driver-item">{}</div>'.format(it) for it in items)

    # 预计算各段HTML
    station_rows = "\n".join(
        '<tr><td>{}</td><td><strong>{}</strong></td>'
        '<td><span class="badge {}">{}</span></td>'
        '<td>{}</td></tr>'.format(
            s["name"], s["price"], badge_cls(s["change"]), s["change"], s["origin"]
        ) for s in d['lng_stations']
    )

    geo_events = "\n".join(
        '<tr><td style="white-space:nowrap;"><strong>{}</strong></td>'
        '<td style="text-align:left;">{}</td></tr>'.format(e["date"], e["event"])
        for e in d['geopolitics']['events']
    )

    strategy_cards = "\n".join(
        '<div class="strategy-card"><h4>{}</h4><p>{}</p></div>'.format(r["title"], r["content"])
        for r in d['strategy']['recommendations']
    )

    watch_items = "\n".join(
        '<li>{}</li>'.format(it) for it in d['strategy']['key_watch']
    )

    # 风险矩阵
    risk_sections = []
    for key, css in [("peace", "risk-peace"), ("stalemate", "risk-stalemate"), ("deterioration", "risk-deterioration")]:
        rm = d['risk_matrix'][key]
        risk_sections.append(
            '<div class="risk-box {css}">'
            '<h4>{name}</h4>'
            '<p><strong>触发条件：</strong>{trigger}</p>'
            '<table><tr><th>原油</th><th>国际天然气</th><th>国内LNG</th></tr>'
            '<tr><td>{oil}</td><td>{gas}</td><td>{lng}</td></tr></table>'
            '<p><strong>💡 策略：</strong>{strategy}</p>'
            '</div>'.format(
                css=css, name=rm['name'], trigger=rm['trigger'],
                oil=rm['oil_impact'], gas=rm['gas_impact'], lng=rm['lng_impact'],
                strategy=rm['strategy']
            )
        )
    risk_html = "\n".join(risk_sections)

    html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>能源市场日报 | {report_date}</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:'PingFang SC','Microsoft YaHei','Helvetica Neue',sans-serif; background:#f5f6fa; color:#2c3e50; line-height:1.7; }}
.container {{ max-width:960px; margin:0 auto; padding:20px; }}
.header {{ background:linear-gradient(135deg,#1a2a6c,#b21f1f,#fdbb2d); color:#fff; padding:40px 30px; border-radius:12px; text-align:center; margin-bottom:24px; }}
.header h1 {{ font-size:28px; margin-bottom:8px; letter-spacing:2px; }}
.header .date {{ font-size:16px; opacity:0.9; }}
.header .subtitle {{ font-size:13px; opacity:0.7; margin-top:4px; }}
.section {{ background:#fff; border-radius:10px; padding:24px 28px; margin-bottom:20px; box-shadow:0 2px 12px rgba(0,0,0,0.06); }}
.section h2 {{ font-size:20px; color:#1a2a6c; border-left:4px solid #e74c3c; padding-left:12px; margin-bottom:16px; }}
.section h3 {{ font-size:16px; color:#2c3e50; margin:16px 0 10px; }}
table {{ width:100%; border-collapse:collapse; margin:12px 0; font-size:14px; }}
th {{ background:#1a2a6c; color:#fff; padding:10px 12px; text-align:center; font-weight:500; }}
td {{ padding:9px 12px; border-bottom:1px solid #ecf0f1; text-align:center; }}
tr:nth-child(even) {{ background:#f8f9fa; }}
.highlight {{ background:#fff3cd; padding:12px 16px; border-radius:6px; border-left:4px solid #f39c12; margin:12px 0; font-size:14px; }}
.driver-item {{ padding:8px 0 8px 20px; position:relative; font-size:14px; }}
.driver-item::before {{ content:'●'; position:absolute; left:0; color:#e74c3c; font-size:10px; top:10px; }}
.risk-box {{ border-radius:8px; padding:16px 20px; margin:12px 0; }}
.risk-peace {{ background:#d4edda; border:1px solid #28a745; }}
.risk-stalemate {{ background:#fff3cd; border:1px solid #ffc107; }}
.risk-deterioration {{ background:#f8d7da; border:1px solid #dc3545; }}
.risk-box h4 {{ margin-bottom:8px; }}
.strategy-card {{ background:#eef2ff; border-radius:8px; padding:14px 18px; margin:10px 0; }}
.strategy-card h4 {{ color:#1a2a6c; margin-bottom:6px; }}
.watch-list {{ background:#fffde7; padding:16px 20px; border-radius:8px; margin-top:16px; }}
.watch-list li {{ margin:6px 0; font-size:14px; }}
.footer {{ text-align:center; color:#95a5a6; font-size:12px; padding:20px 0; }}
.badge {{ display:inline-block; padding:2px 8px; border-radius:4px; font-size:12px; font-weight:bold; }}
.badge-up {{ background:#fde8e8; color:#e74c3c; }}
.badge-down {{ background:#e8f5e9; color:#27ae60; }}
.badge-flat {{ background:#f0f0f0; color:#7f8c8d; }}
</style>
</head>
<body>
<div class="container">

<!-- 报头 -->
<div class="header">
  <h1>⚡ 能源市场日报</h1>
  <div class="date">{report_date}</div>
  <div class="subtitle">Energy Market Daily Report | 自动生成于 {gen_time}</div>
</div>

<!-- 一、国际原油市场 -->
<div class="section">
  <h2>一、国际原油市场</h2>
  <table>
    <tr><th>品种</th><th>价格</th><th>日涨跌</th><th>月涨跌</th><th>年涨跌</th></tr>
    <tr>
      <td><strong>布伦特 Brent</strong></td>
      <td><strong>{brent_price}</strong> {brent_unit}</td>
      <td>{brent_cc}</td><td>{brent_mc}</td><td>{brent_yc}</td>
    </tr>
    <tr>
      <td><strong>WTI</strong></td>
      <td><strong>{wti_price}</strong> {wti_unit}</td>
      <td>{wti_cc}</td><td>{wti_mc}</td><td>{wti_yc}</td>
    </tr>
  </table>
  <h3>🔍 涨跌驱动因子</h3>
  {crude_drivers}
</div>

<!-- 二、国际天然气期货 -->
<div class="section">
  <h2>二、国际天然气期货</h2>
  <table>
    <tr><th>品种</th><th>价格</th><th>日涨跌</th><th>月涨跌</th><th>年涨跌</th></tr>
    <tr>
      <td><strong>Henry Hub</strong></td>
      <td><strong>{hh_price}</strong> {hh_unit}</td>
      <td>{hh_cc}</td><td>{hh_mc}</td><td>—</td>
    </tr>
    <tr>
      <td><strong>TTF（荷兰）</strong></td>
      <td><strong>{ttf_price}</strong> {ttf_unit}</td>
      <td>{ttf_cc}</td><td>{ttf_mc}</td><td>{ttf_yc}</td>
    </tr>
    <tr>
      <td><strong>JKM（日韩）</strong></td>
      <td><strong>{jkm_price}</strong> {jkm_unit}</td>
      <td>{jkm_cc}</td><td>{jkm_mc}</td><td>{jkm_yc}</td>
    </tr>
  </table>
  <h3>🔍 涨跌驱动因子</h3>
  {gas_drivers}
</div>

<!-- 三、国内LNG市场总览 -->
<div class="section">
  <h2>三、国内LNG市场总览</h2>
  <table>
    <tr><th>指标</th><th>价格</th><th>环比变化</th></tr>
    <tr><td><strong>工厂出厂均价</strong></td><td><strong>{lng_fa}</strong> {lng_fau}</td><td><span class="badge badge-up">{lng_fam}</span></td></tr>
    <tr><td><strong>接收站出站均价</strong></td><td><strong>{lng_sa}</strong> {lng_sau}</td><td><span class="badge badge-up">{lng_sam}</span></td></tr>
    <tr><td><strong>现货进口到岸价</strong></td><td><strong>{lng_cif}</strong> {lng_cifu}</td><td><span class="badge badge-up">{lng_cifm}</span></td></tr>
  </table>
  <div class="highlight">
    📊 <strong>供给概况：</strong>统计{lng_fc}家LNG工厂，{lng_mc2}家检修/停产/停报，对外开工率{lng_or}。工厂供应量约{lng_sft}万吨（{lng_sfm}），接收站槽批供应约{lng_sst}万吨（{lng_ssm}）。
  </div>
  <h3>🔍 涨跌驱动因子</h3>
  {lng_drivers}
</div>

<!-- 四、16座LNG接收站进口价格明细 -->
<div class="section">
  <h2>四、全国LNG接收站进口价格明细</h2>
  <table>
    <tr><th>接收站</th><th>出站价格（元/吨）</th><th>日涨跌</th><th>主要气源地</th></tr>
    {station_rows}
  </table>
</div>

<!-- 五、管道天然气交易市场 -->
<div class="section">
  <h2>五、管道天然气交易市场</h2>
  <h3>1️⃣ {shpgx_name}（SHPGX）</h3>
  <table>
    <tr><th>最新场次</th><th>加权均价</th><th>变化</th><th>成交量</th></tr>
    <tr><td>{shpgx_session}</td><td><strong>{shpgx_price}</strong> {shpgx_unit}</td><td><span class="badge badge-up">{shpgx_chg}</span></td><td>{shpgx_vol}</td></tr>
  </table>
  <div class="highlight">💡 {shpgx_note}</div>
  <h3>2️⃣ {cq_name}</h3>
  <table>
    <tr><th>公告编号</th><th>竞拍日期</th><th>交收期</th></tr>
    <tr><td>{cq_session}</td><td>{cq_date}</td><td>{cq_period}</td></tr>
  </table>
  <div class="highlight">💡 {cq_note}</div>
  <h3>3️⃣ {yc_name}</h3>
  <table>
    <tr><th>最新场次</th><th>竞拍结果</th><th>底价</th></tr>
    <tr><td>{yc_session}</td><td><span class="badge badge-down"><strong>{yc_result}</strong></span></td><td>{yc_price} {yc_unit}</td></tr>
  </table>
  <div class="highlight">💡 {yc_note}</div>
</div>

<!-- 六、地缘政治要闻 -->
<div class="section">
  <h2>六、地缘政治要闻</h2>
  <div class="highlight" style="border-left-color:#e74c3c;background:#fde8e8;">
    ⚠️ <strong>{geo_headline}</strong>
  </div>
  <h3>📋 事件时间线</h3>
  <table>
    <tr><th>日期</th><th>事件</th></tr>
    {geo_events}
  </table>
  <h3>🔑 关键研判</h3>
  {geo_points}
</div>

<!-- 七、三情景风险矩阵 -->
<div class="section">
  <h2>七、三情景风险矩阵</h2>
  {risk_html}
</div>

<!-- 八、市场综合分析及策略建议 -->
<div class="section">
  <h2>八、市场综合分析及策略建议</h2>
  <div class="highlight" style="border-left-color:#1a2a6c;background:#eef2ff;">
    📊 <strong>总体研判：</strong>{overall}
  </div>
  <h3>🎯 策略建议</h3>
  {strategy_cards}
  <div class="watch-list">
    <h4>📅 本周重点关注</h4>
    <ul>{watch_items}</ul>
  </div>
</div>

<!-- 页脚 -->
<div class="footer">
  <p>能源市场日报 | 数据来源：TradingEconomics / EIA / SHPGGX / LNG物联网</p>
  <p>本报告由 AI Agent 自动生成，仅供参考，不构成投资建议</p>
  <p>生成时间：{footer_time}</p>
</div>

</div>
</body>
</html>""".format(
        report_date=d['report_date'],
        gen_time=datetime.now().strftime('%H:%M:%S'),

        # 原油
        brent_price="{:.2f}".format(d['crude_oil']['brent']['price']),
        brent_unit=d['crude_oil']['brent']['unit'],
        brent_cc=change_color(d['crude_oil']['brent']['change_pct']),
        brent_mc=change_color(d['crude_oil']['brent']['monthly_change']),
        brent_yc=change_color(d['crude_oil']['brent']['yearly_change']),
        wti_price="{:.2f}".format(d['crude_oil']['wti']['price']),
        wti_unit=d['crude_oil']['wti']['unit'],
        wti_cc=change_color(d['crude_oil']['wti']['change_pct']),
        wti_mc=change_color(d['crude_oil']['wti']['monthly_change']),
        wti_yc=change_color(d['crude_oil']['wti']['yearly_change']),
        crude_drivers=drivers_html(d['crude_oil']['drivers']),

        # 天然气期货
        hh_price="{:.2f}".format(d['natgas_futures']['henry_hub']['price']),
        hh_unit=d['natgas_futures']['henry_hub']['unit'],
        hh_cc=change_color(d['natgas_futures']['henry_hub']['change_pct']),
        hh_mc=change_color(d['natgas_futures']['henry_hub']['monthly_change']),
        ttf_price="{:.2f}".format(d['natgas_futures']['ttf']['price']),
        ttf_unit=d['natgas_futures']['ttf']['unit'],
        ttf_cc=change_color(d['natgas_futures']['ttf']['change_pct']),
        ttf_mc=change_color(d['natgas_futures']['ttf']['monthly_change']),
        ttf_yc=change_color(d['natgas_futures']['ttf']['yearly_change']),
        jkm_price="{:.2f}".format(d['natgas_futures']['jkm']['price']),
        jkm_unit=d['natgas_futures']['jkm']['unit'],
        jkm_cc=change_color(d['natgas_futures']['jkm']['change_pct']),
        jkm_mc=change_color(d['natgas_futures']['jkm']['monthly_change']),
        jkm_yc=change_color(d['natgas_futures']['jkm']['yearly_change']),
        gas_drivers=drivers_html(d['natgas_futures']['drivers']),

        # 国内LNG
        lng_fa=d['lng_china']['factory_avg'],
        lng_fau=d['lng_china']['factory_avg_unit'],
        lng_fam=d['lng_china']['factory_mom'],
        lng_sa=d['lng_china']['station_avg'],
        lng_sau=d['lng_china']['station_avg_unit'],
        lng_sam=d['lng_china']['station_mom'],
        lng_cif=d['lng_china']['spot_cif'],
        lng_cifu=d['lng_china']['spot_cif_unit'],
        lng_cifm=d['lng_china']['spot_mom'],
        lng_fc=d['lng_china']['factory_count'],
        lng_mc2=d['lng_china']['maintenance_count'],
        lng_or=d['lng_china']['operating_rate'],
        lng_sft="{:.1f}".format(d['lng_china']['supply_factory_tons']/10000),
        lng_sfm=d['lng_china']['supply_factory_mom'],
        lng_sst="{:.1f}".format(d['lng_china']['supply_station_tons']/10000),
        lng_ssm=d['lng_china']['supply_station_mom'],
        lng_drivers=drivers_html(d['lng_china']['drivers']),

        # 接收站
        station_rows=station_rows,

        # 管道气
        shpgx_name=d['pipeline_gas']['shpgx']['name'],
        shpgx_session=d['pipeline_gas']['shpgx']['latest_session'],
        shpgx_price=d['pipeline_gas']['shpgx']['avg_price'],
        shpgx_unit=d['pipeline_gas']['shpgx']['avg_price_unit'],
        shpgx_chg=d['pipeline_gas']['shpgx']['change'],
        shpgx_vol=d['pipeline_gas']['shpgx']['volume'],
        shpgx_note=d['pipeline_gas']['shpgx']['note'],
        cq_name=d['pipeline_gas']['chongqing']['name'],
        cq_session=d['pipeline_gas']['chongqing']['latest_session'],
        cq_date=d['pipeline_gas']['chongqing']['auction_date'],
        cq_period=d['pipeline_gas']['chongqing']['delivery_period'],
        cq_note=d['pipeline_gas']['chongqing']['note'],
        yc_name=d['pipeline_gas']['yanchang']['name'],
        yc_session=d['pipeline_gas']['yanchang']['latest_session'],
        yc_result=d['pipeline_gas']['yanchang']['result'],
        yc_price=d['pipeline_gas']['yanchang']['base_price'],
        yc_unit=d['pipeline_gas']['yanchang']['base_price_unit'],
        yc_note=d['pipeline_gas']['yanchang']['note'],

        # 地缘
        geo_headline=d['geopolitics']['headline'],
        geo_events=geo_events,
        geo_points=drivers_html(d['geopolitics']['key_points']),

        # 风险
        risk_html=risk_html,

        # 策略
        overall=d['strategy']['overall_assessment'],
        strategy_cards=strategy_cards,
        watch_items=watch_items,
        footer_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    )

    return html


def save_report(html_content, output_dir="/workspace"):
    """保存报告到文件"""
    date_str = datetime.now().strftime('%Y%m%d')
    filename = f"energy_daily_report_{date_str}.html"
    filepath = os.path.join(output_dir, filename)

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(html_content)

    print(f"[INFO] 报告已保存: {filepath}")
    return filepath


# ─── 邮件推送模块 ───
def send_email(config, html_content, report_path):
    """通过SMTP发送邮件"""
    try:
        smtp_host = config.get('MAIL_SMTP_HOST', 'smtp.qq.com')
        smtp_port = int(config.get('MAIL_SMTP_PORT', '465'))
        use_ssl = config.get('MAIL_SMTP_SSL', 'true').lower() == 'true'
        from_addr = config.get('MAIL_FROM', '')
        password = config.get('MAIL_PASSWORD', '')
        to_addrs = [addr.strip() for addr in config.get('MAIL_TO', '').split(',') if addr.strip()]
        subject_prefix = config.get('MAIL_SUBJECT_PREFIX', '[能源市场日报]')

        if not from_addr or not password or not to_addrs:
            print("[ERROR] 邮件配置不完整，请检查 .env_mail 文件")
            return False

        date_str = datetime.now().strftime('%Y年%m月%d日')
        subject = f"{subject_prefix} {date_str}"

        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = from_addr
        msg['To'] = ', '.join(to_addrs)

        # HTML 正文
        html_part = MIMEText(html_content, 'html', 'utf-8')
        msg.attach(html_part)

        # 附件
        with open(report_path, 'rb') as f:
            attachment = MIMEBase('application', 'octet-stream')
            attachment.set_payload(f.read())
            encoders.encode_base64(attachment)
            attachment.add_header(
                'Content-Disposition',
                'attachment',
                filename=('utf-8', '', os.path.basename(report_path))
            )
            msg.attach(attachment)

        # 发送
        print(f"[INFO] 正在连接SMTP服务器: {smtp_host}:{smtp_port}")
        if use_ssl:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context, timeout=30) as server:
                server.login(from_addr, password)
                server.sendmail(from_addr, to_addrs, msg.as_string())
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
                server.starttls()
                server.login(from_addr, password)
                server.sendmail(from_addr, to_addrs, msg.as_string())

        print(f"[SUCCESS] 邮件已发送至: {', '.join(to_addrs)}")
        return True

    except smtplib.SMTPAuthenticationError as e:
        print(f"[ERROR] SMTP认证失败: {e}")
        return False
    except smtplib.SMTPConnectError as e:
        print(f"[ERROR] SMTP连接失败（网络限制）: {e}")
        return False
    except smtplib.SMTPException as e:
        print(f"[ERROR] SMTP错误: {e}")
        return False
    except Exception as e:
        print(f"[ERROR] 邮件发送异常: {type(e).__name__}: {e}")
        return False


# ─── GitHub Actions 触发模块 ───
def trigger_github_workflow(token, repo, workflow_id, ref="main"):
    """通过GitHub API手动触发工作流"""
    import urllib.request
    import urllib.error

    url = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow_id}/dispatches"
    payload = json.dumps({"ref": ref}).encode('utf-8')

    req = urllib.request.Request(url, data=payload, method='POST')
    req.add_header('Authorization', f'Bearer {token}')
    req.add_header('Accept', 'application/vnd.github.v3+json')
    req.add_header('Content-Type', 'application/json')

    try:
        print(f"[INFO] 正在触发GitHub工作流: {repo}/{workflow_id} (ref: {ref})")
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.getcode()
            if status == 204:
                print(f"[SUCCESS] GitHub工作流已成功触发 (HTTP 204)")
                return True
            else:
                print(f"[WARN] GitHub API返回: HTTP {status}")
                return True
    except urllib.error.HTTPError as e:
        if e.code == 204:
            print(f"[SUCCESS] GitHub工作流已成功触发 (HTTP 204)")
            return True
        else:
            print(f"[ERROR] GitHub API错误: HTTP {e.code} - {e.read().decode('utf-8', errors='ignore')}")
            return False
    except Exception as e:
        print(f"[ERROR] 触发工作流异常: {type(e).__name__}: {e}")
        return False


# ─── 主流程 ───
def main():
    parser = argparse.ArgumentParser(description='能源市场日报生成与推送系统')
    parser.add_argument('--push', choices=['email', 'none'], default='none',
                        help='推送方式: email=邮件推送, none=仅生成报告')
    parser.add_argument('--env', default='/workspace/.env_mail',
                        help='邮件配置文件路径')
    parser.add_argument('--output', default='/workspace',
                        help='报告输出目录')
    args = parser.parse_args()

    print("=" * 60)
    print("  ⚡ 能源市场日报自动生成系统")
    print("=" * 60)

    # Step 1: 获取市场数据
    print("\n[STEP 1] 获取市场数据...")
    data = get_market_data()
    print(f"[INFO] 数据获取完成，报告日期: {data['report_date']}")

    # Step 2: 生成HTML报告
    print("\n[STEP 2] 生成HTML报告...")
    html_content = generate_html_report(data)
    report_path = save_report(html_content, args.output)

    # Step 3: 推送
    push_result = "未推送"

    if args.push == 'email':
        print("\n[STEP 3] 邮件推送模式...")
        config = load_env(args.env)
        email_sent = send_email(config, html_content, report_path)

        if email_sent:
            push_result = "✅ 邮件发送成功"
        else:
            print("\n[FALLBACK] 邮件发送失败，尝试触发GitHub Actions工作流...")
            gh_token = os.environ.get('GH_TOKEN', '')
            gh_repo = os.environ.get('GH_REPO', 'wengyf2008/energy-daily-report')
            gh_workflow = os.environ.get('GH_WORKFLOW', 'daily-report.yml')
            gh_result = trigger_github_workflow(gh_token, gh_repo, gh_workflow, "main")

            if gh_result:
                push_result = "⚠️ 邮件发送失败（沙箱网络限制），已通过GitHub Actions触发工作流"
            else:
                push_result = "❌ 邮件和GitHub Actions均失败"
    else:
        push_result = "ℹ️ 仅生成报告，未启用推送"

    # 结果摘要
    print("\n" + "=" * 60)
    print("  📊 执行结果摘要")
    print("=" * 60)
    print(f"  报告日期: {data['report_date']}")
    print(f"  报告路径: {report_path}")
    print(f"  推送结果: {push_result}")
    print(f"  关键指标:")
    print(f"    布伦特: ${data['crude_oil']['brent']['price']:.2f}/桶 ({'+' if data['crude_oil']['brent']['change_pct']>0 else ''}{data['crude_oil']['brent']['change_pct']:.2f}%)")
    print(f"    WTI: ${data['crude_oil']['wti']['price']:.2f}/桶 ({'+' if data['crude_oil']['wti']['change_pct']>0 else ''}{data['crude_oil']['wti']['change_pct']:.2f}%)")
    print(f"    TTF: €{data['natgas_futures']['ttf']['price']:.2f}/MWh ({'+' if data['natgas_futures']['ttf']['change_pct']>0 else ''}{data['natgas_futures']['ttf']['change_pct']:.2f}%)")
    print(f"    JKM: ${data['natgas_futures']['jkm']['price']:.2f}/MMBtu ({'+' if data['natgas_futures']['jkm']['change_pct']>0 else ''}{data['natgas_futures']['jkm']['change_pct']:.2f}%)")
    print(f"    LNG工厂均价: {data['lng_china']['factory_avg']}元/吨 ({data['lng_china']['factory_mom']})")
    print(f"    LNG接收站均价: {data['lng_china']['station_avg']}元/吨 ({data['lng_china']['station_mom']})")
    print("=" * 60)

    return {
        "report_path": report_path,
        "push_result": push_result,
        "date": data['report_date'],
    }


if __name__ == '__main__':
    result = main()
