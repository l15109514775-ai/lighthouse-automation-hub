# ④ X 推文自动翻译与 B 站动态

一条推文从"刚发出来"到"B 站动态里出现一张中文译图"，中枢要经过发现、去重、翻译、独立质检、排版、人工审核、发布七步。任何一步证据不足都会停下，而不是猜。

## 完整路径

| 阶段 | 代码 | 产物 |
| --- | --- | --- |
| 发现 | `services/hub/x_api_discovery.py`（官方 API 轮询） | 新帖写入候选 |
| 去重 | 同上：`known` 集合来自 `jobs` + `source_tombstones` + `publications` + capture queue | 已处理/已发布的帖子不再建任务 |
| 建任务 | `services/hub/pipelines.py`（`TweetPipeline`） | `tweet_process` job |
| 翻译 | `machine_service(...).process(job, self.model)` → `adapters/model.py`（按 phase 调模型） | 中文译文 + 模型收据 |
| 独立质检 | 模型独立 phase + `machine_tweets.py` 的 `machine_evidence` | `machine_checks_passed`、模型收据完整性 |
| 排版 | `adapters/compositor.py` → `node adapters/render_bridge.mjs`（`compositor/` 子模块） | PNG + 排版报告 |
| 人工审核 | `services/hub/tweet_reviews.py`（签名审核包，6 项检查） | `publication_reviews` 记录 |
| 发布 | `services/hub/publishing.py` + `adapters/bilibili.py` | 动态 ID / 链接 |
| 回写 | `publications` 表的 `external_id` | 任务结果里的链接 |

## 不同推文类型能读到什么

| 类型 | 官方 API 能拿到 | 排版时保留的上下文 |
| --- | --- | --- |
| 普通推文 | 全文、时间、作者、静态图 | 原文 + 译文 + 时间 |
| 回复 | 全文 + 被回复帖 ID/正文（`official_reply_parent`） | 原帖 + 主播回复两段，避免断章取义 |
| 引用 | 全文 + 被引用帖（`official_quoted_post`） | 两段并排或上下排 |
| 转推 | 转推者 + 原帖（`official_quoted_post` / 关系接口） | 标注转推关系，不伪装成原创 |

关系能力由 `machine_tweets_api.py` 的 `SCOPE` / `REFERENCE_SCOPE` 控制；v4 关系策略（`machine-tweet-v4-api-relations`）才包含引用与"回复+引用"。

## 费用与预算

- 所有 X 调用受 `budgets.x` 约束（日/月金额与次数、单次上限）；
- 只有 `paid_authorized=true` 才允许产生费用；
- 每次调用写 `x_cost_ledger`，超额直接拒绝，不会"先花后补"。

## 来源或媒体无法核实时

- 官方 API 收据缺失/过期 → `source_check` 拒绝，任务停下；
- 图片下载失败或尺寸不符 → 排版不启动，保留失败记录；
- 模型输出未通过质检 → 不进入审核队列；
- 发布结果未知 → `PublishUnknown`，只允许查询核对，**不重发**（`publications` 的幂等键 + 状态机）。

## 排版器 vs B 站适配器

| 组件 | 负责 | 不负责 |
| --- | --- | --- |
| `compositor/`（Node） | 读取结构化输入，渲染中文译图 PNG，产出布局报告与哈希 | 不登录、不发布、不翻译 |
| `adapters/bilibili.py` + `publishing.py` | 人工审核证据校验、账号校验、幂等提交、动态/视频发布、结果核对 | 不做图像渲染 |

## 运行入口

```bash
cp config/hub.example.toml config/hub.toml
cp .env.example .env
docker compose build
docker compose run --rm api python -m services.hub.cli init
docker compose run --rm api python -m services.hub.cli worker --queue tweet --once
```

公开版默认 `DRY_RUN=true`、`x_api_enabled=false`、`auto_publish=false`；真实采集需要 X API 凭据，真实发布还需要 `authorized_bilibili_accounts` 与人工审核包。

## 离线示例与预期结果

```bash
docker compose run --rm api python -m pytest \
  tests/test_tweet.py tests/test_manual_openai.py tests/test_qa_benchmark.py -q
```

预期：全部通过（合成帖子、假模型、假平台，不发真实请求）。

## 常见故障与恢复

| 现象 | 含义 | 处理 |
| --- | --- | --- |
| `X_API_BUDGET_EXCEEDED` | 超出预算 | 调整 `budgets.x` 或等下一个周期；不要绕过预算 |
| `SOURCE_RECEIPT_...` | 来源收据缺失/过期 | 重新采集该帖；不要用缓存冒充新证据 |
| 排版失败 | 输入缺字段或字体缺失 | 检查 `compositor/` 的 `npm run doctor` 与字体安装 |
| `PUBLISH_REVIEW_EVIDENCE_REQUIRED` | 没有人工审核或证据不匹配 | 重新走审核，不要改库跳过 |
| 发布停在 `unknown` | B 站结果未知 | 只查询核对；确认未发出才允许重新提交 |
| 中文显示成方块 | 容器缺中文字体 | 安装 `fonts-noto-cjk` 后重建镜像 |
