# 执行器部署（runner）

本目录包含全自动流水线的运行组件：

| 文件 | 运行位置 | 作用 |
| --- | --- | --- |
| `poll_shanghai.py` | 上海 | 每 5 分钟调用 `st_subtitle_auto_decide`，对 queued job 运行 fetcher |
| `fetch_shanghai.py` | 上海 | 领取 job、下载分片、校验、抽 mp3、上传硅谷、清理 |
| `ingest.py` | 硅谷 | 接收音频与 claim，启动 worker 并发布 |
| `runner.py` | 硅谷 | 兼容旧模式：直接从 CloudBase 下载并处理（大文件跨区时不推荐） |

## 硅谷：ingest

```bash
BASE=/opt/lighthouse-hub/backups/subtitle-runner
docker run -d --name subtitle-ingest --restart unless-stopped \
  -p 0.0.0.0:18081:18081 \
  -v "$BASE/app:/app" -v "$BASE/work:/work" \
  --env-file "$BASE/env" -w /app \
  lighthouse-hub:tweet-publish-hotfix2 python /app/ingest.py
```

- 防火墙只放行上海公网 IP 的 TCP 18081（腾讯云 Lighthouse 防火墙）；
- `env` 需要 `SUBTITLE_INGEST_TOKEN`、`OPENAI_API_KEY`、`DASHSCOPE_API_KEY`、`SUBTITLE_GPT_TOKEN`、worker key 与网关 key。

## 上海：poller + fetcher

```bash
mkdir -p /root/subtitle
cp poll_shanghai.py fetch_shanghai.py /root/subtitle/
chmod 700 /root/subtitle
# /root/subtitle/secrets.env
# SUBTITLE_PLATFORM_TOKEN=...
# SUBTITLE_PLATFORM_GATEWAY_KEY=...
# SUBTITLE_INGEST_TOKEN=...
chmod 600 /root/subtitle/secrets.env
systemctl enable --now subtitle-poller
```

- 需要 `python3`、`ffmpeg`、`curl`；
- `poll_shanghai.py` 带文件锁，systemd `Restart=always`；
- 成功后 fetcher 删除 `/root/subtitle-work/<job>`；另建议每 30 分钟清理 2 小时以上残留。

## systemd 示例

见 `systemd/subtitle-ingest.service` 与 `systemd/subtitle-poller.service`。

## 运维命令

```bash
# 上海
journalctl -u subtitle-poller -f
systemctl restart subtitle-poller
ls -la /root/subtitle-work

# 硅谷
docker logs -f subtitle-ingest
docker restart subtitle-ingest
```

## 密钥更换

1. 修改硅谷 `$BASE/env`；
2. `docker rm -f subtitle-ingest` 后按上面的 `docker run` 重新创建（env 在创建时读取）；
3. 上海只改 `/root/subtitle/secrets.env` 并 `systemctl restart subtitle-poller`。

## 注意

- 上海的 fetcher 会 claim job（写入 45 分钟租约）；传输必须在租约内完成。
- 硅谷 ingest 复用同一个 claim，不重新领取，因此不会打断租约。
- 音频上传使用 HTTP + 256 位 token，且防火墙只允许上海 IP；如果链路要跨公网长期使用，建议在前面加 TLS 反向代理。
