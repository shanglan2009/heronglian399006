# 和融联 · 创业板指数监控

Windows 桌面研究工具：监控创业板指数 399006，辅助观察 159915，提供开盘判断、盘中条件提醒、收盘复盘与信号审计。不连接券商，不自动下单，不保证收益。

## 安装与启动

需要 Windows、Python 3.12 和 Microsoft Edge WebView2。仓库只发布源码，不含 EXE。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-app.txt
.\.venv\Scripts\python.exe app/main.py
```

## 测试与打包

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --distpath dist --workpath build app/和融联.spec
```

运行 dist/和融联/和融联.exe；移动时复制整个发布文件夹。不要直接覆盖已有运行数据，更新前请备份 runtime 目录。关闭窗口驻留托盘；电脑必须开机、登录、联网且不睡眠。启动项需在应用内自行启用。

## 隐私与公开版差异

此公开副本已将原用户持仓匿名化为演示值：1000份、成本3元、现金10000元，初始参考资产13000元，不代表真实账户。使用前请按实际账户配置 app/service.py 中的新账户默认值；已有数据库不会被默认值覆盖。推送 Token 默认为空，在设置中自行填写，本机以 Windows DPAPI 保存。请勿提交运行数据库、日志、密钥或账户截图。

data/article_rule_registry.json 是策略溯源的结构化提炼，不包含原始文章 PDF。config 与 src 包含研究规则，桌面实时执行使用 app/signals.py 中的可验证子集，不代表完整复刻。

## 限制

公共行情没有实时性保证；数据过期暂停建议。内置交易日历仅覆盖2026年。方向诊断不等于真实交易收益，复权、分红、交易成本需核验；阈值尚未经过完整历史回测。不要将研究输出视为收益承诺。
