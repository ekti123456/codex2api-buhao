# Account Pool Manager

Codex2API 的独立补号跳板。它解决静态网页无法安全保管 `X-Admin-Key` 的问题：浏览器只连接本服务，管理密钥始终留在服务端环境变量中。

## 已实现

- 管理员密码登录、HttpOnly + SameSite 会话 Cookie、登录失败限速。
- 管理员创建独立供应商密钥，可随时启用/禁用；数据库只保存密钥哈希，明文只在创建时返回一次。
- 新建密钥可一键复制；忘记后可重置并复制新密钥，旧密钥和旧登录立即失效，数据库仍只保存哈希。
- 服务端代理 Codex2API，只向供应商公开分组缺口，不公开账号列表、邮箱、用量明细或管理密钥。
- 每条策略将“检查分组”和“补号目标分组”分离：可检查 Plus，并把合格新账号同时加入 Plus、分流等多个分组。
- 补号目标分组仅管理员可见，不会出现在供应商网页、需求 API 或补号响应中。
- `任一条件触发` / `全部条件触发`、补号冷却、单次补号上限。
- 供应商直补默认关闭；管理员显式开启后，供应商才能提交 Refresh Token。
- 每个 RT 单独导入，随后重新读取账号状态；只有账号为 active/ready 且确认进入全部目标分组才计入成功。
- SQLite 持久化策略、供应商密钥哈希、供应商补号账号和审计记录；不保存 RT、上游管理密钥或登录密码。
- 默认每 10 分钟整批量列出一次上游账号并比对供应商账号状态；间隔可在设置中调整，也可随时手动验活。页面显示最后状态、验活时间和自导入后的存活时长。
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
$env:POOL_MANAGER_DATABASE_FILE='.\data\pool-manager.sqlite3'
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
- 管理员登录后为每个供应商创建独立密钥；
- 定期轮换 `CODEX2API_ADMIN_KEY`；
- 将 `/app/data` 挂载到权限受控的持久卷；
- 限制供应商入口的来源 IP 或再加一层 Cloudflare Access / VPN；
- 不要把供应商密钥放进 URL、聊天消息或前端代码。

Compose 默认使用名为 `pool-manager-data` 的 Docker 卷，避免宿主机 `./data` 由 root 创建后，非 root 容器无法写入 SQLite。`.env` 文件不是强制的：既可以放在仓库目录，也可以由服务器面板或 Shell 注入同名环境变量。

在 Railway、Zeabur 等动态端口平台，程序会按 `PORT`、`WEB_PORT`、`POOL_MANAGER_PORT` 的顺序选择第一个有效数字端口；类似 `PORT=${WEB_PORT}` 的模板值也能回退读取实际的 `WEB_PORT`。通用的 `PASSWORD` 变量不会被读取，管理员密码必须使用 `POOL_MANAGER_ADMIN_PASSWORD`。

## 供应商 API

供应商登录网页后可在“供应商 API 接入”区域查看当前站点的完整 cURL 示例并一键复制。API 无需先创建网页会话，使用管理员创建的同一密钥即可查询实际缺口：

```bash
curl https://pool-manager.example.com/api/supplier/v1/demand \
  -H 'X-Supplier-Key: sup_xxx'
```

返回 `needed` 后，只提交对应数量的 RT：

```bash
curl -X POST https://pool-manager.example.com/api/supplier/v1/supply \
  -H 'X-Supplier-Key: sup_xxx' \
  -H 'Content-Type: application/json' \
  -d '{"group_id":1,"refresh_tokens":"rt_one\nrt_two","proxy_url":""}'
```

响应中的 `accepted` 是通过刷新、存活状态和全部目标分组三重校验的数量；失败账号不会计入补号成功，供应商可重新查询剩余缺口再补。

上游添加接口可能先返回成功、账号随后仍处于 `refreshing`。本服务会在 `POOL_MANAGER_ACCOUNT_VERIFY_SECONDS`（默认 60 秒）内轮询；只有最终变为 `active/ready` 才接受，超时或失败会删除本次创建的账号，避免污染账号池。

## 风险边界

供应商直补是写操作，因此默认关闭。开启后，已登录的供应商能够向当前确实存在缺口的分组提交账号，但仍受以下限制：

- 只接受策略当前计算出的缺口；
- 受 `max_accounts_per_run` 限制；
- 补号后进入冷却期；
- 每次操作写入审计日志；
- Token 仅在请求过程中存在，不写入 SQLite、设置或审计记录。
