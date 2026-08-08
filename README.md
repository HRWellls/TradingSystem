# 望长盈交易系统

## 启动历史净值服务

首次使用需要安装依赖：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

启动本地服务：

```bash
.venv/bin/python server.py
```

然后打开 <http://127.0.0.1:8000/>。展开任意基金卡片的“查看走势”，页面会按需请求历史净值。数据默认缓存 6 小时，服务会在缓存过期后再次从 AKShare 获取；网络暂时不可用时会返回最近一次成功缓存。

服务优先使用 AKShare（东方财富数据），如果东方财富域名无法解析，会自动尝试新浪财经备用源。两者都无法访问时，请先检查当前网络的 DNS 设置，再点击页面上的“重新加载”。

如果需要立即刷新某支基金，可以请求：

```text
/api/funds/003629/history?refresh=1
```

直接双击打开 `system.html` 时，历史走势图接口无法使用；需要通过上面的本地服务访问页面。
