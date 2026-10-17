# ARC-P3-02 真实端到端（e2e）测试层设计方案

> **文档性质**：设计方案（**只设计，不落代码**）。不改动任何既有代码与文档。
> **基线**：HEAD `b2d4b8d`，全量回归 **223 passed**；mcp **1.29.0**（`mcp.server.fastmcp` / `mcp.client.stdio` 均可用，写假后端**无需新依赖**）。
> 所有 `file:line` 在该提交上实测；不引用他人结论作证据。

---

## 0. 结论摘要

新增一层 **真实 stdio 端到端测试**（真实子进程 + 真实 MCP 客户端会话），5 个场景按价值排序落地；**默认不被 `pytest` 收集**，`pytest -m e2e` 显式跑；三条硬规则：**绝不污染仓库 `data/`**、**禁止固定 `sleep`**（一律轮询+超时）、**子进程 stderr 必留存**并在失败断言里打印尾部。

分期约 2 人日：Phase 1 脚手架 + 场景①②（~0.5d）→ Phase 2 场景③ 假后端全链路（~1d）→ Phase 3 场景④⑤（~0.5d）。

---

## 1. 现状盘点：哪些关键路径只有替身

| # | 关键路径 | 现有验证方式（file:line 实证） | 真实链路缺失的**后果** | **概率** | 风险序 |
|---|---|---|---|---|---|
| 1 | **stdio 传输 + `main()` 启动**（含 `migrate_legacy_runtime`、预热/恢复任务） | 无任何用例执行 `main()`（`server.py:419` 起）；预热/恢复任务创建在 `:445`/`:447`，**零覆盖**；唯一真实 stdio 是手工脚本 `tests/test_e2e_install_gate.py:76`（不被 pytest 收集） | 启动即崩、stdout 协议污染、迁移/预热异常**全不可见**；D23/D24/D26 三个缺陷正是「能力存在但从未被真实调用」型 | 中（改启动路径即可能踩） | **R1 最高** |
| 2 | **后端子进程生命周期**（start / 连接 / 心跳 / sync / stop / 收尸） | `tests/test_backend_client.py` 一律 monkeypatch `create_subprocess_shell`（`:801`、`:818` 等 8 处），**无一处真 spawn** | 真实 spawn/握手/拆除/孤儿进程/超时分支断了无人知；`backend_client.py` 1550 行且刚经历 P1-02 改造 | 中高 | **R2** |
| 3 | `list_tools` 五字段 | import 级采集比对 golden（`test_tool_specs.py:27`、`:57`），**不经 stdio** | 真实通道上的序列化/annotations 下发变形（如 outputSchema 校验失败）不可见 | 低-中 | R3 |
| 4 | 15 工具 dispatch | import 级 `test_server_dispatch.py` + `test_dispatch_equivalence.py` | 参数透传/未知工具在真实会话下的表现未验 | 中低（有等价性测试保护） | R4 |
| 5 | protocol roundtrip（isError / structuredContent） | **内存**会话 `create_connected_server_and_client_session`（`test_protocol_roundtrip.py:21`） | 已覆盖 SDK 行为；缺口是 stdio 特有（stdout 污染、进程启停） | 低 | R5 |
| 6 | resources / prompts 往返 | 仅 `test_protocol_roundtrip.py` 与 `test_server_dispatch.py`（内存/import 层） | AnyUrl、返回值类型类缺陷（D23/D26）的回归保护不在真实通道上 | 低 | R6 |
| 7 | **policy deny / `permission_denied`**（P1-05 Phase 2 新增） | 单测覆盖判定表（`test_policy_gate.py`）；`test_policy_config.py` 用子进程验 import 期快速失败——**已有子进程测试先例** | 真实进程里「deny 命中 → 后端零调用」未验（新代码、安全相关） | 中 | **R7** |
| 8 | 启动预热 / 状态恢复（`ensure_all_installed` `backend_client.py:1329`） | 无 | 自愈失效 / 预热误装；与 install 门禁耦合 | 中低 | R8 |

> 排序口径：**后果 × 概率**。R1/R2 是本层要优先补的；R5/R6 因已有内存层保护而靠后。

---

## 2. 场景清单（按价值排序）

公共前置：`tests/e2e/` 下抽一个 `stdio_session()` 辅助（上下文管理器：起子进程 → `ClientSession` → 关闭时收 stderr 全文），全部场景复用。

### 场景①　stdio 真实起服 + `list_tools` ≡ golden（价值最高，覆盖 R1/R3）

- 目的：证明「真实进程下发的 `tools/list`」与 golden 逐位一致，且 `main()` 能真正起来。
- 步骤：复制 `src/` + `data/` 到 tmp（见 §3 隔离）→ `stdio_client` 拉起该副本的 `server.py` → `session.initialize()` → `session.list_tools()`。
- 断言点：
  1. `initialize()` 成功（握手通 = stdio 通道干净、stdout 无污染）；
  2. 工具名集合 == `conftest.EXPECTED_TOOL_NAMES`（15 个，保序）；
  3. 五字段（name/description/inputSchema/annotations/outputSchema）与 `tests/golden/tools_list.json` 逐位全等——**真实进程采集**，与 `test_tool_specs.py:57` 的 import 采集互为交叉验证；
  4. stderr 含启动日志「服务启动中」（证明 `main()` 走到过），且**无** `Traceback`。
- 预算：≤4s。

### 场景②　只读工具真实调用（临时 `data/` 副本，覆盖 R4）

- 目的：在真实会话里跑通「查数据」这条最常用链路。
- 前置：tmp `data/` 副本里放 1–2 个手写条目（不依赖本机真实工具）。
- 断言点：`search_tools({"keyword": ...})` 命中预期条目且 `success=True`、`isError` falsy；`get_tool_info` 返回 `id` 一致；`launch_tool` 只返回命令、stderr/进程表里**无新子进程**（`launch_tool` 不执行是硬契约）。
- 预算：≤2s（可复用①的会话）。

### 场景③　仓内最小假 MCP 后端全链路（价值最高，覆盖 R2）

- 目的：唯一能验证「后端子进程生命周期」的非替身手段。
- 假后端：`tests/fixtures/fake_mcp_backend.py`，用 **mcp 自带的 `FastMCP`** 写一个 echo/为名工具（零新依赖）；条目 `connection = {"type":"stdio","command": sys.executable, "args": [fake_backend_path]}`。
- **关键设计（防竞态）**：command 用 `sys.executable`（绝对路径）→ `_target_available()`（`backend_client.py:375`）为真 → `ensure_installed()` 在 `:494` 直接短路返回，**不进入安装/持锁路径**，从而彻底绕开「启动预热 vs `start_backend`」竞态；同时不设 `AWAM_AUTO_INSTALL`，预热只做检测（`ensure_all_installed` `:1329`）。
- 步骤：`start_backend` → 轮询 `get_backend_status` 至 `running`（上限 10s）→ `call_backend_tool`（调 echo 工具）→ `sync_backend_tools`（断言子工具清单落缓存）→ `stop_backend` → 轮询至 `stopped`。
- 断言点：status 序列 `stopped → starting → running` 与 `→ stopped`（常量 `backend_client.py:200-203`）；`call_backend_tool` 返回值经假后端往返一致；`stop_backend` 后**无残留子进程**（进程存活检查，`_conn_task` 已收尸——孤儿进程是 R2 的核心风险）；全程 stderr 无 `Traceback`。
- 预算：≤8s（唯一允许超 5s 的场景）。

### 场景④　resources / prompts 往返（覆盖 R6）

- 断言点：`list_resources()` 含 `awam://tools`；`read_resource("awam://tools")` 返回 JSON 且可被解析（`AnyUrl` 归一化在真实通道下仍成立——D23 回归保护）；`list_prompts()`/`get_prompt()` 返回 `GetPromptResult`（D26 回归保护）。
- 预算：≤2s（复用①会话）。

### 场景⑤　policy deny 在真实进程中的表现（覆盖 R7）

- 前置：`AWAM_POLICY_FILE` 指向 tmp 里的策略文件（`{"tools":{"deny":["stop_backend"]},"destructive":{"mode":"deny"}}`；覆盖机制见 `policy.py:48-49`、`:75`）；import 期加载失败会快速失败（`:103`）——这一条已被 `test_policy_config.py` 的子进程测试覆盖，本场景验**运行期行为**。
- 断言点：
  1. 调用被 deny 的工具 → `isError=true` 且 payload `errorType == "permission_denied"`；
  2. **后端零调用**：以场景③的假后端为前提时，deny 命中后后端进程的调用计数/日志**无新增**（`check_call` 在 dispatch 前置、handler 根本没跑，`policy.py:275`）；
  3. 拒绝文案**不提** `allowDestructive`（P1-05 Phase 2 的 H1 话术要求）；
  4. `audit.log`（`<runtime>/audit.log`，`policy.py:61`）新增一行 `decision=block`。
- 预算：≤4s（独立进程）。

---

## 3. 运行策略

| 项 | 设计 |
|---|---|
| **收集方式** | 新增 `pytest.ini`（或 `pyproject.toml` 的 `[tool.pytest.ini_options]`）注册 `markers = e2e: 真实子进程/真实 stdio 的端到端用例（较慢，默认不跑）`；用例打 `@pytest.mark.e2e`。**默认 `pytest` 不跑 e2e**（`-m "not e2e"` 写入 `addopts`），显式 `pytest -m e2e` 才跑——理由：起真实子进程与后端，慢、依赖本机 python/npm 存在，且不宜让每次本地提交都付这个成本 |
| **耗时预算** | 单场景 ≤5s（③ 例外 ≤8s），**总量 ≤30s**（①~⑤ 合计）；超时即失败，不重试 |
| **隔离** | ① tmp 目录里**整树复制** `src/` + `data/`，起该副本的 `server.py`——因为 `DATA_DIR` 在 `server.py:63` 是**硬编码**（`<src 父目录>/data`，无 env 覆盖），用复制代替「给生产代码加 `AWAM_DATA_DIR`」可**零产品代码改动**；② `AWAM_RUNTIME_DIR` 指向 tmp（`runtime_paths.py:24`；`tests/conftest.py:22-23` 已有同款隔离先例）；③ **绝不写仓库 `data/`**——现手工脚本把探针条目写进真实 `data/tools/`（`test_e2e_install_gate.py:58`、`:64`），这是本层必须改正的一条 |
| **失败可诊断** | 子进程 stderr 一律落到 tmp 文件并在断言消息里附**尾部 30 行**；超时失败时把「已采集的 stderr + 最后一次状态快照 + 已用时长」一并塞进断言消息；假后端自带 stderr 前缀便于区分「谁的报错」 |
| **清理** | tmp 由 pytest `tmp_path_factory` 管理；进程用 `async with` + 显式 terminate，用例结束断言无残留子进程（孤儿进程会拖垮后续用例） |

---

## 4. 防 flaky 设计（直接从 `test_e2e_install_gate.py` 吸取的教训）

1. **禁止固定 `sleep`**：一律 `wait_until(predicate, timeout=..., interval=0.1)`（轮询 + 超时 + 失败带现场）。状态类断言（后端 running/stopped、缓存落盘）全部走它。
2. **预热 vs `start_backend` 竞态**：场景③ 用 `sys.executable` 作 command 使 `ensure_installed()` 短路（见 §2），**从根上不进入安装路径**，因此不存在该竞态。
3. **若将来要加「真实安装」场景**：两条硬约束——`(a)` 只断言 stderr 出现「执行安装」（证明命令真的提交给了 shell），**不对 `errorType` 断言**（服务启动预热持锁时 `start()` 命中 `install_result.get("installing")` 分支返回 `backend_installing`（`backend_client.py:780`），未持锁则走原生 spawn 失败路径——两态均合法；手工脚本 `:120-123` 也是这么处理的）；`(b)` install 命令必须 `--offline` 且目标包不存在，保证秒级失败、无网络、无副作用。
4. **每用例独立运行时目录与策略文件**：避免 `audit.log`/期望状态/后端缓存在用例间串味。
5. **不依赖本机真实工具**：Quicker、everything 等一律不参与（本机有无不可控）；只读场景用 tmp 手写条目，后端场景用仓内假后端。
6. **超时上限统一**：握手 5s、状态轮询 10s、单场景 8s、总量 30s——超时即失败（不做重试，重试会掩盖真 flaky）。

---

## 5. 与 `tests/test_e2e_install_gate.py` 的关系

**建议：拆分，不整体合并。**

- 迁进 e2e 框架的部分：**场景 1（未授权 → `install_required` + stderr 无「执行安装」）**——无副作用、秒级、断言确定，正适合做 `@pytest.mark.e2e` 用例；它顺带把「探针写真实 `data/`」改成 tmp 副本（修掉 §3 隔离里那条）。
- 保留手工脚本的部分：**场景 2（已授权 → 用 `--offline` 让 npm 真的被提交给 shell）**——它依赖本机 npm、验证的是「命令确实被 shell 执行」这一副作用面，且 `errorType` 受预热竞态影响不能断言；做成自动用例只会引入 flaky 与本机依赖。
- 两者共用新的 `tests/e2e/` stdio helper，消除 `StdioServerParameters` + stderr 采集的重复（`test_e2e_install_gate.py:76-93` 那段）。
- 手工脚本文件头补一行指路：说明 e2e 框架在哪、哪些场景已自动化、本脚本为何保留。

### 实施结果（2026-10-17，C 期落地）

按本节建议拆分完毕，路径以本节**之后**的为准（上文 §1/§3 对
`tests/test_e2e_install_gate.py` 的引用是设计时的快照，未回改）：

- 自动化：`tests/e2e/test_e2e_install_gate.py`（未授权那一轮，探针写 tmp 副本）；
- 手工：`tests/e2e/manual_install_gate.py`（`git mv` 保留历史，**去掉 `test_`
  前缀**以免被 pytest import；已授权 → npm 真跑那轮；`errorType` 不确定性说明
  原地保留）；
- 两者都复用 `tests/e2e/helpers.py`，仓库 `data/` 零触碰。

---

## 6. 分阶段实施 + 工作量 + scope 边界

| 阶段 | 内容 | 工作量 |
|---|---|---|
| **Phase 1** | `pytest.ini`（marker + 默认排除）、`tests/e2e/` 脚手架（stdio helper、整树复制 fixture、stderr 采集、`wait_until`）、场景①② | ~0.5 人日 |
| **Phase 2** | `tests/fixtures/fake_mcp_backend.py`（FastMCP）+ 场景③ 全链路 + 无残留子进程断言 | ~1 人日 |
| **Phase 3** | 场景④、场景⑤（含 `permission_denied` 真实进程断言与 `audit.log` 断言）+ 手工脚本拆分与指路注释 + `README`/`TASKS.md` 记录运行方式 | ~0.5 人日 |

**总计约 2 人日**（含用例与文档同步）。

**scope 边界（明确不做）**：
1. **不模拟网关 toolport 托管 stdio 路径**（多一层进程与协议中介，收益/成本比不划算）。
2. **不引入任何新依赖**（假后端用 mcp 1.29 自带的 `FastMCP`）。
3. **不为测试改动生产代码**——尤其不加 `AWAM_DATA_DIR` 之类的 env 覆盖（`DATA_DIR` 硬编码用整树复制绕过）。
4. 不做性能/压力/并发测试；不做 Windows 之外的平台兼容。
5. 不为 launcher（Quicker）做真实动作执行（本机有无 Quicker 不可控）；launcher 的破坏性判定仍是单测覆盖。
6. 不重做既有内存层（`test_protocol_roundtrip.py` 保留，e2e 是**叠加**不是替换）。

---

## 7. 待裁决（3 个）

| # | 问题 | 我的建议 |
|---|---|---|
| **Q1** | 收集策略：`-m e2e` **默认不跑**（推荐）还是默认跑？ | 默认不跑、`pytest -m e2e` 显式跑——真实子进程慢且依赖本机；但要求 **CI/发布前必跑一次**，并在 `TASKS.md` 写清命令 |
| **Q2** | `test_e2e_install_gate.py` 处置：按 §5 **拆分**（无副作用场景进 e2e、npm 真跑留手工）还是整体保留手工？ | 拆分：把「未授权门禁」这条最有价值且确定的断言自动化，其余保留手工 |
| **Q3** | 假后端形态：**FastMCP**（mcp 自带，~20 行）vs 手写 JSON-RPC 脚本（零框架依赖但脆） | FastMCP——它已在依赖里，写起来短、能随 mcp 升级一起演进；手写 JSON-RPC 会在 mcp 版本演进时自己先烂 |

---

## 附：三条硬规则（实施时逐条对照）

1. 绝不写仓库 `data/`（tmp 整树复制 + `AWAM_RUNTIME_DIR` 隔离）。
2. 禁止固定 `sleep`（一律 `wait_until` 轮询 + 超时 + 失败带现场）。
3. 子进程 stderr 必留存，失败断言里打印尾部——e2e 失败若只剩一句 timeout，等于没测。
