# 推文发布运维：上传失败、孤儿草稿与补发

本文说明推文自动发布在"图片上传失败"时的行为、如何识别孤儿草稿，以及三条运维工具的用法。所有示例使用占位 ID，真实账号与帖子不在仓库里。

## 两个真实故障模式

1. **上传失败留下孤儿草稿**：浏览器先把译文说明输入 B 站动态编辑器，再上传译图；如果上传接口报错（例如大图或网络中断），正文已经留在编辑器里。任务失败重试后，草稿仍在；后续发布任务看到"不是本次帖子"的草稿，会按设计拒绝覆盖，报 `UNRELATED_DRAFT_REQUIRES_REVIEW`。
2. **超时掩盖真实错误**：协调端等待"草稿就绪"与浏览器等待"图片上传完成"都用 120 秒，但浏览器还要先导航、输入正文。协调端总是先超时，于是任务被记成可重试的 `WEB_DRAFT_PREPARATION_TIMEOUT`，精确错误被掩盖，还会多试几次。

## 现在的行为

- **上传前尺寸闸门**（`adapters/browser_publisher.mjs`）：译图超过 3 MB 或 4096px 时直接报 `IMAGE_TOO_LARGE_FOR_UPLOAD`，**不输入正文**，因此不会产生草稿。
- **上传失败快速失败**：页面出现"上传失败"后先点一次"重试"，仍失败立即写 `IMAGE_UPLOAD_NOT_COMPLETED`，不再等满 120 秒。
- **失败后清理自己的正文**：仅当编辑器文本**完全等于**本次 caption 且没有成功图片时，才清空自己的正文并写 `own-draft-cleared.json`；任何不匹配的草稿都不动。
- **渲染缩放**（`adapters/render_machine_official_relations.mjs`）：译图超限时用 sharp 等比缩到 ≤1600 宽 / ≤4096 高，报告写 `normalization` 并同步哈希。
- **owner 授权补发**：`config/owner-backfill-authorizations.json` 中逐条列出帖子 ID、`authorized_at`、`not_after` 与 `reference` 后，该帖子可以跨 2 小时窗口与 `OWNER_BACKLOG_CLEARED` 补发；默认规则不变。

## 三条运维工具

```bash
# 1) 健康检查：近 24 小时失败发布与不确定投稿（只读，异常退出码 1）
python scripts/ops/tweet_publish_health_v1.py --hours 24

# 2) 重排一条失败的发布子任务（默认 dry-run）
python scripts/ops/requeue_machine_tweet_publish_v1.py --list
python scripts/ops/requeue_machine_tweet_publish_v1.py --parent <tweet_process_job_id> --apply --reason "owner chat ..."

# 3) 清空一条孤儿草稿（默认 dry-run；哈希匹配且无图片才会清空）
node scripts/ops/clear_orphaned_bilibili_draft_v1.mjs \
  --expected-caption-sha256 <64位哈希> [--confirm-clear]
```

安全约束：

- 重排工具拒绝：已有投稿记录、子任务 attempts 耗尽、子任务处于 `publish_unknown`（结果未知）、未授权跨窗口或墓碑；
- 草稿工具只在文本哈希匹配且图片数为 0 时清空，前后截图，**永不点击发布**；
- 所有写操作都写 `audit`，保留原任务、attempts 与证据文件。

## 建议的处置顺序

1. `tweet_publish_health_v1.py` 确认失败清单（含 `source_url`、`caption_sha256`）；
2. 用哈希匹配的草稿工具清掉孤儿草稿；
3. 需要补发时，先确认该帖子在授权文件内（或仍在 2 小时窗口内），再 `requeue ... --apply`；
4. 观察下一次发布：`ready.json` 应在 60 秒内出现，结果为 `published_visible`；
5. 若译图过大，先让新渲染的缩放生效，旧任务需要重新渲染后再发布。
