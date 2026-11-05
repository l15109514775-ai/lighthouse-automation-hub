# ② QQ 群聊机器人

两个 AstrBot 插件，一个负责"私聊查任务"，一个负责"群里收通知"。它们都不直连数据库，只调用网站的 RPC 网关，所以权限、频率和可见范围都由数据库函数控制。

| 插件 | 目录 | 作用 |
| --- | --- | --- |
| `subtitle_bridge` | `subtitle-platform/bot/plugins/astrbot_plugin_subtitle_bridge/` | 私聊：账号绑定、项目/任务查询 |
| `subtitle_notify` | `subtitle-platform/bot/plugins/astrbot_plugin_subtitle_notify/` | 群/私聊：新申请提醒、审批指令、云端结果通知、清理 |

## 接入什么服务

- **AstrBot**（机器人框架），使用 `qq_official` 平台适配器（QQ 开放平台机器人）。
- 网站的 **CloudBase RPC 网关**：`https://<环境ID>.api.tcloudbasegateway.com`。
- 机器人只持有 **Publishable Key（anon）**，不是管理密钥；绑定关系由 `st_qq_dispatch` 校验。

## 配置

```bash
# 在服务器上，从生成的部署包里运行（只写本地文件）
python subtitle-platform/tools/configure_bot.py --deployment <部署目录> --data /opt/subtitlebot/data
```

交互输入：QQ AppID、AstrBot 平台实例 ID、网站组长的用户 ID、Publishable Key。脚本会：

1. 写 `subtitle_bridge.json`（权限 0600，含 `platform_id`、`publishable_key`、`gateway_key`）；
2. 生成 `REGISTER_GATEWAY.sql`（只含 `gateway_key` 的 SHA-256，不含明文），由你在 CloudBase SQL 编辑器执行；
3. 把两个插件复制到 `data/plugins/`。

已有配置/插件时脚本会拒绝覆盖，更新插件请走维护流程。

## 私聊命令（以 `bridge.py` 为准）

| 发送 | 机器人回复 | 后台发生什么 |
| --- | --- | --- |
| `字幕帮助` | 指令列表 + 网站地址 | 无写库 |
| `菜单` / `帮助` / `help` | 功能菜单（群聊也可用） | 无写库 |
| `字幕绑定` | 绑定码（5 分钟有效）+ 提示去网站确认 | `st_qq_dispatch` 生成一次性绑定码 |
| `字幕账号` | `已绑定网站成员：<昵称>` / `当前未绑定网站账号` | 查询绑定关系 |
| `字幕项目` | 可访问项目（最多 20 项，标题 + ID） | 查询项目权限 |
| `我的任务` | 我的任务列表（标题 + 状态 + 项目 + ID） | 查询任务 |
| `待领任务` | 可认领任务列表 | 查询任务 |
| `任务详情 <完整任务ID>` | 任务标题、状态、项目、说明（最多 1000 字） | 查询单任务 |
| `字幕解绑 确认` | 解除提示 | `st_qq_link_remove` |

规则（代码里写死的）：

- **除菜单外全部要求私聊**；群里发 `字幕绑定` 会回复"请私聊本机器人"。
- 平台必须是 `qq_official`，且 `platform_id` 与配置一致。
- 同一个人上一次查询未完成时，再发会提示"上一次查询还在处理中"。
- 回复超过 1800 字会截断并提示去网站看。

## 群内命令（`subtitle_notify`）

| 发送（在目标群 @机器人） | 回复 | 作用 |
| --- | --- | --- |
| `字幕通知绑定 <32位绑定码>` | 已把当前群设为提醒群 | 绑定群；绑定码由组长私聊生成 |
| `字幕通知设置 群聊` / `私聊` / `双通道` | 设置结果 | 仅组长私聊可用 |
| `字幕通知状态` | 已发/待发/待核对数量 | 仅组长私聊 |
| `字幕通知关闭` | 已停止自动提醒 | 仅组长私聊 |

## 一段可照做的群聊示例

1. 组长私聊机器人：`字幕通知设置 双通道` → 机器人回复设置成功并给一次性绑定码。
2. 组长在 **目标群** @机器人：`字幕通知绑定 a1b2c3…`（32 位）→ 机器人回复"已将当前群设为字幕申请提醒群"。
3. 组员 **小灯** 私聊机器人：`字幕绑定` → 机器人回复绑定码 + 网站地址。
4. 小灯打开网站 →「QQ 账号绑定」→ 粘贴绑定码 → 确认。
   → 数据库 `st_qq_link_confirm` 写入绑定；机器人之后 `字幕账号` 回复"已绑定网站成员：小灯"。
5. 小灯私聊：`待领任务` → 返回可认领任务与 ID；`任务详情 <ID>` → 返回说明。
6. 小灯在网站认领并交付后，群里会收到"新申请/待审核"提醒（由 `subtitle_notify` 轮询 `st_qq_notification_feed` 发出）。

**只在群内完成的操作**：群的提醒绑定（第 2 步）必须在目标群里发绑定码，私聊无法完成；其余查询与绑定确认都在私聊/网站完成。

## 离线验证

```bash
cd subtitle-platform
python -m unittest discover -s tests/bridge -p 'test_*.py'         # 9 项
python -m unittest discover -s tests/notifications -p 'test_*.py'  # 64 项
```

这些测试使用假网关与固定命令文本，不连接真实 QQ；真实收发需要 AstrBot + QQ 开放平台机器人。

## 常见故障

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 私聊没反应 | 平台不是 `qq_official`，或 `platform_id` 不匹配 | 检查 `subtitle_bridge.json` 与 AstrBot 平台实例 ID |
| 回复"网站连接暂不可用" | gateway 地址/Key 错，或 RPC 未部署 | 用 `configure.py` 重新生成，确认 `st_qq_dispatch` 存在 |
| 绑定码提示无效/过期 | 超过 5 分钟或已被使用 | 重新发 `字幕绑定` |
| 群里收不到通知 | 群未绑定，或机器人无主动消息权限 | 重新走群绑定；检查机器人权限 |
| 通知重复/漏发 | 账本文件丢失 | 不要清零重发；按 `subtitle-platform/docs/维护与排错.md` 恢复账本 |
