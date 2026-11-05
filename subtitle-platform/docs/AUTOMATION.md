# 全自动字幕流水线（v0.3）

本文说明当前正式流程：网页提交 → 自动审批 → 上海拉取 → 硅谷识别 → 自动回传。

## 为什么拆两台服务器

| 约束 | 结果 |
| --- | --- |
| CloudBase 环境在上海 | 上海节点下载原视频是同地域，285MB 通常几十秒 |
| OpenAI 只能从硅谷访问 | 识别、裁判、翻译必须放在硅谷 |
| 上海出口带宽 5Mbps | 不能把 285MB 视频传硅谷；只传 16k 单声道 mp3（20 分钟约 10MB，约 20 秒） |

所以上海只做「下载 + 抽音频 + 传输 + 清理」，硅谷做「识别 + 翻译 + 发布」。

## 组件

| 组件 | 位置 | 职责 |
| --- | --- | --- |
| `st_subtitle_auto_decide` | CloudBase SQL | 按白名单和每日限额自动批准，返回可执行的 queued job |
| `subtitle-poller` | 上海 systemd | 每 5 分钟调用 auto_decide，对每个 queued job 运行 fetcher |
| `fetch_shanghai.py` | 上海 | 领取 job → 下载分片 → 校验 SHA-256 → ffmpeg 抽 mp3 → POST 到硅谷 → 删除本地 |
| `ingest.py` | 硅谷 | 校验 token 与音频 SHA-256，写入 claim/session，启动 worker |
| `platform_worker.py --audio-file` | 硅谷 | 复用上海领取的租约，跳过视频下载，直接处理音频并发布 |
| `worker.py` + `asr_api.py` | 硅谷 | whisper-1 主识别、千问 60 秒窗口、gpt-4o 分歧裁判、翻译、交付 9 个文件 |

## 一次任务的完整时序

```
成员提交（带 24 小时签名 URL）
  └─ st_subtitle_request_submit(p_sources)
        ↓ ≤5 分钟
subtitle-poller → st_subtitle_auto_decide
  ├─ 白名单 + 每日条数/分钟 + 全局限额
  └─ 建 project/task/job，state=queued
        ↓
fetch_shanghai.py
  ├─ claim（state=running，lease 45 分钟）
  ├─ 下载 69 个分片，逐片校验，最后校验整文件 SHA-256
  ├─ ffmpeg -ac 1 -ar 16000 -b:a 64k → audio.mp3
  ├─ POST https://<硅谷>:18081/ingest（X-Token / X-Claim / X-Source-Sha256 / X-Audio-Sha256）
  └─ 删除 /root/subtitle-work/<job>
        ↓
ingest.py
  ├─ 校验 token、claim、音频 SHA-256
  ├─ 写 work/control/<job>/session.json（沿用上海领取的 claim 与 lease）
  └─ 启动 platform_worker.py --audio-file ... --execute
        ↓
worker
  ├─ whisper-1 verbose_json → 句段与时间轴
  ├─ 千问 60 秒窗口 → 第二意见
  ├─ gpt-4o-transcribe 只在分歧窗口调用（上限 10 次）
  ├─ 翻译（中转站）+ 审校 + ASS/SRT/报告
  └─ publish → 任务 succeeded，触发 completed 通知
```

## 限额与配额

- 单条：≤20 分钟、≤1 GiB；
- 每人：每天 2 条、累计 ≤40 分钟；
- 全局：每天 6 条、≤120 分钟；
- 识别请求：text ≤400、千问 ≤40、裁判 ≤10；
- 超限申请留在 `pending`，不会降级、不会重复扣费。

限额可以在组长网页的「自动审批白名单」面板调整，或调用：

```sql
select st_subtitle_auto_limits_set(true, 6, 7200);
select st_subtitle_auto_policy_set('<member-uuid>', true, 2, 2400);
```

## 密钥与环境

硅谷 `/opt/lighthouse-hub/backups/subtitle-runner/env`（root-only）：

| 变量 | 用途 |
| --- | --- |
| `SUBTITLE_API_BASE` | `https://<env>.api.tcloudbasegateway.com/v1/rdb/rest/rpc` |
| `SUBTITLE_PLATFORM_TOKEN` | 64 位 worker key（有效期 2125 年） |
| `SUBTITLE_PLATFORM_GATEWAY_KEY` | CloudBase publish key（anon） |
| `OPENAI_API_KEY` | whisper-1 / gpt-4o-transcribe，需要 Audio scope |
| `DASHSCOPE_API_KEY` | 千问 MaaS key（`sk-ws-…`） |
| `SUBTITLE_QWEN_TOKEN` | 同上，兼容旧路径 |
| `SUBTITLE_GPT_TOKEN` | 翻译中转站 key |
| `SUBTITLE_INGEST_TOKEN` | 上海 → 硅谷音频上传令牌 |

更换密钥：修改 env 文件 → `docker rm -f subtitle-ingest && docker run ...`（环境变量在创建时读取）→ 上海 `systemctl restart subtitle-poller`。

## 失败与人工核对

- 上海下载失败：job 保持 `running`/`failed`，不会重复领取；检查 `journalctl -u subtitle-poller`；
- 音频传输失败：fetcher 退出码非 0，poller 记录 `fetch_done code=1`；
- worker 失败：任务卡显示 `failed`，`docker logs subtitle-ingest` 可见原因；已付费请求的 marker 会阻止自动重发；
- 发布结果不确定：任务进入 `waiting_reconciliation`，不会重复发送，需要人工核对；
- 所有失败都不会自动重试已发送的模型请求。

## 清理

- 上海：fetcher 成功后立即删除 `/root/subtitle-work/<job>`；另有 cron 每 30 分钟清理 2 小时以上残留；
- CloudBase：原视频保留 7 天后由保留期任务清理，字幕与报告继续保留；
- 组长可在「已删除申请」里永久删除已撤回/已拒绝且无云任务的申请（迁移 052–054）。

## 通知

机器人调用 `st_qq_job_feed`（参数与旧 `st_qq_approval_feed` 类似），返回 `started` / `completed` / `failed` 事件，包含 `member_id`、`member_name`、`owner_id`、`owner_name`、`stream_title`、`job_id`、`job_state`。开始与完成各发一条私聊给申请人和组长。

## 已验证

| 用例 | 结果 |
| --- | --- |
| 285MB / 170 秒 | 上海下载 69 片，音频 1,355,476 字节；45 条字幕；3 个千问窗口；1 次裁判；0 冲突；发布成功 |
| 40 秒新申请（全自动） | 02:19:47 自动批准，54 秒拉取，37.6 秒识别发布；19 条字幕；1 次裁判；0 冲突 |

## 已批准申请的自动归档

- 迁移 055 新增 `st_subtitle_requests_archive(p_days default 30)`；
- 只处理满足以下条件的已批准申请：创建超过 30 天、任务已结束（succeeded / failed / cancelled）、没有活动租约；
- 只做软删除：写入 `deleted_at` / `deleted_by`，申请进入“已删除申请”，所有任务、交付、通知和积分记录都保留；
- 组长可在“已删除申请”里点“恢复申请”；已结束任务（含 succeeded）也允许恢复（迁移 055 同时放宽了 `st_subtitle_request_set_deleted`）；
- 定时任务：硅谷服务器 `/etc/cron.d/subtitle-archive`，每天 05:00 调用一次，日志在 `/var/log/subtitle-archive.log`。

```sql
-- 手动执行（默认 30 天）
select st_subtitle_requests_archive(30);
```

