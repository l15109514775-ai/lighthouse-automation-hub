# 运维手册：状态、备份、日志、重启恢复

这一页只讲"跑起来之后怎么确认它还好"。生产环境的实际地址、账号和凭据不在仓库里，命令全部用占位符。

## 1. 状态查询

### 自动化中枢

```bash
# 队列、流水线、磁盘、YouTube 就绪情况
docker compose run --rm api python -m services.hub.cli init
docker compose run --rm api python -m services.hub.cli status

# API 健康检查（默认只监听 127.0.0.1:8080）
curl -s http://127.0.0.1:8080/health
```

直接看库（只读，别在跑任务时写）：

```bash
sqlite3 -readonly data/hub.sqlite3 \
  "select queue, state, count(*) from jobs group by 1,2 order by 1,2;"
sqlite3 -readonly data/hub.sqlite3 \
  "select name, strftime('%s','now')-at as age from heartbeats order by age;"
```

- `heartbeats` 里 `worker:video`、`worker:video-retention-v5`、`worker:video-producer-v5` 的 `age` 应在几十秒内；
- `jobs` 里长期 `processing` 且租约过期的任务，会被 worker 的对账/续传逻辑接手；
- 失败任务看 `error_code` 与 `audit`，不要直接改状态。

### 字幕协作平台

- 网站：登录后「任务工作台」能列出项目与任务即正常；
- 数据库：CloudBase 控制台 → PostgreSQL → SQL 编辑器，`select count(*) from st_members;` 之类只读查询；
- QQ 机器人：私聊 `字幕通知状态`，会返回已发/待发/待核对数量；群通知异常时先看这个。

## 2. CloudBase 异地备份

中枢的备份由 systemd timer 触发（生产为每天 03:30 Asia/Shanghai）：

```bash
systemctl list-timers | grep -i cloudbase
systemctl status lighthouse-cloudbase-backup.timer
journalctl -u lighthouse-cloudbase-backup.service -n 100 --no-pager
```

代码路径：

- `services/hub/cloudbase_backup_runtime.py`：编排、收据、失败重试；
- `adapters/cloudbase_cos_backup.py`：COS 上传、校验、保留清理；
- `services/hub/archive_source_retention_v2.py`：源文件清理（需要合格证书，fail-closed）。

判断一次备份是否成功，看三样：

1. 服务退出码 0；
2. 数据库里当天的备份记录 `state=completed`、`error_code=null`；
3. 归档对象的 SHA-256 与本地回执一致（收据在 `data/` 下的维护记录里）。

**注意**：清理与删除都受"保留证书"约束；证书与当前源码不一致时只会拒绝删除，不会误删。恢复顺序见 `docs/` 中备份相关记录。

## 3. 日志

```bash
# 中枢
docker compose logs -f --tail=200 api
docker compose logs -f --tail=200 video-worker
journalctl -u lighthouse-replay-source-reconcile.service -n 50 --no-pager

# 平台（CloudBase 控制台）
# 云函数/RPC 日志与数据库慢查询在控制台查看，不在本机
```

日志里不应出现凭据、Cookie、绑定码或完整请求体；如果出现，按安全事件处理并轮换对应凭据。

## 4. 重启与恢复

```bash
# 中枢整体重启
docker compose up -d

# 只重启某个 worker
docker compose restart video-worker

# 平台
# 网站是静态托管，无需重启；数据库在 CloudBase 控制台查看实例状态
```

重启后的自愈顺序：

1. worker 心跳恢复（`heartbeats` 表）；
2. 被中断的下载由对账第 ⑦ 类续传（保留 attempts，不重复下载）；
3. 来源修订由 10 分钟对账按档位处理（自动重绑 / 决策包 / 保持）；
4. 备份 timer 到点继续，失败会在下一次自然周期重试。

**不要做**：手工删除 `jobs` 行、清零 attempts、改写 `publications`、删除 `verified.json` 来"让它重跑"。这些都会破坏幂等与审计；确实需要人工介入时，用 `docs/` 里的恢复脚本或决策包。

## 5. 出问题时的第一分钟

| 症状 | 先看 |
| --- | --- |
| 队列不动 | `heartbeats` 的 age、`docker compose ps`、`jobs` 里是否有 `processing` 卡住 |
| 下载反复失败 | `error_code`、磁盘余量、`verified.json` 是否存在 |
| 投稿重复风险 | `publications` 的 `state` 与 `idempotency_key`，只查不重发 |
| 备份失败 | `journalctl -u lighthouse-cloudbase-backup.service`、COS 权限、网络 |
| 网站打不开 | CloudBase 控制台环境状态、静态托管域名、CSP 的 gateway 域名 |
| 机器人不回复 | `subtitle_bridge.json` 的 `platform_id`、gateway 地址、QQ 机器人权限 |
