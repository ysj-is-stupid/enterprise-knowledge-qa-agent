# 企业知识问答 Agent：评估报告

## 1. 评估范围

本报告评估离线 Mock 与真实 DeepSeek 模式下的工程行为，包括检索、原文读取、动态动作选择、冲突处理、引用校验、上下文改写、工具失败、资料替换和执行预算。

离线 Mock 根据实际工具观察选择下一动作，用于稳定验证流程和安全守卫；真实模型实际运行六类必验场景，并补充长对话与跨文档证据两类扩展路径，共 8 条。两类结果分开记录：真实运行证明模型接入和这些场景中的动作调整有效，但不能外推为开放问题或生产数据上的整体决策质量。

## 2. 运行环境

- 日期：2026-11-07
- Python：3.13.9，项目最低要求为 3.11
- 操作系统：Windows / PowerShell
- 第三方运行依赖：无
- 知识库：[data/documents.json](../data/documents.json)
- 同义词：[data/synonyms.json](../data/synonyms.json)
- 模式：`offline_mock` 与 DeepSeek `deepseek-chat` 实际网络调用

## 3. 自动化测试

运行命令：

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```

阶段 6 实际结果：

```text
Ran 69 tests in 0.612s
OK
```

完整原始测试输出：[examples/test-results.txt](../examples/test-results.txt)

测试覆盖：

- 明确答案与逐字引用。
- 未读冲突候选阻断。
- 无法裁决冲突。
- 上下文追问改写。
- 资料外补充检索。
- 口语化相似问题。
- 工具暂时失败恢复与持续失败降级。
- 替换文档后答案和引用变化。
- 工具调用上限。
- 真实模型配置缺失提示。
- 显式 `supersedes` 冲突裁决。
- 伪造证据 ID 阻断。
- 低文本支持度声明阻断。
- 高相似度条件篡改阻断、引用洗白阻断与逐句支持证书。
- 重复文档 ID 和空问题校验。
- 配置中的密钥不进入公共摘要。
- 未来生效与已经失效规则在评分前过滤。
- 有效期元数据缺失和起止日期倒置校验。
- 等价规则表述去重与间接否定冲突识别。
- 特例覆盖关系、范围命中与优先级安全约束。
- 多声明及单声明分句的回答一致性守卫。
- 跨章节及跨文档关联证据的召回、补读、完整引用、ACL/有效期过滤和防泄漏守卫。
- 冲突范围、执行上限部分结果和指数退避。
- Mock/真实模型统一工具调用协议、可溯源长对话记忆检索和回答长度。
- 反馈自动归因、事务型 SQLite 持久化、幂等重试、索引统计和 JSONL 原子迁移。
- DeepSeek 环境变量别名、直接 `finish` 参数归一化和并行工具调用串行化。

### 3.1 验收预期—实际结果对照

以下对照覆盖题目要求的六类必须验收场景。预期来自[需求说明](01-requirements.md)，实际结果来自 CLI 保存的输出与结构化 Trace。

1. **明确答案**
   - 预期：检索并读取支持问题的原文，返回 `completed`；答案包含文档 ID、版本日期和逐字引用。
   - 实际：执行 `search_docs → read_doc → finish`，返回 `completed`，引用 D05（版本日期 2026-04-01）的原文并生成逐句支持证书。
   - 材料：[输出](../examples/outputs/clear-answer.json) / [Trace](../examples/traces/clear-answer-trace.json)。
   - 结论：通过。

2. **文档冲突**
   - 预期：读取冲突双方原文，列出冲突内容和版本信息，返回 `needs_clarification`，不得擅自选择其中一份规则。
   - 实际：依次读取 D01、D02，识别 30 天与 15 天冲突；双方缺少可验证的覆盖或优先关系，系统列出原文和适用范围后请求确认。
   - 材料：[输出](../examples/outputs/conflict.json) / [Trace](../examples/traces/conflict-trace.json)。
   - 结论：通过。

3. **资料外问题**
   - 预期：至少执行一次合理的补充检索；仍无证据时返回 `insufficient_evidence`，说明资料缺口且不生成事实答案。
   - 实际：首次搜索无候选后扩展查询再次搜索，仍无结果；最终无 claims、无 citations，并明确缺少食堂菜单资料。
   - 材料：[输出](../examples/outputs/out-of-scope.json) / [Trace](../examples/traces/out-of-scope-trace.json)。
   - 结论：通过。

4. **改写后的相似问题**
   - 预期：通过混合检索、同义词或查询扩展召回正确资料，读取原文并给出引用。
   - 实际：问题“买的设备坏了怎么办”召回设备故障与质保资料 D04，执行 `search_docs → read_doc → finish` 并返回带引用答案。
   - 材料：[输出](../examples/outputs/paraphrase.json) / [Trace](../examples/traces/paraphrase-trace.json)。
   - 结论：通过。

5. **上下文追问**
   - 预期：结合历史将指代性问题改写为无歧义独立查询，再进行检索；Trace 同时保留原始问题和独立查询。
   - 实际：将“那试用期员工呢？”恢复为远程办公制度相关独立查询，召回并读取 D03，最终返回 `completed`；Trace 保存了两种查询及使用的历史索引。
   - 材料：[输出](../examples/outputs/context-follow-up.json) / [Trace](../examples/traces/context-follow-up-trace.json)。
   - 结论：通过。

6. **工具失败**
   - 预期：模型观察失败后选择有限重试；持续失败时返回 `tool_error`，说明无法核验资料并拒绝凭记忆作答。
   - 实际：两次 `search_docs` 均失败后执行 `finish(tool_error)`；最终 claims 与 citations 均为空。
   - 材料：[输出](../examples/outputs/tool-failure.json) / [Trace](../examples/traces/tool-failure-trace.json)。
   - 结论：通过。

六类场景的预期状态、实际状态和关键动作均一致。真实模型对应路径另见第 4.8 节；离线 Mock 与真实模型结论按评估边界分别陈述。

## 4. 实际运行材料

所有输出均由 CLI 实际运行生成，不是手写的预设日志。输出文件包含完整 Trace，`examples/traces/` 同时保存便于单独审查的 Trace 副本。

### 4.1 明确答案

- 输入：[clear-answer.json](../examples/inputs/clear-answer.json)
- 输出：[clear-answer.json](../examples/outputs/clear-answer.json)
- Trace：[clear-answer-trace.json](../examples/traces/clear-answer-trace.json)

实际状态：`completed`

实际路径：

```text
search_docs → read_doc(D05-C01) → finish
```

结果引用 D05（2026-04-01）原文，回答中的密码重置步骤直接来自已读取片段。

### 4.2 文档冲突

- 输入：[conflict.json](../examples/inputs/conflict.json)
- 输出：[conflict.json](../examples/outputs/conflict.json)
- Trace：[conflict-trace.json](../examples/traces/conflict-trace.json)

实际状态：`needs_clarification`

实际路径：

```text
search_docs → read_doc(D01-C01) → read_doc(D02-C01) → ask_user
```

D01 原文为 30 天，D02 原文为 15 天。二者适用范围重叠，且没有 `supersedes` 或显式优先级。系统列出双方文档 ID、版本日期和原文，没有因为 D02 日期更新而擅自选择 15 天。

### 4.3 资料外问题

- 输入：[out-of-scope.json](../examples/inputs/out-of-scope.json)
- 输出：[out-of-scope.json](../examples/outputs/out-of-scope.json)
- Trace：[out-of-scope-trace.json](../examples/traces/out-of-scope-trace.json)

实际状态：`insufficient_evidence`

实际路径：

```text
search_docs(无候选) → 扩展查询 search_docs(仍无候选) → finish
```

最终没有事实声明或引用，明确指出知识库缺少食堂菜单资料。

### 4.4 改写后的相似问题

- 输入：[paraphrase.json](../examples/inputs/paraphrase.json)
- 输出：[paraphrase.json](../examples/outputs/paraphrase.json)
- Trace：[paraphrase-trace.json](../examples/traces/paraphrase-trace.json)

实际状态：`completed`

问题使用“买的设备坏了怎么办”，资料使用“设备故障、质保、维修申请”。同义概念扩展召回 D04，随后读取原文并回答 12 个月质保与服务台申请方式。

### 4.5 上下文追问

- 当前问题：[context-follow-up.json](../examples/inputs/context-follow-up.json)
- 对话历史：[context-history.json](../examples/inputs/context-history.json)
- 输出：[context-follow-up.json](../examples/outputs/context-follow-up.json)
- Trace：[context-follow-up-trace.json](../examples/traces/context-follow-up-trace.json)

实际状态：`completed`

原始问题“那试用期员工呢？”被改写为包含“远程办公制度”和“试用期员工”的独立查询，再召回 D03。Trace 同时保留原始问题与独立查询。

### 4.6 工具失败

- 输入：[tool-failure.json](../examples/inputs/tool-failure.json)
- 输出：[tool-failure.json](../examples/outputs/tool-failure.json)
- Trace：[tool-failure-trace.json](../examples/traces/tool-failure-trace.json)

实际状态：`tool_error`

实际路径：

```text
search_docs(失败) → 模型选择重试 search_docs(再次失败) → finish(tool_error)
```

最终事实声明和引用均为空，系统明确表示无法核验资料且未凭模型记忆作答。

### 4.7 跨部门权限

- HR 输入：[acl-allowed.json](../examples/inputs/acl-allowed.json)
- HR 输出：[acl-allowed.json](../examples/outputs/acl-allowed.json)
- HR Trace：[acl-allowed-trace.json](../examples/traces/acl-allowed-trace.json)
- Sales 输入：[acl-denied.json](../examples/inputs/acl-denied.json)
- Sales 输出：[acl-denied.json](../examples/outputs/acl-denied.json)
- Sales Trace：[acl-denied-trace.json](../examples/traces/acl-denied-trace.json)

同一薪酬问题下，HR 可以读取受限文档 D06 并完成回答；Sales 在权限过滤后执行两次无结果检索，返回 `insufficient_evidence`。Sales 的引用和 Trace 均不包含 D06。文档若缺少 `access_control` 会在加载阶段被拒绝。

### 4.8 真实模型六类必验场景

使用 DeepSeek `deepseek-chat` 实际执行同一套六类必验合成场景，密钥只从本地 `.env` 读取，未写入输出或 Trace：

- 明确答案：[输出](../examples/outputs/live-clear-answer.json) / [Trace](../examples/traces/live-clear-answer-trace.json)，`completed`，`search_docs → read_doc → finish`，引用 D05。
- 文档冲突：[输出](../examples/outputs/live-conflict.json) / [Trace](../examples/traces/live-conflict-trace.json)，`needs_clarification`，读取 D01、D02 后请求确认。
- 资料外问题：[输出](../examples/outputs/live-out-of-scope.json) / [Trace](../examples/traces/live-out-of-scope-trace.json)，首次提前结束被补检索守卫拒绝，补检索后返回 `insufficient_evidence`。
- 改写问题：[输出](../examples/outputs/live-paraphrase.json) / [Trace](../examples/traces/live-paraphrase-trace.json)，`completed`，引用 D04。
- 上下文追问：[输出](../examples/outputs/live-context-follow-up.json) / [Trace](../examples/traces/live-context-follow-up-trace.json)，独立查询恢复“远程办公制度”主题并引用 D03。
- 工具失败：[输出](../examples/outputs/live-tool-failure.json) / [Trace](../examples/traces/live-tool-failure-trace.json)，两次搜索失败后返回 `tool_error`，无声明和引用。

实测发现并修复两项 OpenAI-compatible 差异：DeepSeek 有时直接返回 `finish` 参数而不包 `action/tool_call`；有时一次返回多个 `read_doc` 调用。适配器现在分别执行 `finish` 归一化，以及只执行首个工具调用、观察结果后再让模型决策的安全串行化。两项均有回归测试。

### 4.9 真实模型长对话记忆

- 输入历史：[long-context-history.json](../examples/inputs/long-context-history.json)
- 输出：[live-long-context-follow-up.json](../examples/outputs/live-long-context-follow-up.json)
- Trace：[live-long-context-follow-up-trace.json](../examples/traces/live-long-context-follow-up-trace.json)

历史共 16 条，远程办公主题位于消息索引 0，最近 6 条窗口内只有“好的、谢谢”等低信息轮次，中间还插入设备质保、密码重置和报销三个无关主题。记忆层从旧消息中召回索引 0 等原文，DeepSeek 将“那试用期员工呢？”改写为独立的远程办公问题，随后执行 `search_docs → read_doc → finish`，状态为 `completed`，引用 D03 并生成支持证书。

### 4.10 真实模型跨文档证据链

- 输入：[cross-document-dependency.json](../examples/inputs/cross-document-dependency.json)
- 知识库：[cross-document-dependencies.json](../data/cross-document-dependencies.json)
- 输出：[live-cross-document-dependency.json](../examples/outputs/live-cross-document-dependency.json)
- Trace：[live-cross-document-dependency-trace.json](../examples/traces/live-cross-document-dependency-trace.json)

D20 主条款通过 `required_evidence_refs` 指向 D21 资格基线。DeepSeek 实际执行 `search_docs → read_doc(D20) → read_doc(D21) → finish`，状态为 `completed`；最终引用 D20、D21 并生成两份逐句支持证书。

## 5. 动态决策证明

不同工具观察产生了不同后续动作：

- 唯一候选：读取 D05 后选择 `finish`。
- 两份同规则候选：读取 D01 后选择继续读取 D02，随后选择 `ask_user`。
- 无候选：选择改写查询再次 `search_docs`。
- 可重试错误：选择相同工具重试；第二次仍失败后选择 `finish(tool_error)`。

此外，`test_completion_guard_blocks_unread_conflict_candidate` 使用一个故意提前完成的测试模型。它在只读取 D01 后选择 `finish`，程序守卫返回 `unread_conflict_candidate`，模型收到新观察后改为读取 D02，最终进入冲突追问。该测试证明执行器不允许模型绕过高相关冲突资料。

## 6. 资料替换验证

`test_replacing_document_text_changes_answer_and_quote` 在内存知识库中把 D04 的质保期限从 12 个月替换为 18 个月，然后使用相同问题重新运行。

实际断言结果：

- 新状态仍为 `completed`。
- 新答案包含 18 个月。
- 新答案不再包含 12 个月。
- 新引用原文包含 18 个月。

事实值从当前原文读取，没有写死在 Mock 动作或规则元数据中。

## 7. 引用与声明校验

程序执行以下硬校验：

1. 成功回答前必须至少成功搜索和读取一次原文。
2. 每条声明必须绑定已存在的证据 ID。
3. 引用的文档 ID、版本、片段和原文由执行器从读取结果生成，模型不能直接伪造。
4. 声明中的数字、金额、日期和百分比必须出现在绑定原文中。
5. 非数字声明必须达到最低文本支持度。
6. 同规则冲突检查带内存在未读候选时不能完成。
7. 未裁决冲突存在时不能返回 `completed`。
8. 期限、金额和百分比按规范化语义事实比较；相同数值但不同单位不能通过。
9. 每个 claim 分句必须逐字对应绑定原文分句，或仅使用已定义的事实等价变换。
10. 每个引用必须实际参与至少一个分句的证明，不能用无关引用洗白声明。

伪造 `E999` 和“员工拥有无限特权”等测试声明均被拒绝，最终进入明确的未完成状态。

针对单位错位的回归测试确认：原文“30天”改写为“30个工作日”会被 `semantic_fact_mismatch` 拦截；等价写法“30日”允许通过，引用仍保持原文。

`test_high_overlap_condition_mutation_cannot_complete` 保留相同主体、动作、期限和大部分文字，只把“费用发生之日”替换为“审批通过之日”。该声明能通过旧的数字与文本重合度检查，但不能与任何原文分句形成确定性映射，因此被 `unproven_semantic_clause` 阻断。

`test_unused_evidence_cannot_launder_a_claim` 给正确原文声明附加另一条未参与证明的引用，系统返回 `unused_claim_evidence`。成功结果会输出 `support_certificates`，记录 claim 序号、实际参与证明的引用、证明方式、匹配原文分句和规范化声明。

## 8. 时间有效性验证

`test_effective_period_changes_retrieved_rule_by_request_date` 为两份报销规则设置不重叠有效期。以 2026-11-06 为基准日运行时，只召回 D01 并回答 30 天；以 2027-01-01 运行时，D01 已失效且 D02 正式生效，只召回 D02 并回答 15 天。被过滤文档不会进入 IDF、候选、引用或 Trace。

有效性基准日来自可信的 `QueryRequest.as_of_date`，CLI 默认为当天并允许评审通过 `--as-of-date` 固定日期；模型动作协议不包含该参数。另有测试确认缺少 `effective_from` 或 `effective_to` 早于 `effective_from` 时，知识库加载会失败。有效期边界按包含起止日处理。

## 9. 冲突语义归一化验证

`test_equivalent_duration_wording_is_not_a_conflict` 使用相同规则键下的“30天”和“30日”两种表述。二者被归一为相同的 30 个自然日，系统读取双方原文后正常完成，不再生成假冲突。

`test_indirect_negative_wording_is_detected_as_conflict` 比较“符合条件的员工可以申请远程办公”和“员工的远程办公申请原则上不予安排”。后者没有使用原硬规则覆盖的“不得/禁止”，新语义画像仍将双方识别为“允许/禁止”的相反立场，最终列出两份原文并返回 `needs_clarification`。

程序仅对期限、金额、比例以及明确的允许、禁止、必须、无需立场作保守比较。单个片段出现多个立场或复杂条件时不强行判定，以避免把不同适用对象误合并；这不是通用自然语言蕴含模型。

## 10. 特例覆盖安全验证

测试构造 D01 通用报销规则和 D02 试用期员工特例，并用 `rule_overrides` 显式绑定 D02-C01 覆盖 D01-C01：

- 问题为“试用期员工报销期限多久”时，查询明确命中特例范围，系统选择 D02 的 15 天规则，同时保留双方引用及裁决依据。
- 问题仅为“报销期限多久”时，虽然存在显式特例关系，但适用员工类型未知，系统返回 `needs_clarification`，不会擅自应用特例。
- 如果特例范围与通用范围相同，知识库加载失败，防止用元数据伪造特例。
- 如果资料范围不同但只有数值 `priority`，冲突保持未裁决，避免通用高优先级错误压过窄范围规则。

该机制要求显式片段关系、严格范围子集和查询范围命中同时成立，比单纯比较优先级或“范围更窄”更保守。

## 11. 回答自相矛盾验证

完成守卫会对绑定同一 `rule_key` 的最终声明进行两层检查：

- 声明之间：同时输出“员工可以申请远程办公”和“员工不能申请远程办公”时，返回 `answer_self_contradiction`，模型不能完成任务。
- 声明内部：单条声明出现“可以……但不能……”时，按转折分句识别相反规则立场并打回。

为避免误伤合法的差异规则，守卫会读取证据范围词。如果回答明确写明“正式员工可以、试用期员工不能”，两条结论的员工范围互斥，因此允许完成。测试分别覆盖上述三种路径。

## 12. 跨片段与跨文档证据完整性验证

合成 D10 把设备借用申请主条款、两个高相似流程片段和资格定义放在四个章节中。资格定义按纯文本相关度位于前三之外，但主条款通过兼容字段 `required_chunk_ids` 显式依赖该章节。

实际验证结果：

- 检索选择主条款后递归加入资格定义，关联片段不受文本匹配前三名限制。
- Agent 读取主条款后继续读取资格定义，最终答案和引用同时包含“资格定义章节”与“连续任职满6个月并完成安全培训”。
- 测试模型在只读主条款时提前完成，守卫返回 `unread_required_chunk`；模型观察后改为读取关联原文。
- 测试模型读完但只引用主条款时，守卫返回 `missing_required_citation`，不能以 `completed` 结束。
- 指向不存在片段、自引用或循环依赖会在知识库加载阶段失败。

另一组合成资料把主条款 D20 和资格基线 D21 放在不同文档，并使用 `required_evidence_refs` 建图。实际验证：

- 文本召回 D20 后递归生成 D21 候选，依赖候选带来源原因；Agent 补读两份原文并同时引用。
- D21 仅 HR 可读而请求者是 Sales 时，返回 `insufficient_evidence`；完整结果 JSON 不含 `D21`。
- D21 尚未生效时同样返回资料不足，输出不泄漏目标 ID。
- 缺失目标和 D20↔D21 跨文档循环均在知识库加载阶段拒绝。
- 模型把未裁决冲突误报成资料不足时，守卫要求改用 `ask_user`，不能用错误终止状态绕过冲突。

## 13. 其余工程改进验证

- 冲突清单：每条冲突证据都携带与证据顺序对齐的 `scope_summaries`，追问直接展示员工、地区和业务范围。
- 部分结果：步数耗尽后保留已读真实引用和已检测冲突，claims 仍为空，并明确说明不是最终答案。
- 重试退避：四次连续故障产生 0.10、0.20、0.25 秒封顶的测试延迟，Trace 可审查。
- 统一协议：离线 Mock 和原生 `tool_calls` 经过同一解析器；无效工具参数在执行前拒绝。
- 长对话：最近原文窗口保持 6 条，旧消息按当前问题检索并保留消息序号、全文和哈希；无生成摘要。相关早期主题能够越过多个无关话题被召回。
- 回答长度：超长单条或 7 条重复 claim 均被 `answer_too_long` 拦截。
- 反馈闭环：结果生成稳定 `run_id`；反馈按终止状态自动归因，默认事务写入版本化 SQLite。相同幂等键重复提交不会增加记录；可以按运行查询，并按评分、状态、归因和时间聚合。旧 JSONL 支持整批校验、事务迁移和重复计数，坏行不会部分入库。实际 CLI 结果见 [sqlite-validation.json](../examples/feedback/sqlite-validation.json)。

## 14. 评估中发现并修复的问题

### 14.1 公共词误召回

早期实现把每个查询中的最高词项分数归一为 1，导致“公司食堂明天吃什么”仅凭公共词“公司”错误召回设备质保文档。

修复：

- 改为按查询全部词项的 IDF 权重上限归一。
- 只有最高候选先达到最低阈值时，才启用同主题冲突检查带。

修复后该问题执行两次无结果检索，并正确返回 `insufficient_evidence`。

### 14.2 受控环境临时目录权限

资料替换测试最初尝试写入系统临时目录，受执行环境权限限制失败。测试改为通过 `KnowledgeBase.from_data` 构造内存资料副本。这同时减少了文件系统副作用，并使替换测试更快、更稳定。

## 15. 失败与限制

- 全量验证时，Python 3.13 的新 venv 不含 setuptools，`pip install -e . --no-build-isolation` 无法导入构建后端。README 已将零依赖的 `PYTHONPATH=src` 方式改为首选离线运行步骤；editable 安装改为可选联网方式。
- 真实模型评估覆盖六类必验场景与两类扩展路径，共 8 条合成路径；尚未使用真实企业资料、开放问题集或统计显著规模的样本。
- 离线 Mock 的指代消解和冲突语义仅覆盖合成领域，不能代表开放领域理解。
- 本地检索适用于小规模资料，不含向量召回或重排序模型。
- 系统不尝试证明任意自然语言改写；它通过只接受逐字原文分句和明确定义的事实等价变换，为 `completed` 声明生成确定性支持证书。代价是合理但自由的释义也会被保守拒绝。
- 结构化冲突检测主要覆盖期限、金额、百分比和有限的规则立场词典；反讽、跨句条件、例外嵌套无法可靠判断时保持未裁决，不允许模型自动选边。
- 特例范围命中目前使用独立查询中的明确范围词；别名、组织层级和复合身份需要后续接入标准化主体属性。
- 回答一致性检查依赖共同规则键、可结构化事实和有限立场词典；隐含前提、代词和跨段复杂逻辑仍可能漏检。
- 跨片段和跨文档关联需要资料维护者显式配置 `required_evidence_refs`（旧同文档字段兼容）；系统不会仅凭标题或相邻位置猜测依赖关系。
- 情景记忆采用有界相关性检索，不伪造或改写历史；极端长对话中的召回率仍需用更大真实对话集评估，但任何召回结果都可由消息索引和原文复核。
- 反馈已使用本地 SQLite 和 CLI 聚合查询，但未接入企业用户身份、远程数据库、高可用备份或可视化看板；反馈不会未经审核自动修改知识库。
- `as_of_date` 和请求者部门必须由可信网关或调用方注入；当前 CLI 没有实现身份认证或防篡改时间戳。
- 没有实现线上部署、监控或生产级持久化。

## 16. 结论

工程验收通过：六类必验场景及跨部门权限场景均有离线实际输出和 Trace，六类必验场景、长对话及跨文档证据路径有真实 DeepSeek 输出和 Trace，69 项自动化测试通过；原 18 项审查意见均已核对，其中真实或部分真实的问题已按风险顺序修复并形成回归测试。

本结论证明离线流程、安全约束、真实模型网络接入及 8 条合成路径已经验证；不宣称复杂开放问题或生产环境中的模型决策质量已经得到广泛验证。
