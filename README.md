# Lighthouse Automation Hub

多源内容自动化中枢：采集 X（Twitter）推文并翻译成中文、合成译图，发布到 B 站；抓取 YouTube 直播回放（最高画质、完整性校验、按需分段），并在确认后投稿；每天把业务库与素材归档到 CloudBase COS；任务队列带崩溃自愈、来源修订对账和保留策略。

本项目来自一套真实长期运行的部署，公开版做了脱敏：所有账号、频道、服务器地址、桶名、路径都换成示例值，真实录播、译图、封面和投稿证据不在仓库内。**默认 DRY_RUN，不会调用真实平台、不会产生费用。**

## 能力与边界

| 能力 | 状态 |
| --- | --- |
| SQLite 任务队列（租约、attempts、幂等、审计） | 已验证，`tests/` 覆盖 |
| YouTube 回放登记 → 计划 → 下载绑定 → 完整性校验 → 分段 | 已验证（含时长修订、崩溃续传、失败下载重排） |
| 回放分段上传 / 投稿 / 结果核对 | 已验证为“人工确认后发布”；自动发布默认关闭 |
| X 官方 API 采集 → 翻译 → 译图合成 → B 站发布 | 代码与测试完整；需要你自己的 X API、模型与 B 站授权 |
| 浏览器适配器（登录态、投稿页面、草稿、私有视频） | 可选；需要本机浏览器 profile，默认不启用 |
| CloudBase（腾讯云 COS）异地备份、校验、保留清理 | 已验证；需要你自己的桶与密钥 |
| 崩溃自愈 / 来源修订对账 / 保留证书（fail-closed） | 已验证；按定时器运行 |

## 快速开始（Docker Compose）

```bash
git clone --recursive https://github.com/<your-account>/lighthouse-automation-hub.git
cd lighthouse-automation-hub
cp config/hub.example.toml config/hub.toml
cp .env.example .env

docker compose build
docker compose run --rm api python -m services.hub.cli init
docker compose run --rm api python -m services.hub.cli status
docker compose up -d api scheduler
```

- 首次启动不需要任何真实账号：`DRY_RUN=true`、所有采集/下载/发布开关都是 `false`。
- `config/hub.toml` 里两个 `[[sources]]` 是示例频道；改成你自己的频道 ID / handle。
- 数据库、媒体、日志、备份分别落在 `./data`、`./media`、`./logs`、`./backups`。
- 只读根文件系统 + `cap_drop: ALL` + `no-new-privileges` + 浏览器 seccomp；`heavy_concurrency` 固定为 1。

## 离线样例（不联网、不产生费用）

```bash
# 初始化数据库并查看流水线
docker compose run --rm api python -m services.hub.cli init
docker compose run --rm api python -m services.hub.cli status

# 用合成数据跑通“登记 → 计划 → 下载绑定 → 来源修订 → 重排”的完整链路
docker compose run --rm api python -m pytest \
  tests/test_replay_fifo_v4.py \
  tests/test_replay_fifo_duration_revision_v1.py \
  tests/ops/test_reconcile_replay_source_revision_v1.py -q
```

这些测试使用内置的合成元数据与 SQLite，不访问 YouTube、B 站或任何外部服务。

## 接入真实服务需要什么

| 用途 | 需要你自己取得 |
| --- | --- |
| X 采集 | X API v2 凭据（付费档位）+ `X_BEARER_TOKEN` |
| 翻译 / QA | 兼容 OpenAI 风格的模型端点与 Key（`MODEL_*`），或替换为本地模型 |
| YouTube 元数据 | Google Cloud 项目 + YouTube Data API v3 Key（`YOUTUBE_API_KEY`） |
| B 站发布 | 你自己的账号登录态（浏览器 profile 或 Cookie 导入）；`target_bilibili_site_uid` 改成你的公开 UID |
| 异地备份 | 腾讯云 COS 桶 + 子账号密钥（`CLOUDBASE_*`） |

授权与启用顺序：先把 `DRY_RUN` 保持 `true` 跑通流程 → 单独开启 `youtube_feed_enabled` / `youtube_download_enabled` → 需要发布时再显式打开 `machine_tweet_enabled` / 自动发布开关。**收费调用必须由使用者自己把对应 budget 的 `paid_authorized` 改成 `true`。**

## 目录

```
services/hub/      队列、FIFO、计划/下载/发布、保留与对账、CLI
adapters/          平台适配器（HTTP、浏览器、COS、媒体处理）
scheduler/         持久化调度器与 SQL 迁移
tests/             单元/集成测试（合成数据）
config/            示例配置（hub.example.toml、各类策略示例）
scripts/           verify_upstream.py 等工具
compositor/        译图合成（git submodule，见下）
vendor/            字幕云 worker（git submodule，见下）
```

## 上游依赖与许可证

本项目自有代码（`services/`、`adapters/`、`scheduler/`、`tests/`、`scripts/`）以 **GNU AGPL-3.0** 发布，完整文本见 `LICENSE`。

`compositor/` 与 `vendor/subtitle-cloud-worker/` 通过 git submodule 引用各自仓库与固定提交，不在本仓库内复制代码；它们可能有各自的许可证，请分别查看对应仓库。

## 测试

```bash
docker compose run --rm api python -m pytest -q
```

单元测试不需要网络；浏览器类测试需要 Playwright 与浏览器缓存（镜像内已安装）。

## 安全与合规

- 不提交任何真实凭据、Cookie、私钥、生产数据库或投稿证据；`.env` 与 `config/hub.toml` 已在 `.gitignore` 中。
- 默认只读、单并发、磁盘下限、媒体大小上限，失败即停，不无限重试。
- 仅处理你有权处理的内容；遵守 X、YouTube、B 站与腾讯云的服务条款。自动化发布由你自行承担合规责任。
