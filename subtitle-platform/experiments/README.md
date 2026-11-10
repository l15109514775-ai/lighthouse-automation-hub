# 历史实验已移出当前源码

初版 worker、未上线的 Codex Cloud dispatcher、旧诊断及专用历史测试不属于当前自托管 API Worker。它们已从当前工作树移除，未删除生产任务、数据库对象或投稿记录。

完整路径、原始内容哈希和恢复提交见 [代码清理清单](../../docs/code-cleanup-removed.json)。清理前提交为 `86e987c07ba12f18c30dbc8bb9d755adfc1eeba3`。需要研究时可从该提交读取单个文件，无需恢复整个旧部署。

当前安装入口是 [runner 部署说明](../runner/README.md)。历史 Cloud 自动启动实验不提供当前登录、授权或发布能力。
