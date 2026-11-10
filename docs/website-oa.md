# ① 字幕组网站与 OA 协作平台

一个静态网站 + CloudBase PostgreSQL（RDB）+ RPC gateway。它回答的是"组里的事怎么算数"：谁申请、谁认领、谁交付、谁审核、积分从哪来，全部落库可追溯。

## 用一个任务走完整个流程

假设组员 **小灯** 想接一个新番字幕任务。

| 步骤 | 谁操作 | 在哪里 | 看到什么 / 做什么 | 留下什么记录 |
| --- | --- | --- | --- | --- |
| 1. 注册 | 小灯 | 网站登录页 →「邮箱注册 / 验证码登录」 | 填邮箱，收验证码，设用户名密码 | 身份认证用户（CloudBase），尚未获得项目权限 |
| 2. 申请入组 | 小灯 | 「成员申请」 | 填组内昵称，提交 | `st_apply_membership` 写入申请 |
| 3. 审批 | 组长 | 「成员管理」 | 看到待审批列表，选项目与初始身份（成员 / 审核 / 负责人） | `st_decide_member` + `st_assign_member`，写入成员与项目角色 |
| 4. 建项目 | 组长或正式成员 | 「未认领项目」→「新建项目」 | 项目名称、说明；创建者成为项目负责人 | `st_create_project`，写入项目与 `lead` 角色 |
| 5. 发任务 | 项目负责人 | 当前项目 →「发起任务」 | 任务标题、类型（字幕 / 视频 / 封面）、截止时间、说明 | `st_create_task`，任务状态 `open` |
| 6. 认领 | 小灯 | 「全体未认领任务」或项目任务列表 | 点「认领」；首位认领者成为项目负责人（若项目尚无负责人） | `st_claim_task`，状态 `open → claimed`，负责人字段写入 |
| 7. 交付 | 小灯 | 任务详情 → 上传交付物 | 上传字幕/视频/封面，填交付说明 | `st_upload_begin(_chunked)` / `st_upload_finish` + `st_submit`，状态 `submitted`，生成交付版本记录 |
| 8. 审核 | 审核者 | 右侧「待审核队列」 | 看交付版本、来源与权利证据，选通过或退回 | `st_review`，状态 `approved` 或退回 `claimed`，写审核记录与贡献 |
| 9. 积分 | 系统 / 组长 | 「贡献与积分」 | 汇总审核通过的贡献；组长可手工调整 | `st_credit_report`、`st_credit_adjust` |
| 10. 归档 | 负责人 | 项目/任务操作 | 删除或归档（软删除，可恢复） | `st_delete_task` / `st_delete_project` / `st_restore_*`，审计保留 |

关键点：

- **只有审核通过才算贡献**（`st_review` 触发 `st_validate_contribution` / `st_validate_points`）。
- **删除是软删除**，回收站可恢复，物理删除需要单独授权（`043_retention_and_purge.sql`）。
- **QC / 交付版本** 保留历史，不覆盖（`st_validate_review` 约束）。

## 源码与依赖

| 部分 | 目录 | 说明 |
| --- | --- | --- |
| 前端 | `subtitle-platform/site/` | `index.html` + `app.js`（登录、任务板、审核）、`project-workflow.js`（项目/认领）、`credits.enhanced-*.js`（积分）、`qq-binding.*.js`（QQ 绑定码） |
| 数据库 | `subtitle-platform/database/migrations/` | 目录包含至 060 的 SQL；首次安装清单目前仅登记至 049，后续依赖尚需单独核实 |
| 部署配置 | `subtitle-platform/tools/configure.py` | 生成独立部署目录，替换示例域名/环境 ID |
| 测试 | `subtitle-platform/tests/` | 数据库（pglite）+ 前端离线测试 |
| 文档 | `subtitle-platform/docs/从零搭建.md`、`使用手册.md`、`架构与数据.md` | 面向非程序员的逐步搭建 |

依赖：Node.js ≥ 22（只用于测试与工具）、Python ≥ 3.11（配置工具）、一个 CloudBase PostgreSQL 环境。

## 最小配置

```bash
cd subtitle-platform
cp config.example.json config.local.json   # 只改本地文件，别提交
# 编辑 config.local.json：environment_id / website_url / cloud_environment_name / text_api_url / text_model / owner_auth_subject
python tools/configure.py --config config.local.json --out ../my-deployment
```

`configure.py` 只做文本替换并写本地文件，**不连接任何服务**；输出目录已存在会拒绝覆盖。

生成的首次安装 SQL 不代表已覆盖目录中的全部后续迁移。现有生产库不能直接重跑 `INSTALL_ALL.sql`；新环境也必须先核对 API Worker 所需的迁移依赖，不能把生成成功当作数据库安装验收通过。

## 本地访问入口

```bash
# 1) 纯前端预览（没有数据，只用来看界面）
python -m http.server 8080 --directory subtitle-platform/site
# 打开 http://127.0.0.1:8080

# 2) 数据库与前端离线测试（不连云、不花钱）
cd subtitle-platform
npm ci
npm test
python tools/check_release.py
```

预期结果：

```
ℹ tests 42
ℹ pass 42
ℹ fail 0
19 registration feedback cases passed; no real authentication requests.
PASS 237 source files: release content, common credential patterns, Python/JSON syntax and maintained document links
```

真正登录、建项目、上传交付物需要 CloudBase 环境按 [`从零搭建.md`](../subtitle-platform/docs/从零搭建.md) 配置数据库与身份认证；本文不重复那套步骤。

## 常见故障与恢复

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 页面能开，但登录/列表一直转圈 | `index.html` 的 CSP `connect-src` 还是示例 gateway | 用 `configure.py` 生成部署包，换成你的环境域名 |
| 报 `function st_xxx does not exist` | 迁移没按顺序执行 | 按 `database/migrations/` 编号 001→049 重跑缺失的迁移 |
| 新成员登录后看不到项目 | 只完成注册，未审批 | 组长在「成员管理」审批并分配项目角色 |
| 交付上传失败 | 存储桶/权限未配好 | 检查 CloudBase 存储与 `st_storage_*` 函数；错误信息会指明桶或函数 |
| 误删任务 | 软删除 | 「回收站」恢复；物理删除另需保留授权 |
