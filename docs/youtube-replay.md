# ③ YouTube 直播回放自动投稿

一条几小时的长直播，从"频道开播"到"B 站出现 P1/P2 分段稿件"，中枢用一条 SQLite 队列串起来。每一段都可以单独重试、单独审核、单独查结果。

## 一场长直播的完整路径

| 阶段 | 触发 | 代码 | 结果 |
| --- | --- | --- | --- |
| 发现 | 每 5/10 分钟轮询频道与直播状态 | `services/hub/youtube_monitor.py` | 官方元数据写入 `youtube_videos` |
| 登记 | 直播结束后登记候选 | `services/hub/replay_fifo_v4.py`（`register_all_observed`） | `replay_fifo_v4` 候选，状态 `registered` |
| 等待就绪 | 平台还在处理回放 | `services/hub/youtube_readiness_v4.py` | 等待 `uploadStatus=processed` 或稳定时长 |
| 计划 | 就绪后排计划 | `services/hub/youtube_download_v4.py`（`YouTubeWholeReplayPrepare`） | 计划含时长、字节数、分段边界、最高画质选择 |
| 授权/排队 | 磁盘门与单并发 | `replay_fifo_v4.enqueue_download_tick` + 容量门 | 同一时刻只有一个重任务 |
| 下载 | 计划批准后 | `YouTubeWholeReplayDownload` + `adapters/ytdlp_whole_v4.py` | 原始文件 + `verified.json`（完整性校验） |
| 封面 | 下载完成后 | `services/hub/replay_video_cover.py` | 封面图 |
| 分段 | 超过单稿件上限时 | `services/hub/replay_parts_v5.py`、`replay_multipart_v4.py` | 各段独立计划，标题标 P1/N |
| 投稿 | 人工确认后 | `services/hub/replay_part_publish_worker_v5.py` | 每段一个 BV，`publications` 记录 |
| 审核 | B 站审核期 | `replay_later_result_lookup_v2.mjs` | 只查结果，不重发 |
| 回写/清理 | 审核通过后 | `publications` + `services/hub/replay_parts_retention_v5.py` | BV 号回写；素材按保留策略清理 |

## 四个容易踩坑的点

**1. 同一回放如何去重**

- `replay_fifo_v4` 表对 `(source_id, target_uid)` 建唯一约束；
- 登记前 `_existing()` 会检查：已有投稿（`publications`）、来源墓碑（`source_tombstones`）、同源已有素材任务；
- 所以同一场直播不会出现两个候选，也不会重复下载。

**2. 长视频如何排队**

- 全局只有一条重任务通道（`heavy_concurrency = 1`），下载前有过磁盘门（`disk_floor_gb`）；
- 计划、下载、投稿都是独立 job，失败只影响自己；
- 下载中断（进程崩溃）由 10 分钟对账的第 ⑦ 类自动续传，保留原 attempts。

**3. 各段如何作为独立稿件标注**

- 计划阶段由 `replay_multipart_v4.plan_whole_replay` 切段；
- 每段标题带 `P1/N`、`P2/N` 标记，各自独立投稿、独立 BV；
- 单段仍受 20 分钟 / 1 GiB 上限约束（与平台的字幕任务上限一致）。

**4. 投稿结果未知时只查结果**

- `Publishing` 用幂等键（`bilibili + 来源 + 账号 + kind`）防重复；
- 结果未知会抛 `PublishUnknown`，任务停在"待核对"，由 `replay_later_result_lookup_v2.mjs` 只读查询；
- **不会**自动重发，也不会把"查询不到"当成"没发出去"。

## 公开版默认 vs 生产已验证

| 能力 | 公开版默认 | 生产环境已验证 |
| --- | --- | --- |
| 频道轮询 / 发现 | `youtube_feed_enabled=false` | 是，5/10 分钟轮询 |
| 回放下载 | `youtube_download_enabled=false` | 是，最高画质 + 完整性校验 |
| 分段投稿 | 需显式打开 | 是，P1/N 独立稿件 |
| 自动发布 | `auto_publish=false` | 人工确认后发布 |
| 崩溃自愈 / 来源修订对账 | 代码在，需自己挂 timer | 是，10 分钟对账 |

## 运行入口

```bash
cp config/hub.example.toml config/hub.toml
cp .env.example .env
docker compose build
docker compose run --rm api python -m services.hub.cli init
docker compose run --rm api python -m services.hub.cli status

# 只跑一个 worker 周期（不下载，队列为空时直接返回）
docker compose run --rm api python -m services.hub.cli worker --queue video --once
```

真实下载需要：YouTube Data API Key、可用的 yt-dlp、`youtube_download_enabled=true`、磁盘余量 ≥ `disk_floor_gb`。

## 离线示例与预期结果

```bash
docker compose run --rm api python -m pytest \
  tests/test_replay_fifo_v4.py \
  tests/test_replay_fifo_duration_revision_v1.py \
  tests/ops/test_reconcile_replay_source_revision_v1.py \
  tests/test_worker_unexpected_retry.py \
  tests/test_replay_fifo_rebind_v4.py -q
```

预期：`65 passed`（合成元数据 + 真 SQLite，不访问 YouTube）。

## 常见故障与恢复

| 现象 | 含义 | 处理 |
| --- | --- | --- |
| `REPLAY_FIFO_SOURCE_IDENTITY_CHANGED` | 官方时长/状态与登记快照差超过 5 秒 | 等平台定稿；10 分钟对账会按档位自动重绑或出决策包 |
| 候选一直 `hold` | 修订超出自动范围 | 看 `data/ops-maintenance/` 决策包，确认后按包内命令处理 |
| 下载失败但无 `verified.json` | 媒体不完整 | 对账第 ⑦ 类自动续传；不要手工重置 attempts |
| `REPLAY_PART_RETENTION_RELEASE_REQUIRED` | 保留证书与当前源码不一致 | 重新鉴定并安装证书；期间不会删除素材（fail-closed） |
| 投稿停在"待核对" | B 站结果未知 | 只用查询脚本核对，不要重新投稿 |
| 磁盘不足 | 低于 `disk_floor_gb` | 先清理已归档素材或扩容，队列会等待 |
