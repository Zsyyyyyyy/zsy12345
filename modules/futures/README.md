# 期货行情网页版 · 使用说明

一个零依赖的 Python 本地服务：网页前端 + 服务端代理新浪行情接口（补 Referer、GB18030→UTF-8），并托管「我的持仓」看板。

## 本地跑

```bash
cd web-futures
./start.sh        # 默认端口 8642，访问 http://localhost:8642
./stop.sh         # 关闭
```

也可以直接跑：

```bash
python3 server.py 8642
```

> 改 `config.json`（看盘清单 / 持仓 / 密码）后页面 3 秒内自动同步，无需重启服务。
