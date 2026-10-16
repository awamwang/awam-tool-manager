# awam-tool-manager 测试健康度与论断独立验证报告

- 验证人：严过关（Yan）· QA 工程师（独立验证，不改源码/既有测试）
- 验证时间：2026-10-16
- 被验证仓库：`G:/Projects/19AI/skills/awam-tool-manager`
- **验证时的仓库提交：`c40d80d`「feat: A14——运行时数据统一迁到 %LOCALAPPDATA%\.awam-tool-manager（168 passed）」**
- 解释器（生产同款）：`/c/Users/Administrator/.pyenv/pyenv-win/versions/3.12.0/python.exe`（Python 3.12.0）
- 实测环境：`mcp==1.29.0`、`jsonschema==4.26.0`、`pytest==9.1.1`、`anyio==4.14.2`、`pytest-asyncio==1.4.0`
- 报告定位：**只验证与判定，不改代码**。为证明断言有效性做过 4 处临时改动，均已在同一轮内还原（详见 ④），还原后 `git status --porcelain` 为空。
- **行号锚定**：本报告所有 `file:line` 引用均以 **`c40d80d`** 为准（该提交重写了 README/SKILL/TASKS/architecture/format/mcp-conformance 等文档；A14 顺带修正了约 16 条手写漂移）。凡涉及更早提交的行号，均另行标注提交号。

> ⚠️ 重要时间线说明：本任务派发时，仓库处于提交 `8afc317`（A13）的**未提交工作区**，当时 TASKS.md 写「158 passed（8 个文件）」、architecture.md 写「7 个文件，106 个用例」——**这对矛盾在派发时真实存在**。在我验证过程中，A14 提交 `c40d80d` 落地，两处文档已被同步为「168 passed（9 个文件）」/「9 个文件，168 个用例」，与实测完全一致。本报告以 `c40d80d` 为基准裁定，并在 ⑧ 中对「158」做历史切面裁定。

---

## ① 结论

**测试是否可信：可信度高，可作为重构安全网——但有一个明确的先决条件。**

可信的依据（全部实测）：

1. **真实全绿、可复现**：`168 passed / 0 failed / 0 skipped / 0 error`，连续 9 次全量运行结果完全一致（`3.6s~4.1s`）。
2. **关键不变式是真的**：抽查 6 条核心不变式，静态阅读显示测试断言的是**真实契约**（真实 SDK 表面白名单、真实状态迁移、真实并发锁行为），而非只断言自写 mock。其中 4 条做了**红→绿双向实证**：故意改坏源码后对应用例确实变红，还原后变绿（详见 ④）。
3. **覆盖率有实际分量**：用 `pytest-cov` 实测 `src/ + scripts/` 合并行覆盖 **83%**（1445 语句，漏 251）。核心风险模块 `backend_client.py=84%`、`launcher_client.py=90%`、`registry.py=95%` 覆盖扎实。
4. **零外部依赖污染**：全部外部程序调用（npm / Quicker / Everything / winget）在单测里都被 monkeypatch 成替身，测试**不依赖任何真实外部进程**，因此也不存在「外部程序缺失→假通过」的隐患。

**先决条件 / 不可用于安全网的部分：**

- 测试**只覆盖了 in-memory 通道**，没有一条用例真正跑过 **stdio 子进程传输**（`server.main()` 从未被执行，覆盖率显示 `server.py:954-996` 整段未命中）。这意味着：**「stdout 协议污染」「真实 stdio 握手/退出」「真实子进程启停」这三类问题，现行的自动化测试抓不到。** 作为重构安全网时，凡涉及 stdio 传输层与进程生命周期的改动，必须有手工端到端复核，不能只信 `pytest` 绿灯。
- 多实例并发读写同一运行时目录**无任何测试**（A10 的跨世代自愈恰是多实例场景，仅用单进程 fake 验证）。

**一句话裁定：可用于重构安全网的「业务逻辑 / 状态机 / 协议载荷映射」部分；不可用于「stdio 传输层 / 进程生命周期 / 多实例并发」部分——后三者需补测或手工验证。**

---

## ② 实测数据表

### 2.1 解释器与依赖（命令 + 原始输出）

命令：
```bash
PY="/c/Users/Administrator/.pyenv/pyenv-win/versions/3.12.0/python.exe"
"$PY" -c "import importlib.metadata as m; print('mcp',m.version('mcp')); print('jsonschema',m.version('jsonschema')); print('pytest',m.version('pytest')); print('anyio',m.version('anyio')); print('pytest-asyncio',m.version('pytest-asyncio'))"
```
原始输出：
```
mcp 1.29.0
jsonschema 4.26.0
pytest 9.1.1
anyio 4.14.2
pytest-asyncio 1.4.0
```
> 更正派发说明的一处事实：任务说明称「项目未装 pytest-asyncio」。实测**该解释器实际装有 pytest-asyncio 1.4.0**。不过项目测试**不依赖**它（所有异步用例统一 `asyncio.run(...)` 包装），故不影响任何结论，仅记录环境差异。

### 2.2 用例总量 / 通过率 / 耗时

命令（项目根目录执行，与 TASKS.md 记载的运行方式一致）：
```bash
"$PY" -m pytest -q
```
原始输出（摘尾部）：
```
168 passed in 3.97s
```
连续 9 次全量运行（4 次 `-q`、5 次 `-q -p no:cacheprovider`）结果：
```
168 passed in 3.97s / 3.63s / 3.63s / 3.57s / 3.76s / 3.75s / 3.78s / 3.68s / 4.09s
```
**汇总：Total 168 | Passed 168 | Failed 0 | Skipped 0 | Error 0，文件数 9，耗时约 3.6–4.1s。**

### 2.3 逐文件用例数（命令 + 原始输出）

命令：
```bash
for f in tests/test_*.py; do n=$("$PY" -m pytest "$f" --collect-only -q 2>/dev/null | tail -1); echo "$f -> $n"; done
```
原始输出：
```
tests/test_backend_client.py   -> 56 tests collected
tests/test_docs_sync.py        -> 13 tests collected
tests/test_launcher_client.py  -> 27 tests collected
tests/test_protocol_roundtrip.py -> 9 tests collected
tests/test_registry.py         -> 5 tests collected
tests/test_registry_policy.py  -> 16 tests collected
tests/test_runtime_paths.py    -> 10 tests collected
tests/test_server_dispatch.py  -> 24 tests collected
tests/test_store.py            -> 8 tests collected
```
合计 `56+13+27+9+5+16+10+24+8 = 168`，与全量运行一致；**9 个文件**（`conftest.py` 非测试文件）。

### 2.4 与文档「158 passed（8 个文件）」的逐项对比

| 项目 | 当前 TASKS.md(`c40d80d`) | 实测 | 判定 |
|---|---|---|---|
| 总用例 / 通过 | 168 passed | 168 passed | ✅ |
| 文件数 | 9 | 9 | ✅ |
| `test_backend_client.py` | 56 | 56 | ✅ |
| `test_store.py` | 8 | 8 | ✅ |
| `test_server_dispatch.py` | 24 | 24 | ✅ |
| `test_registry_policy.py` | 16 | 16 | ✅ |
| `test_protocol_roundtrip.py` | 9 | 9 | ✅ |
| `test_launcher_client.py` | 27 | 27 | ✅ |
| `test_runtime_paths.py` | 10 | 10 | ✅ |
| `test_docs_sync.py` | **12** | **13** | ❌ 文档少记 1 |
| `test_registry.py` | 未给数字 | 5 | ⚪ 未声明 |
| 对外 MCP 工具数 | **12 个**（第 28 行）/「实测 12 个工具就位」（第 188 行） | **15** | ❌ 陈旧 |

**缺口解释**：TASKS.md 的「158 / 8 文件」是提交 `8afc317` 时的旧值（当时尚无 `test_runtime_paths.py` 的 10 例；`158 + 10 = 168`，`8 + 1 = 9`），`c40d80d` 已修正。**唯一残留的数字错是 `test_docs_sync.py` 记 12、实为 13**：该文件 9 条固定用例 + `data/tools/*.json` 参数化 4 条 = 13（`data/tools` 现有 4 个文件），文档仍按旧的 3 文件口径记 12。

### 2.5 静默跳过 / 条件跳过排查（重要）

- **全量运行 0 skipped**。全仓库唯一的条件跳过是 `tests/test_runtime_paths.py:41` 的 `pytest.skip("只在 Windows 上验证 LOCALAPPDATA 分支")`——**仅非 Windows 平台触发**；本机是 Windows，该用例正常运行。
- **`test_docs_sync.py` 的 glob 参数化「静默消失」风险——实测结论：在 pytest 9.1.1 下不存在「静默」失败，会表现为可见的 SKIP。**
  验证方法（在 `/tmp` 下建探针，**未触碰仓库**）：构造一个 `sorted(空目录.glob("*.json"))` 的参数化用例 + 一个对照用例。
  命令与原始输出：
  ```
  $ "$PY" -m pytest -v -rs
  test_param_empty.py::test_real_files_are_valid[NOTSET] SKIPPED (got ...) [ 50%]
  test_param_empty.py::test_control_still_here PASSED                      [100%]

  SKIPPED [1] test_param_empty.py:5: got empty parameter set for (p)
  ======================== 1 passed, 1 skipped in 0.02s ========================
  ```
  即：空参数集生成一个 `[NOTSET]` 用例并 **SKIPPED**，跳过原因 `got empty parameter set`。所以曾有的「目录为空时用例会静默消失」描述（见 8afc317 版 TASKS.md；`c40d80d` 已改为删除线注记于第 199–201 行并将该措辞移除）**在当前 pytest 版本下不准确**——不会静默，会留 1 个 skip。**但残留风险仍在**：该 skip 只在 `-rs`/`-v` 或汇总行可见，若只看「passed」很容易忽略；空目录时「真实数据校验」从「4 条断言」降级为「1 条 skip」，等于这条真实数据防线被静默削弱。（`test_docs_sync.py` 已把 `backend-tools` 改为写入侧校验 `test_generated_backend_catalog_is_valid`，该具体风险已消解；仅 `data/tools/*.json` 这条仍是 glob 参数化。）

---

## ③ Flaky 与外部依赖清单

### 3.1 稳定性实测（时间敏感文件连续 3 轮）

命令：
```bash
for i in 1 2 3; do "$PY" -m pytest tests/test_backend_client.py tests/test_launcher_client.py -q -p no:randomly; done
```
原始输出：
```
83 passed in 3.34s
83 passed in 2.92s
83 passed in 3.20s
```
结论：**稳定一致**（session 内无 `pytest-randomly`，执行顺序确定）。

### 3.2 时间敏感用例清单与「是否依赖真实时钟」判定

| 用例 | 时间相关点 | 实测判定 | 抗 flaky 手法 |
|---|---|---|---|
| `test_tools_cache_respects_ttl_and_force_refresh` | `TOOLS_CACHE_TTL` / `_tools_cache_time` | **稳定** | 显式 `_tools_cache_time -= 1` 回拨，不依赖 `time.time()` 相邻两次返回不同值（规避 Windows ~15ms 刻度）；注释已记录原始 flaky 现场 |
| `test_heartbeat_failure_cleans_up` | `HEARTBEAT_INTERVAL=0.01` + 轮询 | 稳定 | 缩短间隔 + `for _ in range(100): sleep(0.02)` 有界轮询（≤2s） |
| `test_heartbeat_probe_matches_real_sdk` | 同上 + `ping_calls>=2` | 稳定 | 有界轮询，不依赖固定 sleep |
| `test_start_cancelled_does_not_strand_starting_status` | `sleep(0.05)` 让状态进入 starting | 稳定 | 等待的是「状态进入」而非精确时长；断言最终值 |
| `test_list_actions_*`（A13 刷新，7 例） | `ACTIONS_CACHE_TTL=300` | 稳定 | **改写 `lastSynced` 制造陈旧**，不睡满 300s、不依赖时钟刻度 |
| `test_last_synced_is_real_timestamp` | `datetime.now(UTC)` 格式 | 稳定 | 断言格式与合理区间，不做精确相等 |

未发现任何用例断言「两个相邻 `time.time()` 必然不同」这类脆弱假设；也未发现依赖真实网络/真实外部进程的用例。

### 3.3 外部依赖行为矩阵（npm / Everything / Quicker / winget）

命令（静态核查所有 `create_subprocess*` 与真实 exe 触点）：
```bash
grep -rn "create_subprocess|QuickerStarter|Everything\.exe|winget|npm " tests/
```
结论：**所有** `create_subprocess_shell` / `create_subprocess_exec` 调用点在测试中均被 `monkeypatch` 替换为替身（`FakeInstallProcess` / `_fake_install_factory` / `_fake_shell_dispatch` / `fake_exec` 等）。

| 外部依赖 | 是否有测试真实调用 | 缺失时行为 | 判定 |
|---|---|---|---|
| npm（`npm prefix -g`、`npm i -g`） | 否（全部替身） | 不影响任何用例 | 无隐患（也不会发现「npm 真的缺失」这类集成问题——属覆盖盲区，非 flaky 源） |
| Everything（`Everything.exe`） | 否 | 不影响 | 同上 |
| Quicker（`QuickerStarter.exe`） | 否（`create_subprocess_exec` 被替身，断言命令构造） | 不影响 | 同上 |
| winget | 否 | 不影响 | 同上 |

**关键结论：不存在「外部程序缺失 → 假通过 / 真失败」的用例**，因为没有任何用例真正调用外部程序。代价是：真实子进程启停链路（TASKS.md 宣称「真实后端子进程启停 + 调用回归通过」）**在自动化测试里没有对应证据**，该断言只能靠手工端到端记录支撑。

---

## ④ 红绿验证记录（本任务最重要一项）

> 红线纪律：改动前已记录基线 `git status --porcelain` 与逐文件 `git hash-object`（存于 `/tmp/qa_baseline_*`）。每处改动用精确文本编辑做出、**用精确反向编辑还原**（不使用 `git checkout --`——因为源文件当时处于未提交工作区，`git checkout` 会误删上游改动）。每组都贴 **红 / 绿** 原始输出，并给出还原后哈希与基线哈希的一致性证明。
> 说明：验证过程中 A14 提交 `c40d80d` 落地，把工作区改动提交；`git status` 至此从「脏」变为**完全干净**。下述红线文件的当前哈希与验证基线逐字节一致。

**基线哈希（我改动的文件）：**
```
e79fa14bb65c5b0796146e7a1a46c25e1519d0b0  src/backend_client.py
9b367253786e6dce774b76a32faf3afdb5b061cd  src/server.py
（docs/format.md 在基线时相对 HEAD 干净）
```
**最终还原证明（全部 4 处还原后）：**
```
$ git status --porcelain
(空)
$ git hash-object src/backend_client.py  # e79fa14b...  ✅ 与基线一致
$ git hash-object src/server.py          # 9b367253...  ✅ 与基线一致
$ git diff --quiet docs/format.md && echo CLEAN   # CLEAN ✅
$ grep -rn "QA-REDGREEN" src/ docs/               # 无残留 ✅
```

### 4.1 绿基线（4 条目标用例，改动前）
```bash
"$PY" -m pytest \
  tests/test_backend_client.py::test_start_cancelled_does_not_strand_starting_status \
  tests/test_backend_client.py::test_heartbeat_probe_matches_real_sdk \
  tests/test_registry_policy.py::test_destructive_gate_is_per_sub_tool \
  tests/test_docs_sync.py::test_format_doc_matches_generated_tables -q
```
```
....                                                                     [100%]
4 passed in 0.28s
```

### 4.2 红绿 #1 — A11「start() 被取消后不得卡在 starting」
- 做坏：`src/backend_client.py` 中 `start()` 的 `except BaseException as e:`（兜底取消）改回 `except Exception as e:`。
- **红**（原始输出）：
  ```
  >       assert client.status == BackendClient.STATUS_STOPPED, (
  E       AssertionError: 取消后 status 应从 starting 收回为 stopped，实际: starting
  E       assert 'starting' == 'stopped'
  FAILED tests/test_backend_client.py::test_start_cancelled_does_not_strand_starting_status
  1 failed in 0.79s
  ```
- 还原后 **绿**：`1 passed`（见 4.6 组合复跑）。
- 判定：**测试真实有效**——`CancelledError` 继承 `BaseException`，一旦兜底退回 `Exception`，状态就永久卡 `starting`，用例立刻抓住。

### 4.3 红绿 #2 — A9「心跳探测必须落在真实 SDK 方法上」
- 做坏：`_heartbeat_loop` 中 `await self.session.send_ping()` 改为 `await self.session.ping()`。
- **红**（原始输出）：
  ```
  >       assert session.ping_calls >= 2, "心跳应周期调用 send_ping"
  E       AssertionError: 心跳应周期调用 send_ping
  E       assert 0 >= 2
  [awam:fake][warning] 心跳失败，连接标记为不可用: '_SdkSurfaceSession' object has no attribute 'ping'
  FAILED tests/test_backend_client.py::test_heartbeat_probe_matches_real_sdk
  1 failed in 3.66s
  ```
- 判定：**测试真实有效**，且**双重防线成立**——① 契约断言 `hasattr(mcp.ClientSession, "send_ping")` 从真 SDK 侧堵；② 假会话 `_SdkSurfaceSession` 表面即真实 SDK 白名单，调错方法立刻 AttributeError。这正是当年「开发环境 mcp 1.30 自带 ping、测试全绿也发现不了」缺陷的针对性回归。

### 4.4 红绿 #3 — A12「子工具级 destructive 门禁」
- 做坏：`src/server.py` 的 `_needs_destructive_confirm` 末尾 `return sub_tool in listed` 改为 `return False`。
- **红**（原始输出）：
  ```
  >       with pytest.raises(server.ToolError) as exc:
  E       Failed: DID NOT RAISE ToolError
  [awam-tool-manager] 路由调用: boom/nuke
  FAILED tests/test_registry_policy.py::test_destructive_gate_is_per_sub_tool
  1 failed, 1 passed in 0.64s
  ```
  （同批的 `test_destructive_without_sub_tool_list_still_blocks_everything` 仍 passed——证明两条用例分别锁住「配了 destructiveTools 只拦清单内」与「未配则整后端拦」两个**不同**分支，非重复断言。）
- 判定：**测试真实有效**，且断言的是契约（子工具是否被拦 + 是否带 `warning`），非实现细节。

### 4.5 红绿 #4 — 文档漂移检测
- 做坏：手工改 `docs/format.md` 生成区块内 `id` 字段表行（追加 `[QA-REDGREEN]`）。
- **红**（原始输出）：
  ```
  E         - 9._-]+$`） [QA-REDGREEN] |
  E         + 9._-]+$`） |
  tests\test_docs_sync.py:55: AssertionError
  FAILED tests/test_docs_sync.py::test_format_doc_matches_generated_tables
  1 failed in 0.52s
  ```
- 判定：**测试真实有效**——`test_docs_sync.py` 真能拦住手改 `docs/format.md` 字段表；真相源单一（`src/schema.py`）+ 生成器 + 逐字节比对，闭环成立。

### 4.6 还原后组合复跑（4 条同时回绿）
```bash
"$PY" -m pytest \
  tests/test_backend_client.py::test_start_cancelled_does_not_strand_starting_status \
  tests/test_backend_client.py::test_heartbeat_probe_matches_real_sdk \
  tests/test_registry_policy.py::test_destructive_gate_is_per_sub_tool \
  tests/test_docs_sync.py::test_format_doc_matches_generated_tables -q
```
```
....                                                                     [100%]
4 passed in 0.32s
```

### 4.7 未做红绿（仅静态）的 2 条不变式
- **A8（`_connection_loop` 独占任务 / CancelScope 同任务进出）**：静态判定**有效**。`test_connection_contexts_enter_and_exit_in_same_task` 用 `_RecordingStdio`/`_RecordingSession` 记录 `__aenter__/__aexit__` 发生在哪个 `asyncio.current_task()`，断言「进入任务 == 退出任务」且「≠ 调用方任务」，start 与 stop 特意跑在不同任务里复现真实 server 场景。这是对 anyio CancelScope 硬约束的直接建模，不是自证 mock。（因改动会牵扯 `_connection_loop` 结构、影响面超出「一行改坏」，未做破坏性红绿。）
- **A10（期望状态落盘 + 新世代恢复）**：静态判定**有效**。`test_start_persists_desired_state` / `test_failed_start_does_not_persist` / `test_stop_clears_desired_state` / `test_restore_starts_desired_backends` / `test_restore_skips_already_running` / `test_restore_cleans_stale_entries` / `test_desired_state_corrupt_file_treated_as_empty` 覆盖了**写意图、失败不写、清意图、新世代恢复、已运行跳过、过期清理、损坏容错**完整闭环，且断言落盘文件真实内容（`json.loads(state_file.read_text())`）。用 fake `start/stop` 驱动，符合单测隔离原则。

---

## ⑤ 协议层测试「是否名副其实」（对 `test_protocol_roundtrip.py` 的核实）

**它走的通道：in-memory（内存流），不是真实 stdio 子进程。** 证据：该文件使用 `from mcp.shared.memory import create_connected_server_and_client_session`。我读取了该 SDK 函数的实现（`...\site-packages\mcp\shared\memory.py`）：
```python
async with create_client_server_memory_streams() as (client_streams, server_streams):
    ...
    tg.start_soon(lambda: server.run(server_read, server_write, ...))
    async with ClientSession(read_stream=client_read, write_stream=client_write, ...) as client_session:
        await client_session.initialize()
        yield client_session
```

判定：
- **「不 mock SDK」这句属实**：它用的是**真实 `mcp.ClientSession`** 与**真实 `mcp.server.Server.run`**，走完整 JSON-RPC 编解码 + 初始化握手，没有替代实现，也不是自己写的假会话。`isError` 映射、`structuredContent`、`outputSchema`、`resources/read`、`prompts/get` 这些**全部发生在 Server/ClientSession 内部**，因此用 in-memory 通道验证**足够**——这些能力与传输层无关。
- **「真实 MCP 会话」需加限定**：通道是内存流而非子进程 stdio；**没有**经历 `stdio_server()`、子进程 spawn、真实 pipe、真实进程退出。因此它**没有资格**验证：真实 stdio 传输、stdout/stderr 分流、子进程生命周期、`server.main()` 启动路径。
- 佐证：coverage 显示 `server.py:954-996`（`main()` 内 `mcp.server.stdio.stdio_server()` 与 `app.run`）**整段未命中**——全仓库没有任何测试真的跑过 stdio 服务主循环。

---

## ⑥ 未覆盖区域与风险等级（coverage 实测）

覆盖率命令与原始输出：
```bash
"$PY" -m pytest tests/ -q --cov=src --cov=scripts --cov-report=term-missing
```
```
Name                             Stmts   Miss  Cover   Missing
scripts\generate_format_doc.py      80     20    75%   108, 124-144, 148
src\backend_client.py              609     97    84%   51, 74, 128-131, ... (见下)
src\launcher_client.py             202     21    90%   87-88, 110, 154-155, 184, 194-195, 209, 214-215, 294-295, 335-339, 355, 402-403
src\registry.py                     65      3    95%   28, 43, 134
src\runtime_paths.py                52      7    87%   45, 61-62, 80-81, 91-92
src\schema.py                       52      5    90%   207, 221, 235, 309, 317
src\server.py                      286     79    72%   454-455, 459, 485-486, 561-567, 580, 612-617, 620-623, 626-629, 650-653, 656-659, 673-674, 704-714, 781, 894, 923, 941-949, 954-996, 1000-1004
src\store.py                        99     19    81%   55-57, 103, 108, 142-148, 188-194
TOTAL                             1445    251    83%
```

| 未覆盖区域 | 实测覆盖 | 风险等级 | 理由 |
|---|---|---|---|
| `server.py` 的 `main()`（stdio 服务主循环 `954-996`、`__main__` `1000-1004`） | 未命中 | **高** | 真实 stdio 传输、`app.run`、预热/恢复后台任务、`close_all` 收尾**全无测试**；任何 stdout 污染、启动崩溃、退出泄漏都逃过 CI |
| 多实例并发读写同一运行时目录 | 无用例 | **高** | A10 跨世代自愈本身就是多实例/多进程场景，却仅用单进程 fake 验证；`lastSynced` 被活实例反复改写是已知现象，无并发写测试 |
| `launcher_client.py` `run_action` 超时/启动失败分支（`335-339`）、`stderr` 非空（`355`）、`LauncherManager.run_action`（`402-403`） | 未命中 | 中 | 失败路径（TimeoutError/OSError）无测试；GBK 解码与「exit 0 但输出含失败」**已覆盖**，仅异常分支空白 |
| `runtime_paths.py` 异常兜底分支（`45`、迁移失败 `_note` `61-62/80-81/91-92`） | 未命中 | 中 | `get_runtime_dir` 无 override 的 home 分支、迁移 `OSError` 静默降级路径未测；正常路径已测 |
| `store.py` 读取 `OSError` 分支（`55-57`）、`save_tool`（`103`）、`list_backend_tools`/`list_launcher_actions`（`142-148`/`188-194`） | 未命中 | 中 | 原子写正常路径与写失败保留旧内容**已测**（`test_failed_write_keeps_old_content`）；「断电/fsync 中断」物理场景无法在单测模拟，属合理留白 |
| `scripts/generate_format_doc.py` 的 `main()`/CLI/`--check`/写文件（`124-144`、`148`、`block 缺失` `108`） | 75%，逻辑已覆盖 | 低 | `generate()`/`render_table`/`_flatten`/`_suffix` 均被 `test_docs_sync` 间接跑通；仅 CLI 包装与文件落盘未测，风险低 |
| `server.py` prompts **内容正确性** | 部分 | 低 | 仅 `how_to_call_backend` 正文被断言（`tool_id=`/`search_files`/无 `launcher_id=`）；`getting_started`/`how_to_search` 的正文文案无断言，但二者不含易漂移参数名，风险低 |
| `schema.py` 降级分支（`207/221/235/309/317`） | 90% | 低 | 缺 `jsonschema` 时的降级校验路径未测（当前环境已装 jsonschema） |

---

## ⑦ 代码卫生与 stdout 协议污染

### 7.1 编译与静态检查（命令 + 原始输出）

```bash
"$PY" -m compileall -q src scripts tests   # 退出码 0，无语法错误
"$PY" -m ruff check src scripts --statistics
"$PY" -m ruff check tests --statistics
```
`src + scripts` 的 ruff 统计：
```
13  BLE001  blind-except
 3  I001    unsorted-imports
 3  RUF012  mutable-class-default
 2  RUF100  unused-noqa
 1  FURB188 slice-to-remove-prefix-or-suffix
 1  RUF010  explicit-f-string-type-conversion
Found 23 errors.
```
`tests` 的 ruff 统计：
```
25 RUF100 / 4 I001 / 3 RUF012 / 2 PLR1711 / 2 RET501 / 2 RUF059 / 1 F401 / 1 SIM117 / 1 UP012
Found 41 errors.
```

**专项核查结论：**
- **裸 `except:`（E722）：无。** `grep -rn "except\s*:" src/` 为空；`ruff check src --select E722` → `All checks passed!`。
- **未使用导入 / 未定义名（F401/F821/F841）：`src` 内无。**（`tests` 有 1 个 F401，非生产代码。）
- **`except Exception` 宽捕获（BLE001）13 处**：属**有意为之**的容错设计（心跳失败、预热、清理、单后端恢复失败不阻断其他），多数带注释说明；`except BaseException` 5 处在关键取消/清理路径，正当。
- **其余为风格项**（import 排序、`mutable-class-default` 等），不影响正确性。

### 7.2 stdout 协议污染专项（最重要）

命令：
```bash
grep -n "\bprint\(|sys\.stdout" src/*.py
```
原始输出（全部 `print` 调用点）：
```
src/backend_client.py:51   print(..., file=sys.stderr, flush=True)
src/backend_client.py:288  print(..., file=sys.stderr, flush=True)
src/launcher_client.py:72  print(..., file=sys.stderr, flush=True)
src/registry.py:30         print(..., file=sys.stderr, flush=True)
src/server.py:66           print(..., file=sys.stderr, flush=True)
src/store.py:15            print(..., file=sys.stderr, flush=True)
```
**结论：`src/` 内所有 6 处 `print` 全部显式 `file=sys.stderr`，无一处写 stdout，无 `sys.stdout.write` 调用。** stdout 协议通道在源码层面是干净的。旁证：我以脚本 import `server` 并调用 `list_tools()` 时，日志 `[awam-tool-manager] 收到 list_tools 请求` 出现在 **stderr**、stdout 只输出我脚本自己的 `print`。

> 但注意：因**没有任何测试真正跑 stdio 子进程**（见 ⑤/⑥），「运行期是否意外产生 stdout 字节」**在自动化测试里无护栏**——当前干净依赖人工审查，而非测试保证。

### 7.3 仓库根目录残留 `test_*.py` 与 `test_output.txt`

命令与原始输出：
```bash
ls test_*.py test_output.txt
# test_backend_start.py test_basic.py test_debug.py test_direct_mcp.py
# test_e2e.py test_list_backend.py test_local.py test_search_mcp.py
# test_start_mcp.py test_output.txt          ← 共 9 个 test_*.py + 1 个输出文件
grep -l "^def test_|^async def test_" test_*.py   # NONE have test_ functions
"$PY" -m pytest -q                                # 从根目录运行
# 168 passed in 3.97s   ← 根目录脚本未被收集为用例
```
- 这 9 个根目录脚本是**调试期手工脚本**（均为 `asyncio.run(main())` 形式，无 `test_` 函数），虽受 `.gitignore` 排除，但**命名带 `test_` 极易误导后来者**误以为是活跃测试。
- 内容已过期、含误导性断言：如 `test_search_mcp.py` / `test_start_mcp.py` 使用 `cmd.exe /c npx -y everything-search-mcp` 直连老包；`test_output.txt` 里是「已配置的 MCP 工具: ['everything-search-mcp']」「工具目录已自动同步：3 个工具」等**陈旧快照**（现为 15 个对外工具、子工具数不同）。**建议删除或移入 `dev_scripts/` 并改名去掉 `test_` 前缀。**
- 由于它们**不含 `test_` 函数**，`pytest` 从根目录运行也与从 `tests/` 运行一致（均 168），故当前**不会**污染用例数或产生假失败；风险纯属「误导人」。

---

## ⑧ 对「文档自称 158 passed」的裁定（含文档可信度抽查 ≥5 条）

### 8.1 裁定

1. **在验证基准 `c40d80d` 下：TASKS.md 与 architecture.md 均已写「168 passed（9 个文件）」，与实测 100% 吻合。** 派发任务时所述的「158 passed（8 个文件）」与「106 用例 / 7 文件」矛盾，属于**提交 `8afc317` 的历史状态**，已在 `c40d80d` 修正。
   - 证据：`git show 8afc317:TASKS.md | grep 158` → `158 passed（8 个文件）`；`git show 8afc317:docs/architecture.md | grep 106` → `7 个文件，106 个用例`；`git show 8afc317:TASKS.md | grep 106` 无。
   - 当前：`TASKS.md:165 → 168 passed（9 个文件）`；`docs/architecture.md:88 → 9 个文件，168 个用例`。
   - **权威复核（非破坏）**：`git archive 8afc317 | tar -x -C /tmp/qa_8afc` 后在该副本运行，实测 **`158 tests collected` / `158 passed`，8 个文件** —— 即 TASKS.md 当时的「158/8」**在其自身提交下完全准确**，只是后来 `test_runtime_paths.py`（+10 例、+1 文件）把口径更新为 168/9。**唯一差异就是这一个新增文件；`158 + 10 = 168`、`8 + 1 = 9`。**
2. 因此**「158」不是当前文档的错误，而是被修正的历史值**；`158 + test_runtime_paths(10) = 168`、`8 + 1 = 9`，可完全解释。
3. **但文档仍有未清理的陈旧数字**（见 8.2），其中最显眼的是 TASKS.md 第 28 行仍写「对外 MCP 工具（12 个）」，而实际是 **15 个**——这会让读者误判对外能力规模。

### 8.2 可机器验证的事实抽查（≥5 条，逐条实测）

| # | 文档说 | 实测是 | 是否正确 |
|---|---|---|---|
| 1 | TASKS.md:165「合计 168 passed（9 个文件）」 | 168 passed / 9 文件 | ✅ |
| 2 | architecture.md:88「9 个文件，168 个用例」 | 168 / 9 | ✅ |
| 3 | TASKS.md:28「对外 MCP 工具（12 个）」 | `server.list_tools()` 返回 **15** 个 | ❌ 陈旧 |
| 4 | TASKS.md:123「test_docs_sync.py（12 用例）」 | **13**（9 固定 + `data/tools` 4 文件参数化） | ❌ 少记 1 |
| 5 | architecture.md:214「store.py \| 139 行」 | `wc -l` = **194** | ❌ 陈旧 |
| 6 | architecture.md:216「server.py \| 828 行」 | **1004** | ❌ 陈旧 |
| 7 | architecture.md:215「backend_client.py \| 1246 行」 | **1267** | ❌ 陈旧 |
| 8 | architecture.md:215「schema.py \| 240 行」 | **317** | ❌ 陈旧 |
| 9 | architecture.md:214「registry.py \| 140 行」 | **140** | ✅ |
| 10 | TASKS.md:324「requirements：mcp>=1.29.0,<2.0.0」 | `requirements.txt` 一致；实测装有 `mcp 1.29.0` | ✅ |
| 11 | TASKS.md:113「test_backend_client.py（56 用例）」 | 56 | ✅ |
| 12 | TASKS.md:188「实测 12 个工具就位」 | 15 | ❌ 陈旧（同 #3） |
| 13 | 派发说明「src 7 模块约 3400 行」 | `wc -l src/*.py` 合计 **3420** | ✅ |

**文档可信度评级：**
- **测试相关数字：当前高度可信**（168/9、逐文件用例数 7/8 项吻合，仅 `test_docs_sync` 少记 1）。
- **「对外工具数」数字：不可信**（仍写 12，实际 15，TASKS.md 第 28 行与 188 行两处）。
- **结构 / 行数类数字：不可信**（architecture.md 模块行数表 5 项中 4 项过期）。

---

## ⑨ 必须修的问题清单（按严重度）

| 严重度 | 问题 | 影响 | 建议 |
|---|---|---|---|
| **高** | 无任何测试跑真实 stdio 传输 / `server.main()`（覆盖 `server.py:954-996` 空） | stdout 污染、启动/退出泄漏、真实子进程启停**无 CI 护栏**；「真实后端启停回归通过」仅靠手工记录 | 增加一个 end-to-end 测试：用 `mcp.client.stdio.stdio_client` 以子进程方式拉起 `src/server.py`，调一次 `list_tools`/`call_tool` 并断言 stdout 只有合法 JSON-RPC 帧；现有 `test_protocol_roundtrip` 保留（覆盖 inner 逻辑） |
| **高** | 多实例并发读写运行时目录无测试 | A10 自愈与 `lastSynced` 反复改写是真实多实例现象，当前只单进程 fake 验证 | 增补：两进程/两协程并发写同一 `AWAM_RUNTIME_DIR`，断言原子写不产生半截 JSON、`os.replace` 语义成立 |
| **中** | TASKS.md 第 28 行 / 188 行「12 个工具」（实际 15）；architecture.md 模块行数表 4 项过期 | 文档误导「对外能力规模」与「代码规模」 | 修正为 15；行数表改为「以 `wc -l` 为准」或去掉易腐数字 |
| **中** | TASKS.md `test_docs_sync.py` 记 12、实为 13 | 用例口径不准 | 改成 13，或改为「9 固定 + data/tools 参数化」动态描述 |
| **低** | 根目录 9 个 `test_*.py` + `test_output.txt` 过期、命名误导 | 后来者可能误当活跃测试/误信过期断言 | 删除或移入 `dev_scripts/` 并改名去掉 `test_` 前缀；`test_output.txt` 删除 |
| **低** | `data/tools/*.json` 仍用 glob 参数化（空目录→1 skip 而非失败） | 覆盖度可能被静默削弱（skip 不显眼） | 为 `data/tools` 增加「非空 + 至少 N 条」显式断言，或对文件集合做 `len(...) > 0` 守卫，避免空目录静默降级 |
| **低** | `launcher_client.run_action` 超时/OSError 失败分支未覆盖（`335-339`） | 失败路径无回归 | 补 2 条用例：`TimeoutError` → `kill()` + `LauncherError`；`OSError` → `LauncherError` |

---

## 附：可复现命令索引

```bash
PY="/c/Users/Administrator/.pyenv/pyenv-win/versions/3.12.0/python.exe"
cd /g/Projects/19AI/skills/awam-tool-manager

# 全量
"$PY" -m pytest -q
# 逐文件计数
for f in tests/test_*.py; do "$PY" -m pytest "$f" --collect-only -q | tail -1; done
# 稳定性（3 轮）
for i in 1 2 3; do "$PY" -m pytest tests/test_backend_client.py tests/test_launcher_client.py -q -p no:randomly; done
# 覆盖率
"$PY" -m pytest tests/ -q --cov=src --cov=scripts --cov-report=term-missing
# 静态
"$PY" -m compileall -q src scripts tests
"$PY" -m ruff check src scripts --statistics
# stdout 污染
grep -n "\bprint\(|sys\.stdout" src/*.py
# 红绿（示例，A11）
"$PY" -m pytest tests/test_backend_client.py::test_start_cancelled_does_not_strand_starting_status -q
```
