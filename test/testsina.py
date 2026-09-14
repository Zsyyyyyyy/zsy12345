# python3 -m pip install requests
# 新浪行情：http://hq.sinajs.cn/list=<code1>,<code2>,...  一次请求批量取，绝不逐个品种发请求
# 返回体是 GBK 编码的 JS 文本，形如： var hq_str_<code>="字段1,字段2,...";
# 运行：./venv/bin/python test/testsina.py
import os

import requests

os.environ["no_proxy"] = "*"          # 走系统代理会 ProxyError，直接绕开

SINA_HEADERS = {
    "Referer": "https://finance.sina.com.cn",   # 不带 Referer 会被 403
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}

CODES = [
    "nf_RB2701",   # 国内期货·具体合约
    "nf_RB0",      # 国内期货·主力连续
    "nf_CU0",      # 国内期货·主力连续
    "s_sh000001",  # 上证指数（简版）
    "hf_CL",       # 外盘期货（纽约原油）
]

# ---- 国内期货 nf_ 字段名（44 个字段，前 18 个有效，其余为预留/扩展位）----
NF_FIELDS = [
    "合约名称", "时间(HHMMSS)", "开盘价", "最高价", "最低价", "昨收盘",
    "买价", "卖价", "最新价", "结算价", "昨结算价", "买量", "卖量",
    "持仓量", "成交量", "交易所", "品种", "日期",
]
# 后段有效位（其余为 0）
NF_EXTRA = {27: "均价"}

# ---- 指数 s_ 字段名（6 个）----
S_FIELDS = ["名称", "现价", "涨跌额", "涨跌幅(%)", "成交量(手)", "成交额(万元)"]

# ---- 外盘期货 hf_ 字段名（15 个）----
HF_FIELDS = [
    "最新价", "-", "买价", "卖价", "最高价", "最低价", "时间(HH:MM:SS)",
    "昨结算价", "开盘价", "-", "-", "-", "日期", "名称", "-",
]


def field_name(code: str, i: int) -> str:
    if code.startswith("nf_"):
        return NF_FIELDS[i] if i < len(NF_FIELDS) else NF_EXTRA.get(i, "")
    if code.startswith("s_"):
        return S_FIELDS[i] if i < len(S_FIELDS) else ""
    if code.startswith("hf_"):
        return HF_FIELDS[i] if i < len(HF_FIELDS) else ""
    return ""


def sina_quotes(codes):
    url = "http://hq.sinajs.cn/list=" + ",".join(codes)
    r = requests.get(url, headers=SINA_HEADERS, timeout=10,
                     proxies={"http": None, "https": None})
    r.encoding = "gbk"
    raw = r.text
    out = {}
    for line in raw.strip().splitlines():
        if "=" not in line:
            continue
        key = line.split("hq_str_", 1)[1].split("=", 1)[0]
        val = line.split('"', 1)[1].rsplit('"', 1)[0]
        out[key] = val.split(",") if val else []
    return out, raw


def pct(cur, base):
    return round((cur - base) / base * 100, 2) if base else 0.0


q, raw = sina_quotes(CODES)

print("=" * 90)
print("请求 URL : http://hq.sinajs.cn/list=" + ",".join(CODES))
print("=" * 90)

# ---------- 1. 原始文本 ----------
print(f"\n【1】原始响应文本（GBK 解码后 {len(raw)} 字符）")
print("-" * 90)
print(raw.rstrip())
print("-" * 90)

# ---------- 2. 逐 code 全字段 ----------
print("\n【2】逐 code 全字段明细")
for code in CODES:
    fields = q.get(code)
    print("\n" + "=" * 90)
    if not fields:
        print(f"  {code} → 空响应（0 字段），通常表示：code 写错 / 该合约不存在 / 未上市")
        continue
    print(f"  {code} → 共 {len(fields)} 个字段")
    print("-" * 90)
    for i, v in enumerate(fields):
        name = field_name(code, i)
        show = v if v != "" else "(空)"
        print(f"   [{i:>2}] {name:<12} = {show}")

# ---------- 3. 关键字段速览 ----------
print("\n" + "=" * 90)
print("【3】关键字段速览")
print("-" * 90)

if q.get("nf_RB2701"):
    rb = q["nf_RB2701"]
    print(f"  螺纹钢 nf_RB2701 : 最新={rb[8]}  昨结={rb[10]}  "
          f"涨跌={pct(float(rb[8]), float(rb[10])):+}%  "
          f"开={rb[2]} 高={rb[3]} 低={rb[4]}  持仓={rb[13]} 量={rb[14]}  "
          f"{rb[17]} {rb[1]}")

if q.get("s_sh000001"):
    idx = q["s_sh000001"]
    print(f"  上证指数 s_sh000001 : 现价={idx[1]}  涨跌额={idx[2]}  "
          f"涨跌幅={idx[3]}%  量={idx[4]}  额={idx[5]}")

if q.get("hf_CL"):
    cl = q["hf_CL"]
    print(f"  纽约原油 hf_CL : 最新={cl[0]}  昨结={cl[7]}  开={cl[8]}  "
          f"高={cl[4]} 低={cl[5]}  涨跌={pct(float(cl[0]), float(cl[7])):+}%  "
          f"{cl[12]} {cl[6]}")

# ---------- 4. 字段格式结论 ----------
print("\n" + "=" * 90)
print("【4】字段格式结论")
print("-" * 90)
print("""  · nf_ 国内期货：44 个字段，下标 0~18 有效，19~26 恒空，27=均价，28+ 全 0（预留）
      [0]名称 [1]时间HHMMSS [2]开 [3]高 [4]低 [5]昨收盘(常为0)
      [6]买价 [7]卖价 [8]最新 [9]结算(盘中为0) [10]昨结算 ← 涨跌基准
      [11]买量 [12]卖量 [13]持仓量 [14]成交量 [15]交易所 [16]品种 [17]日期 [18]标志位
  · s_ 指数简版：6 个字段 [0]名称 [1]现价 [2]涨跌额 [3]涨跌幅% [4]量 [5]额
      （要完整字段用 sh000001 非简版前缀，返回 30+ 字段）
  · hf_ 外盘：15 个字段 [0]最新 [2]买 [3]卖 [4]高 [5]低 [6]时间 [7]昨结 [8]开
      [12]日期 [13]名称 —— 注意与 nf_ 顺序完全不同
  · 编码必须 gbk；必须带 Referer: https://finance.sina.com.cn；分割符为英文逗号；空字段为连续逗号""")

# 空字段位置提示
empty_idx = [i for i, v in enumerate(q.get("nf_RB2701", [])) if v == ""]
print(f"\n  nf_RB2701 中空字段下标：{empty_idx}")
