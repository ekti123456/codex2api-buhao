# Account Pool Manager

Codex2API 的独立补号跳板。它解决静态网页无法安全保管 `X-Admin-Key` 的问题：浏览器只连接本服务，管理密钥始终留在服务端环境变量中。

## 已实现

- 管理员密码登录、HttpOnly + SameSite 会话 Cookie、登录失败限速。
- 管理员与供应商完全隔离的会话和页面。
- 服务端代理 Codex2API，只向供应商公开分组缺口，不公开账号列表、邮箱、用量明细或管理密钥。
- 每个分组独立设置目标可用数、可用账号阈值、7d 剩余额度阈值和触发条件。
- `任一条件触发` / `全部条件触发`、补号冷却、单次补号上限。
- 供应商直补默认关闭；管理员显式开启后，供应商才能提交 Refresh Token。
- 新版 Codex2API 直接使用 `group_ids`；旧版响应不含 `bound_groups` 时，自动定位本次新账号并逐个调用 scheduler 接口绑定分组。
- 审计日志不记录 Token、密码或 Codex2API 管理密钥。
- 后台按设置的评估间隔监测需求变化，不会自动凭空创建账号。

## 补号判定

“目标数量”定义为目标可用账号数，而不是数据库里的账号总数。账号同时满足以下条件才计为可用：

1. `status` 为 `active` 或 `ready`；
2. 没有被禁用；
3. `health_tier` 不是 `banned` 或 `error`。

7d 指标使用“剩余额度”而不是“已用比例”：

```text
单账号 7d 剩余额度 = 100% - usage_percent_7d
分组 7d 剩余额度 = 有用量样本账号的平均剩余额度
```

当额度触发时，系统估算添加多少个全新账号后能把平均剩余额度拉回阈值。最终建议数量取“账号数量缺口”和“额度容量缺口”的较大值，再应用单次上限与冷却时间。

## 本地运行（PowerShell）

不要把真实 `.env` 提交到 Git。可以从 `env.example` 复制值到进程环境：

```powershell
$env:CODEX2API_BASE_URL='https://your-codex2api.example.com'
$env:CODEX2API_ADMIN_KEY='你的管理密钥'
$env:POOL_MANAGER_ADMIN_PASSWORD='管理员长密码'
$env:POOL_MANAGER_SUPPLIER_PASSWORD='供应商长密码'
python server.py
```

打开：

- 管理员：`http://127.0.0.1:8790/`
- 供应商：`http://127.0.0.1:8790/#supplier`

## 生产部署

复制环境变量模板并启动容器：

```bash
cp env.example .env
# 编辑 .env 后再启动
docker compose up -d --build
```

使用 `Dockerfile` 或 `compose.yml` 部署，并在前面放置 HTTPS 反向代理。生产环境必须：

- 设置 `POOL_MANAGER_SECURE_COOKIE=true`；
- 只让反向代理访问 8790，不直接暴露服务端口；
- 管理员和供应商使用不同的随机长密码；
- 定期轮换 `CODEX2API_ADMIN_KEY`；
- 将 `/app/data` 挂载到权限受控的持久卷；
- 限制供应商入口的来源 IP 或再加一层 Cloudflare Access / VPN；
- 不要把供应商密码放进 URL、聊天消息或前端代码。

## 风险边界

供应商直补是写操作，因此默认关闭。开启后，已登录的供应商能够向当前确实存在缺口的分组提交账号，但仍受以下限制：

- 只接受策略当前计算出的缺口；
- 受 `max_accounts_per_run` 限制；
- 补号后进入冷却期；
- 每次操作写入审计日志；
- Token 仅在请求过程中存在，不写入设置或审计文件。
