# Experiment Integrity Substrate

> 文件路径：`protocols/EXPERIMENT_SUBSTRATE.md`

> 文件说明：本文件定义 Native/B public release 的实验完整性边界；它不是任一能力组件，也不向 solver 提供领域答案。

所有条件必须保持同一公开题目、公开 options、官方数据、模型、reasoning effort、环境、timeout、公共资源政策、并发策略和 candidate policy。generation 阶段不得访问 evaluator、private reference、answer key、历史预测、其他 workspace 或 benchmark solution；评分只能在 generation freeze 后进行。

## Condition package boundary

本 public solver release 只携带 `Native`、`A` 和 `B`，分别位于
`conditions/native/`、`conditions/a/` 和 `conditions/b/`。每个 public
condition 可以 import `common/`，但不得 import scoring/private code，亦不得
依赖 archive 外部的实现。
`common/` 仅能提供题目加载、workspace、环境、hash、freeze、schema validation、
answer-blind isolation 与 post-hoc scoring；不得包含 capsule workflow、科学状态、
domain grounding、reflection、repair 或 multihead 能力。

每个 capsule/arm 使用独立 workspace 和 context。任何真正的私有访问、数据暴露不匹配、workspace escape 或跨 arm 信息流都是 hard fail。所有 solver-visible prompt、代码、schema、public question object、official-data hash、environment 和 event log 都必须记录 SHA256/provenance。

每个执行单元必须在自己的 workspace 下拥有独立 `tmp/`，并由 controller
将 `TMPDIR`、`TMP`、`TEMP` 及科学计算缓存变量绑定到该目录。B 的执行单元
是 capsule attempt；Native 的执行单元是 question attempt。共享 `/tmp`、
`/var/tmp` 或其他执行单元的临时目录一律禁止。

## Executable-source persistence

所有条件中为任务编写的程序、查询、notebook cell 导出或多行 shell/R/Python
片段，必须在执行前以完整、可运行的源文件落盘在该 capsule 的
`analysis/scripts/` 下。不得只通过 heredoc、`-c`/`-e` 参数、临时文件或
交互会话执行任务专用代码。执行记录必须关联源文件路径、用途、运行命令及
SHA256；generation freeze 必须包含这些源文件的哈希。普通的一行文件检查或
不含任务专用程序源码的标准 CLI 调用不受此限制。

## File self-description and non-destructive annotation

每个新建的可注释文本文件（Python、R、shell、Markdown 等）必须在文件开头
写明其仓库相对路径与用途。JSON、JSONL、日志、二进制、压缩文件、数据库及
冻结产物不得为了加入注释而改变字节；它们必须由实验根目录的
`FILE_ANNOTATIONS.md` 提供逐文件路径与用途注释，并由所在 run 的 manifest
关联。历史 source snapshot 和已冻结 run 同样只允许通过该外部索引注释，不能
回写修改原文件或其哈希。
