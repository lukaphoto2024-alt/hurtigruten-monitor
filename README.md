# Hurtigruten 舱位监控

每 10 分钟查一次 Hurtigruten 港到港航线（默认 Tromsø → Svolvær，2027-02-07，2 人一间），
发现可订的舱位就发邮件提醒。**只提醒，不自动下单。**

## 怎么改查询条件
编辑 `config.json`：
- `dates`：出发日期（挪威当地日期，可写多个，如 `["2027-02-07", "2027-02-08"]`）
- `categories`：要盯的舱型
  - `PolarInside` 内舱（无窗，最便宜）
  - `PolarOutside` 外舱（有窗）
  - `ArcticSuperior` 高级外舱
  - `ExpeditionSuite` 套房
  - `DECK` 无舱位 / 大厅座位
- `mail_to`：收提醒的邮箱

## 需要的 Secrets（Settings → Secrets and variables → Actions）
| 名称 | 例子 |
|---|---|
| `SMTP_HOST` | 可选，默认 `smtp.gmail.com` |
| `SMTP_PORT` | 可选，默认 `465` |
| `SMTP_USER` | 发件邮箱地址 |
| `SMTP_PASS` | 邮箱的 SMTP 授权码（不是登录密码） |
| `MAIL_TO` | 可选，覆盖 config.json 里的收件人 |

如果 GitHub 服务器被官网拦截，在 Variables 里加 `USE_BROWSER=true`，会改用无头浏览器查询。

## 手动测试
Actions → Hurtigruten 舱位监控 → Run workflow：
- `check` 正常查询一次
- `test-mail` 只发一封测试邮件

## 工作原理
调用官网搜索页自己用的接口 `POST /nellie-coastal-v2/api/availability`，
同一舱位只提醒一次，价格降了会再提醒（状态存在 `state.json`）。
