# 企业知识问答 Agent

一个以企业资料为唯一事实来源、可展示原文引用和执行 Trace 的知识问答 Agent。

## 当前阶段

核心链路、六类必验场景、两类扩展真实模型路径、规定文档和独立 venv 全量验证均已完成。代码与评审材料已齐；本地 Git 元数据已存在，但还没有首个 commit 或 remote。正式提交只差题号、首个 commit、GitHub URL、最终 commit SHA，以及私有仓库可能需要的评审账号只读权限。按当前要求尚未执行这些 Git/GitHub 交付动作。

## 环境

- Python 3.11+
- 离线模式不需要 API Key
- 核心运行依赖：无第三方依赖

## 安装

项目的离线模式没有第三方运行依赖。推荐在全新虚拟环境中直接设置源码路径，不需要 pip 或网络：

```powershell
python -m venv .venv --without-pip
.\.venv\Scripts\Activate.ps1
$env:PYTHONPATH = (Resolve-Path "src").Path
```

随后即可运行测试和 CLI。关闭当前 PowerShell 后需要重新设置 `PYTHONPATH`。

可选：如果环境能够获取 `setuptools` 构建依赖，可以安装 editable 包并使用 `enterprise-qa` 命令：

```powershell
python -m pip install -e .
enterprise-qa health --json
```

## 运行

```powershell
python -m enterprise_qa_agent.cli ask --question "忘记密码怎么重置？" --json
python -m enterprise_qa_agent.cli ask --question "报销期限多久？" --json
```

带历史记录：

```powershell
python -m enterprise_qa_agent.cli ask --question "那试用期员工呢？" --history examples/inputs/context-history.json --json
```

按部门权限检索：

```powershell
python -m enterprise_qa_agent.cli ask --question "年度薪酬调整需要什么审批？" --department hr --json
python -m enterprise_qa_agent.cli ask --question "年度薪酬调整需要什么审批？" --department sales --json
```

文档必须显式声明 `access_control.departments`。`all` 表示所有部门可读；缺失 ACL 的文档会被拒绝加载。部门过滤发生在相关度计算之前，模型不能通过动作参数修改请求者权限。

按指定日期核验当时有效的规则：

```powershell
python -m enterprise_qa_agent.cli ask --question "报销期限多久？" --as-of-date 2026-11-06 --json
```

文档必须声明 `effective_from`，可用 `effective_to` 表示有效期结束日；起止日均包含在有效期内。检索会在相关度计算前排除尚未生效和已经失效的文档。默认基准日为运行当天，`--as-of-date` 由执行器作为可信请求上下文注入，模型动作不能修改。

持续工具故障演示：

```powershell
python -m enterprise_qa_agent.cli ask --question "忘记密码怎么重置？" --fail-tool search_docs --fail-count 2 --json
```

健康检查：

```powershell
python -m enterprise_qa_agent.cli health --json
python -m enterprise_qa_agent.cli --version
```

## 测试

安装项目或设置 `PYTHONPATH=src` 后运行：

```powershell
python -m unittest discover -s tests -v
```

当前结果：69 项测试通过（最近一次全量运行 `Ran 69 tests in 0.612s`）。

## 评审入口

建议按以下顺序检查：

1. 运行明确答案：

   ```powershell
   python -m enterprise_qa_agent.cli ask --question "忘记密码怎么重置？" --json
   ```

2. 运行冲突答案：

   ```powershell
   python -m enterprise_qa_agent.cli ask --question "报销期限多久？" --json
   ```

3. 查看不同工具观察如何改变动作：

   - [明确答案 Trace](examples/traces/clear-answer-trace.json)
   - [冲突 Trace](examples/traces/conflict-trace.json)
   - [资料外 Trace](examples/traces/out-of-scope-trace.json)
   - [工具失败 Trace](examples/traces/tool-failure-trace.json)

4. 运行全部测试，重点关注资料替换、提前完成阻断和伪造引用测试。

## 输出字段

- `status`：任务状态。
- `answer`：回答、追问或失败说明。
- `claims`：有引用绑定的事实声明。
- `citations`：文档 ID、版本日期、有效期、片段 ID 和逐字原文。
- `support_certificates`：每条成功声明的逐句证据映射、证明方式和规范化结果。
- `gaps`：资料缺口。
- `conflicts`：冲突值和裁决依据。
- `trace`：每步动作、参数、观察摘要、后续动作和行动依据。

成功回答还会执行语义事实校验：自然日、工作日、月、年、金额和百分比必须与引用原文一致。例如“30天”可等价表述为“30日”，但不能改写成“30个工作日”。

成功状态采用 fail-closed 的逐句支持证明：claim 的每个分句必须逐字对应某个绑定原文分句，或只使用程序明确定义的事实等价变换（如“30天/30日”）。条件、否定、对象或动作的自由改写无法生成支持证书，会被 `unproven_semantic_clause` 阻断；附加未参与证明的引用也会被拒绝。这不声称解决通用自然语言蕴含，而是通过收紧可接受答案集合，保证所有 `completed` 声明均可确定性复核。

冲突检测使用相同的事实规范化，避免把“30天/30日”“1万元/10000元”等等价写法误判为冲突；同时区分允许、禁止、必须、无需等规则立场，可识别“可以申请/原则上不予安排”这类间接矛盾。无法可靠拆分的多条款或复杂语义保持未自动判定，不擅自裁决。

特殊条款必须通过 `rule_overrides` 显式绑定源片段、被覆盖片段、规则键和依据。系统还会验证特例适用范围是通用规则的严格子集；只有问题明确命中特例范围时才自动采用特例，否则保留双方引用并请求确认。数字优先级只在适用范围相同时参与裁决。

完成守卫还会检查最终声明之间及单条声明前后分句的一致性。同一规则同时出现“可以/不能”或不同期限等互斥结论时返回 `answer_self_contradiction` 并打回调整；明确写出不同员工、地区或业务范围的差异规则不会被误判。

跨章节、跨文档规则通过 `required_evidence_refs: [{document_id, chunk_id}]` 建立显式证据链；旧 `required_chunk_ids` 会按当前文档兼容解析。检索命中主条款后递归加入当前请求可见且有效的关联证据；完成前必须补读，最终 claims 必须引用完整证据链。全知识库范围内的不存在、自引用和跨文档循环会在加载阶段被拒绝。依赖目标无权限或尚未生效时返回 `insufficient_evidence`，输出和 Trace 不泄漏目标文档 ID。

其他完成守卫与运行能力：

- 冲突清单逐条显示员工、地区和业务适用范围。
- 达到执行上限时保留已读引用和冲突，并明确标为非最终结果。
- 可重试工具错误使用指数退避，默认从 0.25 秒开始并封顶 2 秒。
- 离线 Mock 与真实模型共同使用原生风格 `tool_call` 信封；真实模式声明四个函数 schema。
- 长对话使用“最近 6 条原文 + 最多 6 条相关旧消息原文”的情景记忆检索；旧消息携带原始序号和 SHA-256，不再生成不可溯源摘要，Trace 记录召回索引。
- 成功回答默认最多 6 条 claim、单条 240 字符、总事实文本 800 字符。

记录反馈：

```powershell
python -m enterprise_qa_agent.cli ask --question "忘记密码怎么重置？" --json --output runtime/result.json
python -m enterprise_qa_agent.cli feedback --result runtime/result.json --rating unhelpful --reason citation_quality --comment "引用范围不够明确" --idempotency-key review-001 --json
python -m enterprise_qa_agent.cli feedback-summary --json
```

反馈默认事务写入 `runtime/feedback.sqlite3`，按 `run_id` 关联并自动归因为工具故障、知识缺口、未裁决冲突、执行预算、验证/生成失败或答案质量。数据库包含版本化 schema，以及运行 ID、归因类别、评分和时间索引；调用方可用 `--idempotency-key` 安全重试。`feedback-summary` 输出评分、状态、归因和时间范围统计。

迁移旧 JSONL：

```powershell
python -m enterprise_qa_agent.cli feedback-migrate --source-jsonl examples/feedback/sample-feedback.jsonl --json
```

迁移先校验全部记录，再在事务中批量写入；坏行不会造成部分导入，重复 `feedback_id` 会计入 `duplicates`。只有显式指定 `feedback --format jsonl` 时才使用兼容 JSONL 导出。`runtime/` 默认不进入版本控制。

合成反馈记录与 SQLite 验证结果：[sample-feedback.jsonl](examples/feedback/sample-feedback.jsonl)、[sqlite-validation.json](examples/feedback/sqlite-validation.json)。

## 设计入口

- [技术选型](docs/00-technical-selection.md)
- [需求说明](docs/01-requirements.md)
- [总体方案](docs/02-solution.md)
- [评估报告](docs/03-evaluation.md)
- [AI 使用说明](docs/04-ai-usage.md)
- [项目复盘](docs/05-retrospective.md)
- [详细设计](docs/06-detailed-design.md)
- [全量验证记录](docs/07-validation.md)
- [最终状态与提交清单](docs/08-final-status.md)

## Mock 边界

默认离线 Mock 根据搜索结果、已读证据、冲突和工具错误动态选择下一动作，用于验证动作循环、工具调用、失败处理、状态和引用校验。Mock 只验证工程流程，不证明真实模型的决策效果。

## 真实模型配置

复制 `.env.example` 为 `.env`，配置以下变量后使用 `--mode live`：

- `KNOWLEDGE_QA_API_KEY`
- `KNOWLEDGE_QA_BASE_URL`
- `KNOWLEDGE_QA_MODEL`

也兼容现有 DeepSeek 环境变量：`DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL`、`DEEPSEEK_MODEL`。项目不会把密钥写入输出或 Trace，`.env` 默认不进入版本控制。

已使用 DeepSeek `deepseek-chat` 实际运行六类必验场景，并补充长对话与跨文档证据两类扩展路径，共 8 条。输出与 Trace 位于：

- [明确答案](examples/outputs/live-clear-answer.json) / [Trace](examples/traces/live-clear-answer-trace.json)
- [文档冲突](examples/outputs/live-conflict.json) / [Trace](examples/traces/live-conflict-trace.json)
- [资料外问题](examples/outputs/live-out-of-scope.json) / [Trace](examples/traces/live-out-of-scope-trace.json)
- [改写问题](examples/outputs/live-paraphrase.json) / [Trace](examples/traces/live-paraphrase-trace.json)
- [上下文追问](examples/outputs/live-context-follow-up.json) / [Trace](examples/traces/live-context-follow-up-trace.json)
- [长对话上下文追问](examples/outputs/live-long-context-follow-up.json) / [Trace](examples/traces/live-long-context-follow-up-trace.json)
- [跨文档证据链](examples/outputs/live-cross-document-dependency.json) / [Trace](examples/traces/live-cross-document-dependency-trace.json)
- [工具失败](examples/outputs/live-tool-failure.json) / [Trace](examples/traces/live-tool-failure-trace.json)

8 条路径均达到预期状态。该结果证明真实模型接入和这些合成场景下的动作调整可运行，不代表开放领域或生产数据上的广泛质量结论。
