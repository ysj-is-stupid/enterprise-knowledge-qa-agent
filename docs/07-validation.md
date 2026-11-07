# 企业知识问答 Agent：全量验证记录

## 1. 验证范围

本次最新验证于 2026-11-07 进行，覆盖：

- 全新虚拟环境运行。
- README 离线运行步骤。
- Python 包导入位置。
- 全部自动化测试。
- 六类 CLI 验收路径。
- 真实 DeepSeek 网络调用的六类必验路径，以及长对话、跨文档证据两类扩展路径，共 8 条。
- 执行上限。
- 生成示例与原文引用。
- 必需目录和文档。
- 敏感信息扫描。

按提交者要求，本次未执行 Git commit、远端仓库创建或推送。

## 2. 独立环境

验证解释器：

```text
Python 3.13.9
prefix=F:\AI_PJ\Three\.venv
base_prefix=F:\CONDA
package=F:\AI_PJ\Three\src\enterprise_qa_agent\__init__.py
```

虚拟环境包列表只有：

```text
pip==25.2
```

项目没有安装或引用其他第三方运行包。设置 `PYTHONPATH=src` 后，包从项目源码目录加载。

## 3. 安装步骤发现与修正

原 README 使用：

```powershell
python -m pip install -e . --no-build-isolation
```

全量验证发现 Python 3.13 的新 venv 不预装 setuptools。`--no-build-isolation` 又禁止 pip 获取 `pyproject.toml` 声明的构建后端，因此 editable 安装无法导入 `setuptools.build_meta`。

修正后的首选离线步骤为：

```powershell
python -m venv .venv --without-pip
.\.venv\Scripts\Activate.ps1
$env:PYTHONPATH = (Resolve-Path "src").Path
```

该方式不需要 pip、setuptools、网络或 API Key。editable 安装保留为能够获取构建依赖时的可选方式。

受控执行环境还限制 Python 在系统临时目录写文件，导致初次 `ensurepip` 和 pip 构建失败。获得临时目录写入权限后确认失败原因是 setuptools 缺失；最终离线运行验证不依赖这项权限。

## 4. 测试结果

在独立 venv 中运行：

```powershell
$env:PYTHONPATH = (Resolve-Path "src").Path
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

结果：

```text
Ran 69 tests in 0.612s
OK
```

完整输出：[examples/test-results.txt](../examples/test-results.txt)

## 5. 六类 CLI 结果

### 明确答案

- 状态：`completed`
- 引用：1 条
- 工具调用：2 次
- 动作：`search_docs → read_doc → finish`

### 文档冲突

- 状态：`needs_clarification`
- 引用：2 条
- 工具调用：3 次
- 动作：`search_docs → read_doc → read_doc → ask_user`

### 资料外问题

- 状态：`insufficient_evidence`
- 引用：0 条
- 工具调用：2 次
- 动作：`search_docs → search_docs → finish`

### 相似问题改写

- 状态：`completed`
- 引用：1 条
- 工具调用：2 次
- 动作：`search_docs → read_doc → finish`

### 上下文追问

- 状态：`completed`
- 引用：1 条
- 工具调用：2 次
- 动作：`search_docs → read_doc → finish`

### 工具失败

- 状态：`tool_error`
- 引用：0 条
- 工具调用：2 次
- 动作：`search_docs → search_docs → finish`

### 跨部门权限

- HR：`completed`，引用 D06。
- Sales：`insufficient_evidence`，无引用，Trace 不包含 D06。
- 缺少 ACL 的文档：加载失败。

### 规则有效期

- 基准日 2026-11-06：只使用当前有效的 D01，回答 30 天。
- 基准日 2027-01-01：D01 已失效、D02 生效，只使用 D02，回答 15 天。
- 未生效或已失效文档不会出现在候选、引用和 Trace 中。
- 缺少 `effective_from` 或有效期倒置：知识库加载失败。

### 冲突语义归一化

- “30天/30日”：规范化后语义一致，不产生冲突。
- “可以申请/原则上不予安排”：识别为允许与禁止的对立，返回冲突追问。
- 原报销冲突仍识别为“期限：30自然日/15自然日”，并保留双方原文引用。

### 特例覆盖安全裁决

- 明确特例关系且问题命中“试用期员工”：选择特例 15 天规则。
- 问题未说明员工类型：保留 30 天/15 天冲突并追问。
- 特例范围不是严格子集：知识库拒绝加载。
- 不同适用范围只有数值优先级：不得自动选边。

### 回答自相矛盾

- 两条声明对同一规则分别声称“可以/不能”：完成动作被阻断。
- 单条声明前后写“可以……但不能……”：完成动作被阻断。
- 明确区分“正式员工/试用期员工”的差异规则：不误报自相矛盾。

### 跨片段与跨文档证据完整性

- 排名落在文本匹配前三之外的关联资格章节：通过依赖链加入候选。
- 模型只读主条款提前完成：守卫要求补读关联原文。
- 已读关联原文但最终遗漏引用：完成动作被阻断。
- 不存在、自引用或循环依赖：知识库拒绝加载。
- 跨文档 D20→D21：依赖目标自动加入候选，Agent 读取并引用两份文档。
- 依赖目标无权限或未生效：返回 `insufficient_evidence`，结果和 Trace 不出现目标文档 ID。
- 跨文档缺失目标或循环：知识库拒绝加载。

### 其余加固项

- 冲突规则逐条展示员工、地区和业务适用范围。
- 可重试故障采用指数退避并记录 Trace。
- 离线与真实模型适配器使用统一工具调用信封。
- 长历史不再压缩为摘要；相关旧消息以原文、序号和哈希形式召回，越过无关话题后仍能完成上下文追问。
- 超长和重复回答被长度守卫阻断。
- 高相似度条件篡改和引用洗白被逐句证明守卫阻断；成功回答输出支持证书。
- 反馈按 `run_id` 自动归因并事务写入 SQLite；验证 schema、索引、幂等重试、聚合统计和 JSONL 原子迁移。

六类业务结果及部门权限结果与 `examples/outputs/` 中保存的材料一致。

## 5.1 真实模型结果

使用本地 `.env` 配置的 DeepSeek `deepseek-chat` 运行六类必验合成场景，全部达到预期状态：

- 明确答案：`completed`，引用 D05。
- 文档冲突：`needs_clarification`，引用 D01、D02 并请求确认适用制度。
- 资料外问题：首次提前结束被守卫拒绝，补检索后返回 `insufficient_evidence`。
- 相似问题：`completed`，引用 D04。
- 上下文追问：恢复远程办公主题，`completed`，引用 D03。
- 工具失败：两次搜索失败后返回 `tool_error`，无声明与引用。

对应文件采用 `examples/outputs/live-*.json` 与 `examples/traces/live-*-trace.json` 命名。实测同时验证了 DeepSeek 直接 `finish` 参数的归一化，以及并行 `read_doc` 调用在单动作 ReAct 循环中的安全串行化。

额外长对话路径把远程办公主题放在最近窗口之外并插入三个无关主题；记忆层召回旧消息索引 0，DeepSeek 正确恢复远程办公独立查询，输出和 Trace 分别为 `live-long-context-follow-up.json` 与 `live-long-context-follow-up-trace.json`。

真实跨文档路径使用 D20 主条款依赖 D21 资格基线，DeepSeek 执行 `search_docs → read_doc(D20) → read_doc(D21) → finish`，引用两份原文并生成两份支持证书。

## 6. 执行上限

使用 `--max-tool-calls 1` 运行明确答案问题，结果为：

```text
status=max_steps_exceeded
tool_calls=1
claims=[]
citations=[]
```

系统明确报告“达到工具调用次数上限，任务未完成”，没有在缺少原文的情况下继续回答。

若上限触发前已经读取原文，结果会保留这些引用和已检测冲突，同时保持 claims 为空并明确标注为非最终中间信息。

## 7. 示例与引用验证

自动化测试逐项确认：

- 六份输出均为有效 JSON。
- 每份输出的状态与动作序列符合预期。
- 独立 Trace 文件与输出内嵌 Trace 完全相同。
- 每条引用的文档 ID 和片段 ID 均存在。
- 每条引用文本与 `data/documents.json` 中的当前原文逐字一致。
- 每条事实声明引用的 `C` 编号均存在。
- 每条 `completed` 声明都有逐句支持证书，证书引用映射到实际原文。

## 8. 结构验证

以下必需内容全部存在：

- `README.md`
- `pyproject.toml`
- `requirements.txt`
- `.env.example`
- `src/`
- `tests/`
- `examples/`
- `data/`
- `docs/01-requirements.md`
- `docs/02-solution.md`
- `docs/03-evaluation.md`
- `docs/04-ai-usage.md`
- `docs/05-retrospective.md`

另外提供技术选型、详细设计和本验证记录。

## 9. 敏感信息检查

排除本地 `.env`、`.venv`、`.git`、`__pycache__` 后扫描：

- 常见 `sk-` API Key。
- 非空 `KNOWLEDGE_QA_API_KEY`。
- Bearer Token。
- RSA、OpenSSH、EC 私钥头。
- 明显的明文密码赋值。

扫描无真实密钥命中。`.env.example` 中密钥为空；另使用实际内存 Key 做逐字扫描，仓库文件、示例输出和 Trace 的匹配数为 0。

## 10. 未验证内容

- 没有 Python 3.11 或 3.12 解释器，实际执行环境为 3.13.9；代码只使用 3.11 标准语法与标准库。
- 真实模型网络调用覆盖六类必验场景与两类扩展路径，共 8 条合成路径；未覆盖真实企业资料、大样本统计评估或长期稳定性压测。
- 可选 editable 安装需要可用的 setuptools 构建后端，本次离线验证采用零依赖源码路径方式。
- 未验证 Linux 或 macOS 命令。
- 按要求未验证 GitHub URL、commit SHA 或评审账号权限。

## 11. 结论

在上述边界内，全量验证通过。项目可以在独立 Python venv 中以零第三方依赖方式运行，69 项测试、离线六类必验路径、真实模型 8 条合成路径及全部加固回归均通过。
