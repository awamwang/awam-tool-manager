# P1-03 前置：`errorType` 契约清点单

> **文档性质**：架构契约清点（设计文档，不含实现代码）。**这是 team-lead 要求的 P1-03 启动前置交付物**——
> P1-03（errorType 契约修复）在本清单评审通过前不启动。
> **基线**：HEAD `4edead9`（P1-04 重构进行中，但 `backend_client.py` 不在 P1-04 改动范围，
> 故本清单的行号与结论对 P1-04 前后均成立；唯一例外是 `server.py`/`tool_specs.py` 的发射点行号会随 M03/M04 漂移，已在表中标注）。
> 采集日期：2026-10-17，全部结论均经 `grep` + 逐处上下文阅读确认。

---

## 1. 三方对账：契约声明 vs 实际实现

### 1.1 三份文档互相不一致

| 声明源 | 声明种类数 | 缺什么 |
|---|---|---|
| `SKILL.md:102-105` | **14** | 缺 `unknown_error`（代码在发，文档没列） |
| `docs/format.md:240-242` | **8** | 缺 6 种：`unknown_prompt` / `backend_busy` / `launcher_error` / `install_required` / `install_not_allowed` / `unknown_error` |
| 代码实际 emit | **15** | —— |

### 1.2 15 种取值的真实状态

| # | errorType | 代码发射点（file:line） | 在 SKILL.md？ | 在 format.md？ | 状态判定 |
|---|---|---|---|---|---|
| 1 | `not_found` | `registry.py:122`、`tool_specs.py:168` | ✅ | ✅ | **契约成立** |
| 2 | `unknown_tool` | `server.py:194` | ✅ | ✅ | **契约成立** |
| 3 | `unknown_resource` | `server.py:252` | ✅ | ✅ | **契约成立** |
| 4 | `unknown_prompt` | `server.py:366` | ✅ | ❌ | 代码与 SKILL.md 一致，**format.md 落后** |
| 5 | `invalid_params` | **无显式发射点**（仅 `errors.py:22` 的 `fail()` 默认参数值） | ✅ | ✅ | **声明了但永不到达**——全部 8 处 `fail()` 调用都显式传了 `error_type`，默认值从未被用上 |
| 6 | `connection_error` | `backend_client.py:1384` | ✅ | ✅ | **契约成立** |
| 7 | `runtime_error` | `backend_client.py:1390` | ✅ | ✅ | **契约成立** |
| 8 | `config_error` | `backend_client.py:1367`、`registry.py:130` | ✅ | ✅ | **契约成立** |
| 9 | `destructive_blocked` | `tool_specs.py:229`、`:334` | ✅ | ✅ | **契约成立** |
| 10 | `backend_busy` | `backend_client.py:1378` | ✅ | ❌ | **format.md 落后** |
| 11 | `internal_error` | `server.py:167` | ✅ | ✅ | **契约成立** |
| 12 | `launcher_error` | `tool_specs.py:310` | ✅ | ❌ | **format.md 落后** |
| 13 | `install_required` | `backend_client.py:782`（经 `:791` 的动态 `error_type`） | ✅ | ❌ | **format.md 落后** |
| 14 | `install_not_allowed` | `backend_client.py:780`（同上） | ✅ | ❌ | **format.md 落后** |
| 15 | `unknown_error` | `backend_client.py:1396` | ❌ | ❌ | **代码在发、两份文档都没列**——客户端无法预见这个值 |

**一句话结论**：`SKILL.md` 基本准确（只差 `unknown_error` 一种）；`format.md` 严重落后（缺 6 种）；
代码侧真正的问题不在「枚举漂移」，而在 **§3 的 14 个客户端可达失败站点压根不发 `errorType`**。

---

## 2. 契约卡（每种取值的触发条件 / 语义 / 客户端动作 / 差距）

> 「客户端动作」列是本次清点新增的建议内容——现有两份文档都只列了枚举值，没说每种值**意味着调用方该干什么**。
> 这是契约从「能解析」变成「能编程」的关键，建议 P1-03 一并补进 `SKILL.md`。

| errorType | 触发条件（现状） | 语义 | 客户端该干什么 | 差距 |
|---|---|---|---|---|
| `not_found` | `tool_id` 在 `data/tools/` 无对应条目，或条目校验不合格被跳过 | 目标不存在 | 检查 `tool_id` 拼写；用 `list_tools` 重新枚举 | 无 |
| `unknown_tool` | 调用了不存在的**对外工具名**（非 `tool_id`） | 本服务没这个入口 | 用 `list_tools` 看有哪些工具 | 无 |
| `unknown_resource` | `resources/read` 的 URI 不在 3 个之内 | 资源不存在 | 用 `list_resources` 枚举 | 无 |
| `unknown_prompt` | `prompts/get` 的名字不在 3 个之内 | 提示模板不存在 | 用 `list_prompts` 枚举 | 无 |
| `invalid_params` | **（现状：无发射点）** | 参数缺失/类型错/取值非法 | 按 `inputSchema` 修正参数 | **死值**。两个选择：①删掉它（承认参数校验不存在）；②P1-03 之后加参数校验时启用它。**建议②留位但文档注明「预留」** |
| `config_error` | ①`kind != mcp` 却调 `call_backend_tool`（`backend_client.py:1367`）②条目无 `launch`（`registry.py:130`）③**（现状缺失）工具未配置后端连接** | 数据/配置不满足该操作的前提 | 改条目配置，不是重试能解决的 | 触发条件③见 §3，有 7 处该发未发 |
| `connection_error` | 后端未运行 / 连接失败（`backend_client.py:1384`） | 传输层不通 | 先 `start_backend` 再重试 | 语义与 `config_error` 的边界在文档里没写清（「没配连接」vs「配了但没跑」），建议 P1-03 在契约卡里点明 |
| `runtime_error` | 后端进程内异常（`backend_client.py:1390`） | 后端自己炸了 | 看 `get_backend_logs`；可重试一次 | 与 `internal_error` 的边界模糊（「后端的错」vs「本服务的错」），建议契约卡点明 |
| `destructive_blocked` | 命中破坏性判定且未传 `allowDestructive=true` | 安全门禁拒绝 | 确认无风险后带 `allowDestructive=true` 重试 | 无 |
| `backend_busy` | 同一后端正在处理另一调用（单后端串行，不排队） | 并发冲突 | **稍后重试，不要无限重试** | 无（`SKILL.md:107-108` 已有专门说明） |
| `internal_error` | 本服务未预期的异常（`server.py:167` 兜底） | 本服务的 bug 或环境问题 | 报告；重试通常无效 | 无 |
| `launcher_error` | `LauncherError`（启动器数据目录不可用等） | 启动器侧失败 | 检查启动器是否安装/数据目录是否可访问 | 无 |
| `install_required` | 依赖缺失且**未授权**自动安装 | 需要部署者显式授权 | 设 `AWAM_AUTO_INSTALL=1` 或条目 `install.auto=true` 后重试 | 契约成立，但**payload 形态不一致**：见 §4-Q1 |
| `install_not_allowed` | 已授权但 `install.command` 不在白名单 | 命令本身不合规 | 改条目里的命令后重试 | 同上 |
| `unknown_error` | 后端抛出既非 `BackendBusyError`/`ConnectionError`/`RuntimeError` 的异常 | 未分类的后端异常 | 看日志；可重试一次 | **两份文档都没列**。建议 P1-03 补进 SKILL.md，并在契约卡注明「这是兜底值，出现它说明后端抛了未预期类型」 |

---

## 3. 客户端可达的补齐清单（P1-03 的核心工作量）

### 3.1 先分层：不是所有 `success: False` 都该补 `errorType`

`src/` 共 **30 处** `"success": False`（`backend_client.py` 28 + `registry.py` 2），
按「是否能到达客户端成为 isError 正文」分三层：

| 层 | 站点数 | 说明 | 是否该补 |
|---|---|---|---|
| **A. 客户端可达**（经 `ensure_success()` 转 `ToolError`） | **14** | 见 §3.2 逐条 | **是，P1-03 的本体** |
| **B. 内部消费**（只被本模块/日志读取，不进协议） | 9 | `ensure_installed` 的 6 处检测结果（`backend_client.py:534/547/572/586/603/616`）+ `restore_desired_backends` 详情（`:1239`）+ `ensure_all_installed` 详情（`:1264`）+ `start()` 取消分支 | **否**。它们是「检测结果」不是「工具失败」，补 `errorType` 是噪声 |
| **C. 已合规** | 8 | `backend_client.py:791/1366/1377/1383/1389/1395`（6）+ `registry.py:122/130`（2） | 已有 |

> **计数修正（重要）**：我在此前给 team-lead 的汇报中说「29 处 `success: False`、23 处缺失」，
> 那个数字是把 B 层（内部站点）误算进了缺口。**精确口径是：总数 30、客户端可达且缺失 14、
> 其中 13 处可机械补齐 + 1 处是设计问题（§4-Q2）**。请以本清单为准更新看板。

### 3.2 A 层 14 处逐条补齐建议

| # | 站点 | 所属对外工具 | 现状 payload | 建议 errorType | 理由 |
|---|---|---|---|---|---|
| A1 | `backend_client.py:1099` | `set_backend_log_level` | `{"success":false,"error":"无效的日志级别，可选: ..."}` | `invalid_params` | 纯参数取值非法，正是该值的语义；同时让这个「死值」首次真正可达 |
| A2 | `backend_client.py:1280` | `start_backend` | `未配置 MCP 后端连接` | `config_error` | 与 `call_backend_tool` 的同款判定（`:1367`）保持一致 |
| A3 | `backend_client.py:1298` | `sync_backend_tools` | `MCP 工具未配置` | `config_error` | 同上 |
| A4 | `backend_client.py:1301` | `sync_backend_tools` | `后端未运行，请先启动` | `connection_error` | `SKILL.md` 对 `connection_error` 的说明原文即「后端未运行，用 `start_backend` 启动」——文档意图本就是如此，只是没落地 |
| A5 | `backend_client.py:1310` | `sync_backend_tools` | `后端返回空工具列表，保留原缓存未覆盖` | **见 §4-Q3，不建议机械补** | 这是「部分成功 + 告警」，用 `success:false` 表达本身就可商榷 |
| A6 | `backend_client.py:1319` | `sync_backend_tools` | `同步失败: {异常}` | `runtime_error` | 同步过程抛异常，与 `call_backend_tool` 的 `RuntimeError` 映射同源 |
| A7 | `backend_client.py:1325` | `stop_backend` | `未配置 MCP 后端连接` | `config_error` | 同 A2 |
| A8 | `backend_client.py:1336` | `get_backend_status` | `MCP 工具未配置` | `config_error` | 同 A2 |
| A9 | `backend_client.py:1351` | `get_backend_logs` | `MCP 工具未配置` | `config_error` | 同 A2 |
| A10 | `backend_client.py:1358` | `set_backend_log_level` | `MCP 工具未配置` | `config_error` | 同 A2 |
| A11 | `backend_client.py:864` | `start_backend` | `启动失败: {异常}` | `runtime_error` | 启动过程异常；`internal_error` 亦可，但「后端启动失败」更贴近 runtime 而非本服务 bug |
| A12 | `backend_client.py:1404` | `list_backend_tools` | `未配置 MCP 后端连接` | `config_error` | 同 A2 |
| A13 | `backend_client.py:1410` | `list_backend_tools` | `获取后端工具列表失败: {异常}` | `runtime_error` | 同 A6 |
| A14 | `backend_client.py:768` | `start_backend` | `{"success":false,"status":"installing","message":"依赖正在安装中，请稍后重试"}` | **见 §4-Q2，不建议机械补** | 这是「忙」不是「错」，见下 |

**汇总：13 处机械补齐（A1–A4、A6–A13），其中 `config_error` ×7、`runtime_error` ×3、`connection_error` ×1、`invalid_params` ×1；另 2 处（A5/A14）需先做设计决策。**

### 3.3 补齐后不新增枚举种类

上述 13 处全部复用**已声明的 14 种**，不引入第 16 种取值。这是刻意为之——P1-03 的目标是
「让已声明的契约落地」，不是「扩充契约面」。枚举面已经够大，再扩只会增加客户端分支成本。

---

## 4. 三个需要先决策的设计问题（不可机械补齐）

### Q1：`install_required` / `install_not_allowed` 的 payload 形态不一致

`backend_client.py:791-797` 同时写了两遍同一个值：
```python
"status": error_type,      # status 被复用成 errorType
"errorType": error_type,
```
`status` 字段在别处的取值是 `stopped`/`starting`/`running`/`error`（后端状态机），
在这里却被塞进 `install_required`——**一个字段承载了两套语义**。
客户端若按 `status` 判断后端状态，会看到一个不存在的状态值。
**建议**：P1-03 把 `status` 恢复为状态机取值（此处应为 `error`），错误分类只由 `errorType` 表达。
**但这会改变 payload 形状，属行为变更，需单独确认**（有测试断言 `status` 吗？P1-03 启动时须先查）。

### Q2：A14「依赖正在安装中」是忙态，不是错误

`success: false` + 无 `error` 字段，客户端无法区分「失败」与「请稍后重试」。
三个选项：
- **选项 1（推荐）**：新增 `errorType: backend_installing`（或复用 `backend_busy` 的语义家族）。
  代价：枚举 +1，两份文档要同步。
- **选项 2**：改成 `success: true` + `status: "installing"`。最诚实（它确实不是错误），
  但**破坏性最大**——现有调用方（toolport 网关）按 `success` 分支，行为会变。
- **选项 3**：维持现状，仅在契约卡里注明「`status=installing` 是特殊的非错误态」。
  零行为变更，但把负担推给客户端。

**我倾向选项 1**：它与 `backend_busy` 构成一致的「可重试的忙态家族」，
且不改变 `success` 分支语义。但需 team-lead 拍板（涉及枚举扩容）。

### Q3：A5「后端返回空工具列表，保留原缓存未覆盖」

这是**部分成功**：同步失败了，但旧缓存被明智地保留了（这是个好设计）。
用 `success: false` 表达，客户端会以为是硬失败；用 `success: true` 又丢了告警。
**建议**：保持 `success: false`，补 `errorType: runtime_error`，并**新增 `warning` 字段**
说明「已保留原缓存」——与 `call_backend_tool` 命中 destructive 时挂 `warning` 的既有手法一致。
这个可以机械补齐，但**新增字段属行为变更**，需在 P1-03 方案里显式申报。

---

## 5. 对测试的影响评估（P1-03 的回归风险面）

**好消息**：现有测试**没有任何一条断言「某失败 payload 不含 `errorType`」**
（已 grep `tests/test_server_dispatch.py` 与 `tests/test_backend_client.py` 全部 `errorType` 断言，
均为 `== "某值"` 的正向断言）。因此 §3.2 的 13 处补齐是**纯增量加字段**，
预计不会打破既有断言——这与 P1-04 的「行为等价」是不同性质：P1-03 是**有意的行为变更**，
按 team-lead 裁决须同步 `SKILL.md` 契约文档与测试断言。

**需要新增的测试**（建议）：
1. 每种 `errorType` 至少一条触发用例（当前 15 种里只有 9 种有用例）；
2. 「所有客户端可达的失败 payload 必含合法 `errorType`」的**守卫测试**——
   这是防止契约再次腐烂的机制，对标 `test_docs_sync.py` 对 `format.md` 生成块的保护；
3. 常量枚举的完整性测试：`ErrorType` 常量的值集合 == `SKILL.md` 声明的集合
   （把「文档 vs 代码」的漂移变成显红，而非静默）。

---

## 6. 建议的实施顺序（P1-03 启动后）

| 步 | 内容 | 前置 |
|---|---|---|
| S1 | **先决策 Q1/Q2/Q3**（本清单 §4）——Q2 涉及枚举扩容，Q1/Q3 涉及 payload 形状 | team-lead 裁决 |
| S2 | 在 `src/errors.py` 建 `errorType` 常量枚举（team-lead 已定落点），把 15 个裸字符串字面量收敛 | P1-04 已留门（`fail()` 在此模块） |
| S3 | 机械补齐 §3.2 的 13 处，全部引用常量而非裸字符串 | S2 |
| S4 | 按 S1 的决策处理 Q1/Q2/Q3 | S1 |
| S5 | 同步 `SKILL.md`（补 `unknown_error`、补契约卡的「客户端动作」列、按 Q2 决策扩枚举）与 `docs/format.md`（补 6 种） | S3/S4 |
| S6 | 新增 §5 的三类测试，含「失败必带 errorType」守卫 | S5 |
| S7 | 全量回归 + 提交，提交信息显式列出行为变更面（对标 P1-04 的 NE-1~NE-5 做法） | S6 |

> **与 P1-04 的边界（再确认）**：P1-04 期间**严禁**顺手做 S3/S4——在等价重构里补字段就是行为变更。
> 本清单的存在就是为了让 P1-04 的工程师「看见缺口但不动手」。

---

## 7. 一句话给 team-lead

P1-03 的真实工作量比「补 23 处」小得多也比它更结构化：**13 处机械补齐（全部复用已声明的 14 种枚举，
零枚举扩容）+ 3 个需你先拍板的设计问题 + 1 份文档同步（`format.md` 缺 6 种、`SKILL.md` 缺 1 种）**；
另有两个附带发现值得记一笔：`invalid_params` 是**声明了但永不到达的死值**，
`unknown_error` 是**代码在发但两份文档都没列的黑户**。
