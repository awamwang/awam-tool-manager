# awam-tool-manager 架构文档

> 本文基于**当前代码实际状态**整理（截至 2026-10-18，HEAD `944430f`：含 P0 install 门禁 `4dc8b31`、P1-04 声明式注册表重构 `ee252eb`–`a2a8079`、P1-03 errorType 契约补齐 `7f66d32`–`fce1b4b`、P1 筛选维度 `f062fa0`、P3 使用统计 `0df7b37`、P1 工具发现 `944430f`）。文中行数与行号均为**易腐数字**，精确值以 `wc -l src/*.py` / 源码为准。
>
> 历史包袱说明：仓库文档早期描述的「三层架构」含 `data/launchers/`（启动器配置）与
> `src/adapters/`（启动器适配器）。这两层已在重构中移除（`3f5257a` / `bf78c2b` / `77ed097`），
> **代码中不存在**。相关文档（SKILL.md / README.md / TASKS.md / format.md）已于本次同步更新。
>
> 协议一致性专项对比见 [`mcp-conformance.md`](mcp-conformance.md)，缺陷清单见 [`code-review.md`](code-review.md)。

---

## 1. 项目定位

一个**本地工具的统一入口层**。它本身不实现具体功能，而是把散落在各个启动器 / MCP Server 下的能力收敛成一个名单，
再以 **MCP Server** 的形式暴露给 AI 客户端，让 AI 能够：

1. 搜索 / 列出 / 查看本机已注册的工具；
2. 把调用**路由转发**到真正的后端 MCP Server 执行；
3. 管理后端 MCP Server 进程（启动 / 停止 / 状态 / 日志）。

运行形态：**stdio 模式的 MCP Server**（`python src/server.py`），由 AI 客户端（如 WorkBuddy）拉起。

---

## 2. 功能清单

### 2.1 已实现

| 能力 | 实现位置 | 状态 |
|---|---|---|
| 工具条目读取（JSON 文件即数据库） | `store.py` | ✅ |
| 工具搜索（名称 / 显示名 / 描述 / 标签模糊匹配） | `registry.py` | ✅ |
| 工具列出 + `kind` / `status` / `publisher` 筛选 | `registry.py` | ✅ |
| 调用使用统计（dispatch 出口记日志 + 按窗口聚合） | `usage.py` | ✅ |
| 本机工具发现（声明式候选清单 + 只读探测，默认零 spawn） | `discovery.py` | ✅ 代码与单测就位；真机结论由客户端调 `discover_tools` 给出 |
| 包管理器适配（列已装 / 搜可装 / 启包 / 装包，默认零 spawn） | `pkgman.py` | ✅ 读盘部分已真机实测；安装路径需 `execute=true` + `allowDestructive=true` |
| Steam 游戏库（读 `libraryfolders.vdf` + `appmanifest_*.acf` 列游戏，`-applaunch` 启动） | `steam_library.py` + `launcher_client.py` | ✅ 读盘与门禁已真机实测；真实启动要 `allowDestructive=true`（会拉起全屏客户端） |
| 工具详情查询（app / mcp 分支） | `server.py` | ✅ |
| 工具启动信息返回（默认只返回命令；`execute=true` 才启动） | `registry.py` + `app_launcher.py` | ✅ |
| 后端 MCP 路由调用 | `backend_client.py` | ✅ 已实测跑通 |
| 后端 MCP 子工具列表（带缓存） | `backend_client.py` | ✅ |
| 后端进程生命周期管理（启停 / 状态 / 日志） | `backend_client.py` | ✅ |
| 心跳检测（30s） | `backend_client.py` | ✅ |
| 工具目录自动同步到磁盘 | `backend_client.py` | ✅ |
| 后端依赖安装门禁（默认拒绝 + 授权 + 白名单） | `backend_client.py` | ✅ P0 修复，已实测 |
| MCP Resources / Prompts | `server.py` | ✅ |

### 2.2 已废弃、不应再参照的旧概念

以下均属早期版本遗留，代码中已不存在，若在某份旧文档里看到请忽略：

| 旧概念 | 去向 |
|---|---|
| `data/launchers/` 启动器配置层 | 已移除，无对应目录与 store 方法 |
| `src/adapters/` 启动器适配器 | 已移除 |
| `list_launchers` / `get_launcher_info` 工具 | 从未在当前架构中实现 |
| `mcp_tool_id` / `launcher_id` 参数 | 统一为 `tool_id` |
| `everything-mcp` 包名 | 实际为 `everything-search-mcp`（不同的 npm 包） |

---

## 3. 目录结构（实际）

```
awam-tool-manager/
├── SKILL.md                  # 技能声明（对外契约）
├── README.md                 # 项目说明
├── TASKS.md                  # 任务清单
├── requirements.txt          # mcp>=1.29.0,<2.0.0, jsonschema>=4.0.0(可选), pytest>=7.0.0
├── docs/
│   ├── format.md             # 数据格式规范
│   ├── architecture.md       # 本文档
│   ├── mcp-conformance.md    # MCP 协议一致性对比
│   └── code-review.md        # 审核报告
├── data/                     # 仓库内只有人工维护的真相源（A14 起）
│   ├── policy.json           # 部署策略（deny/白名单 + destructive.mode，ARC-P1-05 Phase 2）
│   ├── discovery.json        # 发现候选清单（discover_tools 的声明源，不是数据层）
│   └── tools/                # 工具条目：一个工具一个 JSON 文件
│       ├── everything.json            (kind=app)
│       ├── everything-search-mcp.json (kind=mcp)
│       ├── winget-mcp.json            (kind=mcp)
│       ├── powertoys.json             (kind=app)
│       ├── quicker.json               (kind=launcher)
│       └── utools.json                (kind=launcher)
├── src/
│   ├── server.py             # MCP Server 入口：协议装配 + 薄分发 + 生命周期
│   ├── tool_specs.py         # 对外 MCP 工具声明式注册表（SSOT：22 条 spec + handlers）
│   ├── policy.py             # 破坏性门禁统一判定（decide_destructive + 结构化审计）
│   ├── errors.py             # 业务错误出口（ToolError / fail / ensure_success）
│   ├── store.py              # JSON 存储层（tools 走仓库，缓存走运行时目录）
│   ├── registry.py           # 工具注册中心（含条目校验过滤）
│   ├── schema.py             # 两层数据的 JSON Schema 与校验（**文档生成源**）
│   ├── backend_client.py     # 后端 MCP 客户端与连接池
│   ├── launcher_client.py    # 启动器适配器（非 MCP：Quicker / uTools / Steam）
│   ├── app_launcher.py       # kind=app 启动执行（launch_tool 的 execute=true 路径）
│   ├── tool_help.py          # 帮助信息分层取数 + 帮助命令执行（get_tool_help 的底座）
│   ├── discovery.py          # 本机工具发现（声明清单 + 只读探测，默认零 spawn）
│   ├── pkgman.py             # 包管理器适配层（scoop：读盘列包 / 搜 bucket / 解析入口 / 构造安装命令 / 读盘查升级；UniGetUI CLI 只是可选的只读外部口径，不是后端）
│   ├── steam_library.py      # Steam 库只读解析（libraryfolders.vdf + appmanifest_*.acf）
│   ├── usage.py              # 调用使用统计：dispatch 出口记 usage.log + 按窗口聚合
│   └── runtime_paths.py      # 运行时目录解析 + 旧路径迁移
├── scripts/
│   └── generate_format_doc.py  # 由 schema.py 生成 docs/format.md 的字段表
└── tests/                    # 25 个用例文件，713 passed（含真实 MCP 会话与文档漂移检测）
                              # 另有 tests/e2e/ 9 个文件 / 11 个真实会话用例，默认不收集
    └── e2e/                  # 真实子进程 / 真实 stdio 端到端层（ARC-P3-02）：11 个用例
                              # **默认不收集**（起真实子进程、慢、依赖本机解释器），
                              # 发布 / CI 前须显式跑 `pytest -m e2e -q`；另有
                              # manual_install_gate.py（依赖本机 npm，手工跑，不进 pytest）
```

> **用例数口径（2026-10-24 更新）**：`pytest -q` = **713 passed**（另有 11 个
> e2e 用例被 deselect）。713 **不是**全部用例数——真实 e2e 层不进默认收集，
> 总数应表述为「713（默认）+ 11（e2e，`pytest -m e2e`）= 724」。

运行时数据（**不在仓库**，A14 起）：`%LOCALAPPDATA%\.awam-tool-manager/`
（环境变量 `AWAM_RUNTIME_DIR` 可覆盖；server 启动时自动从旧路径迁移）

```
%LOCALAPPDATA%/.awam-tool-manager/
├── backend-tools/                  # 后端 MCP 子工具缓存（启动时自动同步写入）
│   ├── everything-search-mcp.json
│   └── winget-mcp.json
├── launcher-actions/               # 启动器动作清单缓存
│   ├── quicker.json
│   └── utools.json
└── desired/                        # 后端启动意图（每工具一个文件，A10 跨世代自愈用）
    └── <urlencoded-tool_id>.json
```

> 根目录下的 `test_*.py`、`test_output.txt` 是调试期临时脚本，已被 `.gitignore` 排除。

---

## 4. 数据模型

### 4.1 工具条目 `data/tools/{tool_id}.json`

**一个工具 = 一个文件**，文件名即工具 ID。`store.list_tools()` 把每个文件整体当作一个工具对象返回。

```json
{
  "id": "everything",            // 必填，与文件名一致，全局唯一
  "name": "Everything",          // 技术名
  "displayName": "Everything 文件搜索",
  "kind": "app",                 // app | mcp | launcher（决定 get_tool_info 走哪个分支）
  "status": "installed",
  "description": "...",
  "version": "1.4.1",
  "publisher": "voidtools",
  "website": "...", "docUrl": "...", "icon": "",
  "launch": {                    // kind=app 时有效
    "type": "executable",        // executable | protocol | command | mcp
    "command": "C:\\...\\Everything.exe",
    "args": []
  },
  "connection": {                // kind=mcp 时有效，BackendClient 读取
    "type": "stdio",
    "command": "cmd.exe",
    "args": ["/c", "everything-search-mcp"]
  },
  "install": { "method": "npm", "command": "npm i -g ..." },  // 启动命令缺失时自动执行一次
  "update": { "method": "winget", "packageId": "voidtools.Everything" },
  "tags": ["文件搜索", "系统工具", "效率"],
  "actions": ["launch", "search"],
  "capabilities": ["search", "index"],
  "readOnly": false,
  "destructive": false,
  "metadata": { "portable": false, "free": true, "supportsRegex": true },
  "createdAt": "...", "updatedAt": "..."
}
```

`kind` 的语义作用：

- `kind == "app"` → `get_tool_info` 返回 `version` / `tags` / `launch`；`launch_tool` 默认返回启动命令，调用方显式传 `execute=true` 时才由 `app_launcher.launch_app()` 真正 spawn 进程（条目 `destructive` 需 `allowDestructive=true`）。`get_tool_help` 同理：默认只返回文档链接与帮助命令预览，显式 `run=true` 才由 `tool_help.run_help_command()` 执行并捕获输出（帮助命令取 `help.command`/`launch.command` + `help.args`，**永不复用 `launch.args`**）。
- `kind == "mcp"` → `get_tool_info` 额外返回 `backend`（运行状态）与 `backendTools`（子工具目录）；`BackendManager` 才会为其创建连接。`get_tool_help` 为其汇总子工具能力清单（不下发完整 inputSchema）。

### 4.2 后端子工具缓存 `<runtime_dir>/backend-tools/{parent_id}.json`

由 `sync_tools_catalog` 自动写入运行时目录（A14 前在 `data/backend-tools/`），
文件名 = 父 MCP 工具 ID。

```json
{
  "parent": "everything-search-mcp",
  "lastSynced": "2026-10-15T12:04:56Z",
  "source": "auto",
  "tools": [
    { "name": "search_files", "description": "...", "inputSchema": { ... } }
  ]
}
```

### 4.3 部署策略 `data/policy.json`（ARC-P1-05 Phase 2）

**部署者的显式决定**：承载「哪些工具可用、破坏性操作怎么管」。仓库文件随部署
分发、可评审；`AWAM_POLICY_FILE` 环境变量可覆盖（机私策略）。`policy.load_policy()`
在 **import 期**校验（`schema.POLICY_SCHEMA`，未知字段一律拒绝）——坏策略让进程
启动失败，而不是静默失效。

```json
{
  "version": 1,
  "tools": { "deny": [], "allow": null },      // deny 优先；allow=null 表示不启用白名单
  "destructive": { "mode": "require_confirm" }, // deny | require_confirm | allow
  "audit": { "destructive": true }             // 判定行追加 <runtime>/audit.log
}
```

判定链（单次调用）：`dispatch_tool` 先 `policy.check_call(name)`（deny 名单 /
白名单，命中即 `permission_denied`，不进 handler）→ handler 内
`policy.decide_destructive(...)`（`destructive.mode` 参与：`deny` 命中即拒绝且
**不读**客户端 `allowDestructive`——策略赢；`allow` 跳过确认；默认
`require_confirm` 维持现行为）。

### 4.4 发现候选清单 `data/discovery.json`（P1）

**不是数据层**：`data/tools/` 说「登记了什么」，这份清单说「该去找什么」，
所以它不进 `schema.py`（不生成字段表），校验源是 `discovery.validate_manifest()`，
`AWAM_DISCOVERY_FILE` 可覆盖。失败时机与策略文件刻意相反——发现不是安全面，
坏清单只让 `discover_tools` 这一次调用返回 `config_error`，不让整个服务起不来。

探测面四档，**默认只读系统状态**：

| 探测 | 能否下结论 | 说明 |
|---|---|---|
| `path_exists` | 能 | 声明的多个位置逐个试，第一个存在即命中；位置用 `{ENV}` 模板 |
| `registry_value` | 能 | 读安装器自己维护的登记值（`hive`/`key`/`value`），默认还要验证值指向的路径存在；补的是声明式路径穷举不出「装在非常规盘」的盲区 |
| `dir_count` | 不能 | 只补充证据（装了几个包）——目录空不等于没装 |
| 命令是否在 PATH | 能 | `resolve_executable()`（内含 `shutil.which`），**不 spawn**；版本号要显式 `run_commands=true` |

`registry_value` 的四档读数里只有「键/值不存在」「值不是非空字符串」「登记值指向的
路径已不存在」算 miss，权限不足 / 非 Windows / `REG_EXPAND_SZ` 未能展开展不到都算
inconclusive——**「没检查成」不能伪装成「没装」**，与 `{ENV}` 取不到判 inconclusive
同一条纪律。这条探针来自一个真机假阴性：Steam 装在 `S:\Program Files (x86)\Steam`，
清单只声明了 `C:\Program Files` 两处，于是明明装着却判 `not_installed`（中信心）。

`{ENV}` 取值有**两个来源**：先读进程环境，取不到再回退查 Windows 注册表
（`HKCU\Environment` → `HKLM\...\Session Manager\Environment` → `ProgramFilesDir` /
`ProgramFilesDir (x86)`）。回退是必需的而不是锦上添花：带 server 的宿主给多少环境
变量是宿主的决定——mcp SDK 只透传 `DEFAULT_INHERITED_ENV_VARS` 白名单（含
`PATH` / `PATHEXT` / `LOCALAPPDATA`，**不含** `ProgramFiles`、`ProgramFiles(x86)`、
用户级 `SCOOP`），别的宿主又缺另一套，只读进程环境会把大片真实存在的安装位置判成
`unknown`（真机实测：SDK 通道 4 个假 unknown → 0 个；Qoder 原生通道缺
`ProgramFiles(x86)` 与 `SCOOP`，steam 曾因此判 unknown、scoop 的 `dir_count` 报
「未能展开: SCOOP」，回退后两者都给出确定结论）。

结论三态 `hit` / `miss` / **`inconclusive`**：`{ENV}` 两处都取不到就是「这个位置没检查成」，
绝不回退成相对路径去试存在性——那会把「没设环境变量」伪装成「这个路径不存在」。
`status` / `kind` 与工具条目共用词表，所以发现结果能直接回填 `list_tools` 的筛选参数。
字段表与示例见 [`format.md`](format.md) 的「发现候选清单」一节。

### 4.5 安装器的本机布局（P2-07，`src/pkgman.py` 的读盘对象）

这节记的是**别人写在盘上的东西**，不是仓库数据，所以没有 schema、没有校验器，
只有实测结论。下面每一行都决定了适配层的一处形状（全部来自本机 `S:\Libs\Scoop`
的勘察，不是从 scoop 文档推的）：

| 观察 | 形状 | 适配层的应对 |
|---|---|---|
| 已装状态 | `apps/<name>/current/manifest.json`（版本 / `bin` / `shortcuts` / 描述 / homepage / 架构） | 直接读，不跑 `scoop list` |
| `current` 会退化 | 本机 14 个 app 里 **6 个** 的 `current` 是空目录（`bayes` / `clink` / `ffmpeg-shared` / `innounp` / `oh-my-posh` / `scrcpy`） | 回落最新的版本目录，并如实标 `state=current_broken`（**不是**当成正常安装） |
| bucket 不在 manifest 里 | 同版本目录的 `install.json`：`{"bucket": "main", "architecture": "64bit"}`；直链安装写 `url` 而非 `bucket` | bucket 从 `install.json` 取，取不到就 `null` |
| 「没有 manifest」是合法状态 | `scoop` 自己的 `current` 是 git 工作区 | `state=no_manifest`，不抛异常 |
| `bin` **不是必填** | 真入口是 `shims\<包名>.exe|.cmd|.ps1`（`bayes` 有 shim 无 bin、`scoop` 无 manifest 有 shim） | 候选按 快捷方式 → bin → shim 排；`launchable` 与 `launch_target` 共用同一批候选，一行数据不许有两种答案 |
| 扩展名决定能不能直接 spawn | 实测：`CreateProcess` 起得了 `.cmd`，`.ps1` 抛 `OSError [WinError 193]` | 候选带 `runnable`；默认只挑「在盘上且可 spawn」的，`.ps1` 永不作为默认入口 |

可安装的包同理只读盘：`buckets/<bucket>/bucket/*.json`，**文件名即包名**（本机 4 个
bucket / 4287 份清单），所以先按文件名筛、只对命中项读 JSON。

只有两个动作会起子进程，且都要显式参数：`launch_package(execute=true)` 复用
`app_launcher.launch_app`（stdio 三件套 DEVNULL + 保活注册表，与 `launch_tool` 同一条
路），`install_package(execute=true, allowDestructive=true)` 跑
`<root>\shims\scoop.cmd install <包>`（`anyio.fail_after` 限时、stdout/stderr 各留尾部
4000 字符）。包名过 `validate_name` 字符集白名单——它会参与路径拼接，`..` 与分隔符一律拒。

`winget` / `npm` **未接线**（`BACKENDS = ("scoop",)`）：两者都没有可靠的纯读盘清单来源，
列包必须 spawn，属执行面。`winget` 另立项再接；**`npm` 决定不接**（2026-10-19 用户口径：
npm 官方无 MCP server，需求澄清不到可选型的程度）——将来真要接，正确形态是给 `pkgman.py`
加一个 `npm` 后端（全局包读 `<prefix>/node_modules/*/package.json`），不是新增一条 MCP 接入项。

### 4.6 Steam 的本机布局（P0，`src/steam_library.py` 的读盘对象）

同 4.5：这节记的是**别人写在盘上的东西**，没有 schema、只有实测结论（本机
`S:\Program Files (x86)\Steam`），每条都决定了适配层的一处形状。

| 观察 | 形状 | 适配层的应对 |
|---|---|---|
| 权威位置不在 `Program Files` | 注册表 `HKCU\Software\Valve\Steam` 的 `SteamPath` / `SteamExe`；本机装在 `S:` 盘，值还是**正斜杠**（`s:/program files (x86)/steam`） | `resolve_root()`：条目 `dataDir` → 注册表 → 穷举常见位置（来源写明「未经登记值确认」）；正斜杠归一化成路径 |
| 库清单 | `steamapps/libraryfolders.vdf`：顶层 `libraryfolders`，数字键下 `path` / `label` / `apps{appid: size}` | 出库列表与 `declaredAppTotal`，路径按根目录相对展开 |
| 「装着」的证据只有 acf | `steamapps/appmanifest_*.acf`：顶层 `AppState`，取 `appid` / `name` / `installdir` / `StateFlags` / `SizeOnDisk` / `LastPlayed` / `buildid` | **games 只来自 acf**；vdf 声明而盘上没有的 appid 不进清单 |
| 库会失踪 | 本机 vdf 声明 3 库 / 27 个 appid，盘上只有 16 份 acf（`S:` 5 + `D:\03Games\SteamLibrary` 11），`E:\Games\SteamLibrary` 目录已不存在 | 失踪库进 `missingLibraries` + note，`source=partial`——不造出 11 个点不开的游戏 |
| VDF 是嵌套文本不是 JSON | 制表符缩进的 `"key" "value"` / `"key" { … }`，路径里反斜杠**成对**出现（`\\`） | 自写 tokenizer：截断的块、键后没有值 → 抛 `SteamError` 快速失败；显式写全的空 `{}` 接受 |

启动只有一条路：`steam.exe -applaunch <appid>`（`run_launcher_action`，action id 形如
`app:400040`）。ID 在 spawn **之前**校验（非数字直接拒），命令缺失时回退注册表
`SteamExe`；执行站点复用 `app_launcher.launch_app`，不新开 spawn 路径。客户端是单实例、
命令只做投递，退出码 0 只代表 steam.exe 收到了请求，故返回体带 `deliveryOnly: true`。

---

## 5. 模块架构

### 5.1 分层

```
┌──────────────────────────────────────────────────────────┐
│  AI 客户端（WorkBuddy 等）                                │
└───────────────────────┬──────────────────────────────────┘
                        │  MCP / stdio (JSON-RPC)
┌───────────────────────▼──────────────────────────────────┐
│  接口层  src/server.py（协议装配 + 薄分发 + 生命周期）   │
│   · 22 个 Tool 定义 + 分发：tool_specs.py 声明（SSOT）  │
│   · errors.py：业务错误出口（fail / ensure_success）    │
│   · 3 个 Resource、3 个 Prompt                           │
│   · 全部返回值统一 JSON 序列化                            │
│   · 启动时 migrate_legacy_runtime（旧路径数据迁入运行时目录）│
└───────┬───────────────────────────────┬──────────────────┘
        │ 查询类                         │ 路由/管理类
┌───────▼───────────────┐   ┌───────────▼──────────────────┐
│ 领域层 registry.py      │   │ 路由层 backend_client.py     │
│  · list_all_tools      │   │  · BackendManager（连接池）   │
│  · search_tools        │   │  · BackendClient（单连接）    │
│  · get_tool            │   │    启动/停止/状态/日志/心跳    │
│  · launch_tool         │   │    工具列表缓存（5 分钟）     │
│                         │   │  launcher_client.py          │
│                         │   │  · LauncherManager（Quicker/uTools）│
└───────┬───────────────┘   └───────────┬──────────────────┘
        │                               │
┌───────▼───────────────────────────────▼──────────────────┐
│ 存储层  store.py（JsonStore）                           │
│  · data/tools/          读取工具条目（仓库内，人工维护）  │
│  · runtime_dir/backend-tools/     读写后端子工具缓存      │
│  · runtime_dir/launcher-actions/  读写启动器动作清单      │
└───────────────────────┬──────────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────────┐
│  后端 MCP Server（子进程，如 everything-search-mcp）      │
│  启动器外部命令（一次性进程，如 QuickerStarter.exe）       │
└──────────────────────────────────────────────────────────┘
```

### 5.2 模块职责

> 行数为**易腐数字**（每次提交都会变），这里只保留数量级参考，精确值以
> `wc -l src/*.py` 为准；新增/移除模块时必须同步本表。

| 模块 | 行数（约） | 职责 | 依赖 |
|---|---|---|---|
| `store.py` | 194 | JSON 文件的读写与目录遍历，读有容错、写走原子替换，无缓存 | 标准库 |
| `runtime_paths.py` | 93 | 运行时目录解析（`%LOCALAPPDATA%\.awam-tool-manager`，`AWAM_RUNTIME_DIR` 可覆盖）+ 旧路径数据一次性迁移 | 标准库 |
| `schema.py` | 470 | 两层数据的 JSON Schema 与校验（`jsonschema` 可选，缺失时降级）；**同时是 `docs/format.md` 字段表的唯一生成源** | `jsonschema`(可选) |
| `registry.py` | 188 | 工具查询语义（列表 / 搜索含 `id` / 详情 / 启动信息），加载时过滤不合规条目；`kind` 之外的筛选维度（`status` 精确、`publisher` 子串，均大小写不敏感、先归一化再判空）集中在 `_apply_filters` | `store`、`schema` |
| `backend_client.py` | 1593 | 后端 MCP 子进程的连接管理、调用转发、缓存、心跳、日志、**install 执行门禁（P0）** | `mcp`、`store`、`runtime_paths` |
| `launcher_client.py` | 761 | 启动器适配器（非 MCP，Quicker / uTools / Steam）：Quicker 走 states 目录解析 + `runaction:` 命令行回传；uTools 走 `plugins/*.asar` 的 plugin.json 纯 Python 解析（Chromium Pickle 封包）+ `utools://` 协议唤起（fire-and-forget）；Steam 走 `steam_library` 读盘列游戏 + `steam.exe -applaunch <appid>`（执行委托给 `app_launcher.launch_app`，ID 在 spawn 前校验、命令缺失回退注册表 `SteamExe`，结果标 `deliveryOnly`）。适配器判别靠 `launcher.type`，`SUPPORTED_ADAPTERS` 必须与 `schema` 的 enum 逐字一致（有守卫测试） | `store`、`steam_library`、`app_launcher` |
| `steam_library.py` | 282 | Steam 库**只读**解析（不动客户端、不联网）：`registry_steam_values()` / `steam_executable()`（`HKCU\Software\Valve\Steam`，只 `QueryValueEx`）+ `resolve_root()`（条目 `dataDir` → 注册表 `SteamPath` → 穷举兜底，兜底来源写明「未经登记值确认」；登记值是正斜杠，统一归一化）+ `parse_vdf()`（自写 tokenizer：嵌套块、成对 `\\` 转义、值可以是字符串化的数字；截断的块与「键后没有值」抛 `SteamError` 快速失败，显式写全的空 `{}` 接受）+ `_library_paths()`（无 vdf 只当主库并如实标 `libraryFolders=missing`）+ `read_manifest()`（无 `AppState` / 无 `appid` 的清单跳过并给原因）+ `installed_games()`（**games 只来自 acf**，vdf 声明的 appid 只汇成 `declaredAppTotal`；失踪库进 `missingLibraries` + note，逐库带 `exists` / `manifests`） | 标准库（Windows 另用 `winreg`） |
| `errors.py` | 92 | 业务错误出口：`ErrorType` 常量枚举 + `ToolError` / `fail` / `ensure_success`（P1-04 从 server.py 原样搬出——tool_specs 的 handlers 要用，留在 server.py 会形成循环 import；server 命名空间继续 re-export；P1-03 补齐枚举至 16 种） | 标准库 |
| `app_launcher.py` | 188 | `kind=app` 的启动执行（`launch_tool` 的 `execute=true` 路径）：`create_subprocess_exec` 无 shell、stdio 三件套 DEVNULL（A8）、`shutil.which` 预检、观察窗口（默认 5s / 封顶 30s）区分「早退报退出码」与「仍在运行报 running」；运行中进程由模块级注册表强引用保活（CPython transport.close() 会 kill 运行中子进程） | `errors`、`anyio` |
| `tool_help.py` | 256 | 帮助信息分层（`get_tool_help` 的底座）：`resolve_help_command()`（`help.command` > `launch.command`，`help.args` > `--help`，**永不复用 `launch.args`**；timeout 默认 5s / 封顶 30s）+ `entry_help` / `backend_summary` / `launcher_summary` 纯取数 + `run_help_command()`（`create_subprocess_exec` 无 shell、PIPE 捕获、UTF-8→GBK→replace 三级解码、超时必 kill、>20000 字符截断、非零但有文本仍算成功） | `errors`、`anyio` |
| `usage.py` | 209 | 调用使用统计（P3）：`record_call()` 在 dispatch 每个出口 append 一行 JSONL 到 `<runtime>/usage.log`（**刻意不记参数**；落盘失败只向 stderr 告警，绝不把「统计写不进去」变成「工具调用失败」）+ `read_rows()` 容错读取（缺文件算空、坏行跳过并汇总告警一次）+ 日历日窗口 + `aggregate()` 纯函数折成 byName / targets / byDay / errorTypes + `window()` 让结果自证口径。选日志而非内存计数器：网关会整池回收进程，计数必须在盘上才是真相源 | `runtime_paths`、标准库 |
| `discovery.py` | 653 | 本机工具发现（P1）：`load_manifest()`（文件不存在算空清单，坏文件抛 `DiscoveryError`）+ `validate_manifest()`（调用期校验，与 `policy` 的 import 期快速失败刻意不同；`registry_value` 探针按类型分支校验 `hive`/`key`/`value`/`require_exists`）+ `expand_path()`（`{ENV}` 模板：进程环境优先、取不到回退 `registry_env_value()` 查注册表，两处都没有才判「没检查成」，**绝不退化成相对路径**）+ `_read_reg_string()` / `_probe_registry_value()`（注册表登记值：读数分 `ok`/`absent`/`empty`/`unreadable`/`unexpanded` 四档，只有前者能下结论）+ `run_probes()` / `fold_verdicts()`（`hit` / `miss` / `inconclusive` 三态；未接线的探针类型一律判 inconclusive，不让 `Path("") == "."` 假命中）+ `resolve_executable()`（Windows 上校验 which 结果的后缀，无扩展名垫片按「目录外层 / PATHEXT 内层」重扫 PATH 换成真目标）+ `probe_command()`（默认只解析 PATH 不 spawn，`run=true` 才用绝对路径无 shell 执行、`stdin=DEVNULL`、按 `version_pattern` 取版本号，超时/OSError 都折成结论不抛）+ `discover()`（纯编排，折成 `status` / `confidence` / `registered` + summary）+ `note_for()`（自证探测面是声明出来的、不是穷举） | 标准库（Windows 另用 `winreg`） |
| `pkgman.py` | 912 | 本机包管理器适配层（P2-07，`list_packages` / `search_packages` / `launch_package` / `install_package` / `check_package_updates` 的底座）：`resolve_root()`（进程环境 `SCOOP` → `registry_env_value` 回退 → `%USERPROFILE%\scoop`，取不到就说取不到）+ `list_installed` / `find_installed`（**纯读盘**，三态 `ok` / `current_broken` / `no_manifest`：`current` 断链回落版本目录、bucket 取自 `install.json`、`bin` 写成字符串时归一、无 `bin` 时用 `shims/<包名>`）+ `_candidates` / `launch_target`（候选按 快捷方式 → bin → shim 排序，每项带 `exists` / `runnable`；`runnable` 按实测可 spawn 的后缀判定——`.cmd` 可、`.ps1` 抛 WinError 193；`launchable` 与 `launch_target` 共用同一批候选，一行数据不许有两种答案）+ `search_packages`（按 bucket manifest **文件名**筛、命中才读 JSON：本机 4 bucket / 4287 份清单，全量读是几百毫秒级浪费）+ `install_argv` / `run_argv`（只构造 / 异步执行，`stdin=DEVNULL` + `anyio.fail_after`；输出按 `max_output` **留尾部**并给 `stdoutTruncated`——留尾对日志是对的，对 JSON 是拦腰砍，所以 `run_argv` 多了一个上限参数，结构化输出用 `UNIGET_OUTPUT_MAX_CHARS`）+ 升级查询（2026-10-24）：`version_key`（逐段比数值、非数字段回落小写串，从 `_version_dirs` 提到模块级）+ `bucket_version`（`buckets/<bucket>/bucket/<包名>.json`，路径拼接走 `_abs_under` 防穿越）+ `read_disk_updates`（已装版本 vs 本地 bucket 索引，零 spawn；比不了的进 `unknown` 给成因，不猜「已最新」；`MAX_UPDATE_ROWS=100` 截断）+ `validate_uniget_manager`（17 个稳定 id 白名单，`--elevated` 这类选项串按 `invalid_params` 拒）+ `locate_uniget_cli`（`AWAM_UNIGET_CLI` > `%LOCALAPPDATA%\Programs\UniGetUI` > `Program Files\UniGetUI`，**不走 PATH**，取不到就枚举试过的路径）+ `uniget_status_argv` / `uniget_updates_argv` + `parse_uniget_running`（只有 `running` 是布尔 true 才算有会话）+ `parse_uniget_updates`（先看 `status`，数组键必须是 `updates`——`package installed` 才用 `packages`，读错键会得假阴性；`id` 作规范名）；`PackageError` 自带 `error_type`，handler 不做文案猜测 | `discovery`（仅 `registry_env_value`）、`errors`、`anyio` |
| `tool_specs.py` | 3027 | 对外 MCP 工具声明式注册表（SSOT）：34 条 `ServerToolSpec`（name/description/inputSchema/annotations/handler/exposure 同条记录）+ 34 个 handler + `build_mcp_tools(mode)` / `get_spec()` / `validate_specs()`（import 期自检：重名/空 handler/非法注解键快速失败） | `mcp`、`errors`、`launcher_client`、`app_launcher`、`tool_help`、`discovery`、`pkgman`、`policy`、`usage` |
| `policy.py` | 483 | 破坏性门禁 + 部署策略：`decide_destructive(kind, tool, sub_op, args)`（mcp 子工具精确匹配 / launcher 动作级 glob / app 条目级 / package 按操作 fail-closed 白名单 `PACKAGE_CONFIRMED_OPS`；`destructive.mode` 参与判定）+ `check_call(name)`（dispatch 前置 deny/白名单）+ `load_policy()`（import 期校验，坏策略快速失败）+ 结构化审计行（stderr + `<runtime>/audit.log`） | `runtime_paths`、`schema`、标准库 |
| `server.py` | 525 | MCP 协议入口：协议装配 + 查表薄分发（`dispatch_tool` 约 40 行：策略前置 → 查表 → handler → 统一 `json.dumps`，五个出口各记一行使用统计）+ Resources/Prompts + 生命周期（启动预热/状态恢复），启动时后台检测依赖 + 恢复期望状态 | `mcp`、`store`、`registry`、`backend_client`、`runtime_paths`、`launcher_client`、`schema`、`errors`、`tool_specs`、`policy`、`usage` |
| `scripts/generate_format_doc.py` | 148 | 读 `schema.py` 渲染字段表并就地替换 `docs/format.md` 的生成区块；`--check` 做漂移检查 | 标准库、`schema` |

### 5.3 后端路由调用链

```
call_backend_tool(tool_id, tool_name, arguments)
  └─ BackendManager.get_client(tool_id)          # 首次调用时读取 data/tools/{id}.json
       └─ 校验 kind == "mcp" 且存在 connection  →  构造 BackendClient（懒加载）
  └─ BackendClient.call_tool(tool_name, arguments)
       ├─ 状态检查（非 running 抛 ConnectionError）
       ├─ session.call_tool(...)                 # 转发到后端子进程
       ├─ 检查 result.isError                    # 后端报错 → RuntimeError
       └─ 拼接 content[*].text 返回
  └─ 统一包装：{success, result} 或 {success, error, errorType}
```

### 5.4 后端连接生命周期

```
                    start()
        stopped ─────────────► starting ──────────► running
           ▲                      │                    │
           │                      │ 失败                │ 心跳失败 / stop()
           └────── stop() ────────┴──── error ◄─────────┘
```

- `start()` 成功后会**预热工具列表**并启动心跳任务（间隔 30s，探测超时 10s）；
- `start()` 在 spawn 子进程前会先走一遍 `ensure_installed()`。该方法的策略见
  「install 执行门禁」小节——**默认不执行安装命令**，未授权且依赖缺失时直接返回
  `install_required` 而不 spawn；
- `BackendManager.start_backend()` 在启动成功后**自动执行一次 `sync_tools_catalog`**；
- 心跳失败会把状态置为 `error` 并**主动清理连接**（不再留僵尸子进程）；
- 调用超时同样会把连接标记为 `error` 并后台清理，让后续请求快速失败而不是继续卡住。

**并发与清理约束**：

- `start()` 与 `stop()` 共用 `_lock`，避免状态与真实连接不一致；
- `start()` 开头会清理上次残留的 context（例如心跳失败后遗留的）；
- 所有清理统一走 `_cleanup()`，用独立的 `_cleanup_lock` 保证**串行且幂等**。

**连接独占任务（anyio 作用域硬约束）**：

`mcp` SDK 的 `stdio_client` 与 `ClientSession` 的 `__aenter__` 都会进入各自内部
TaskGroup 的取消作用域，而 anyio 的 `CancelScope` **绑定进入它的任务**——进入与
退出不在同一任务内就会 `RuntimeError: Attempted to exit a cancel scope...`。
因此整条连接（spawn → 握手 → 持有 → 拆除）由 `_connection_loop` 这一个独占任务
持有，两个 context 都用 `async with` 在同一任务内闭合；请求 / 心跳 / 清理任务只
通过 `self.session`（memory stream + future，跨任务安全）与 `_ready` / `_shutdown`
事件协作。取消路径（start 超时 / 调用方被取消）走 `task.cancel()`，
`CancelledError` 在 `async with` 的退出逻辑中传播，拆除照常。

> 2026-10-16 实证：toolport 网关「启动即崩 / 调用超时」是同族故障——
> `asyncio.wait_for` 从外部任务取消协程会撕裂作用域栈；修复前客户端其实已收到
> 成功响应，崩溃发生在响应之后（进程 exit 1）。

**启动状态机的取消安全（A11）**：`start()` 在持锁后先把 status 置为
`starting`（621 行），其外层异常兜底必须覆盖 `BaseException`——
`CancelledError` 在 Python 3.8+ 继承自 `BaseException`，`except Exception`
接不住。上游有多个现实的取消源：toolport 网关对每次 tools/call 有 30s 预算
（超时取消请求）、daemon 周期回收、调用方断开。若取消落在 `starting` 之后而
无人收回，之后所有 get/status/list 永远只能看到
「后端未运行，当前状态：starting」，没有任何自愈路径（2026-10-16 实测卡死
13+ 分钟）。修复：`except BaseException` 兜底——status 落地为 stopped、
收尸已创建的连接/心跳，取消继续上抛不吞。

**子进程 stdin 隔离**：`ensure_npm_bin_on_path()` 与安装命令的 spawn 一律传
`stdin=DEVNULL`。server 自身经 stdio 管道与网关通信，子进程若继承这条 stdin，
npm/cli 链会阻塞在读 stdin 上永不返回（表现为启动挂死 → 网关 30s 超时）。

**install 执行门禁（P0 修复，2026-10-17）**：

`install.command` 与 `connection.command` 有本质区别：后者是「启动本工具必需的
参数」，前者是一条**会改系统状态的命令**，且写在**进版本库**的
`data/tools/*.json` 里。旧逻辑让它在两条路径上被无条件
`create_subprocess_shell` 执行——服务启动预热（`_prewarm_backends` →
`ensure_all_installed`）与 `start_backend`（`start()` 前置 `ensure_installed`），
既无调用方意图，也无任何 `destructive` / `allowDestructive` 门禁，与
「副作用必须由显式调用触发」的取舍正好相反（最小权限倒置；对照：`launch_tool`
默认只返回命令，只有显式 `execute=true` 才 spawn）。

修复后是三段式门禁，全部收在 `BackendClient.ensure_installed()` 内：

1. **授权**：环境变量 `AWAM_AUTO_INSTALL` 为真（部署者显式打开）**或**条目
   `install.auto === true`（逐工具 opt-in）。两者都缺 → 不执行，返回
   `installBlocked="unauthorized"`；
2. **命令白名单**：授权后仍要「无 shell 元字符」+「前缀命中
   `INSTALL_COMMAND_ALLOWLIST` 且前缀后跟有包名」。`&&` / `;` / `$()` 能在
   前缀合法的情况下串联任意命令，所以这两条必须同时满足；
3. **失败可见**：被拦下且依赖确实缺失时，`start()` **不 spawn**，直接返回带
   `errorType` 的结构化失败（`install_required` / `install_not_allowed`），
   而不是丢一个「命令不存在」的原生错误把真实原因盖掉。

连带变化：服务启动预热**退化为纯检测**——只读 PATH、不执行任何外部命令，
在 stderr 报告「谁缺依赖、缺什么、怎么开授权」。想恢复旧行为由部署者在 MCP
客户端配置里显式设 `AWAM_AUTO_INSTALL=1`，这是**人的决定**，不由程序默认代劳。

> 边界说明：`ensure_npm_bin_on_path()` 仍会在启动时执行**硬编码**的
> `npm prefix -g`。它不是配置驱动、无注入面，且是 PATH 自愈（A7）的必要
> 前提，因此未纳入门禁；如威胁模型要求「启动零外部命令」，可一并加开关。

**期望状态持久化与跨世代自愈（A10）**：

toolport 网关会**周期性整池回收**它拉起的 server 进程（2026-10-16 实测约每
几分钟一批：整批 downstream server 同时重生，回收走「关 stdin → 本服务优雅
退出 → close_all 停掉全部后端」，不留孤儿）。每次回收后新 server 世代内存
状态清零——stdio MCP server 的生命周期归网关管，进程内状态注定随进程消亡，
用户视角就是「启动后保不住，每次都得重新初始化」。对策：

- `start_backend` 成功 → 把启动意图写入运行时目录的 `desired/{tool_id}.json`
  （每工具一个文件、只写自身键，避免多实例「读全表 → 写全表」的 lost update；
  单文件原子写；单文件损坏跳过该条，绝不阻断启动。旧版单文件
  `backend_desired_state.json` 加载时自动迁移）；
- 每个新 server 世代启动时，`_restore_backends()` 后台任务据意图自动拉起
  desired=running 的后端——用户跨网关回收也能随时查到 running；
- `stop_backend` 成功 → 清除意图（用户明确说不要了，下代不再拉起）；
- 意图对应的工具已删除 → 自动清掉过期记录。
- 已知边界：若 server 被**硬杀**（非优雅退出），当代后端会孤儿化，下代自动
  恢复时会再起一个（stdio 后端无端口冲突，可共存）；要「永不重启」需把后端
  生命周期移出网关控制的 server 进程（外部 supervisor），属于后续架构演进。

---

## 6. 对外 MCP 契约（实际实现）

### 6.1 Tools（22 个）

后端相关工具统一使用 **`tool_id`** 指定目标工具（`mcp_tool_id` / `launcher_id` 已废弃）。

> **P1-04 起，本表与 `tools/list` 的实际下发由 `src/tool_specs.py` 的声明表
> 单一来源产出**（22 条 `ServerToolSpec`）：注解、`inputSchema`、`outputSchema`
> 与工具名同条记录，`dispatch_tool` 查表分发。下表的参数/说明与 spec 表的
> 一致性由 `tests/test_tool_specs.py` 的 golden 五字段比对（采集自重构前
> HEAD）与三方名字守卫锁定。

| 工具名 | 参数 | 说明 |
|---|---|---|
| `search_tools` | `keyword`(必填)、`kind`、`status`、`publisher` | 关键词搜索，三个筛选维度取交集 |
| `list_tools` | `kind`、`status`、`publisher` | 列出工具（可按类型/安装状态/发布者筛） |
| `get_tool_info` | `tool_id`(必填) | 工具详情（按 kind 返回不同字段） |
| `launch_tool` | `tool_id`(必填)、`execute`、`wait`、`allowDestructive` | 返回启动命令（默认不执行）；`execute=true` 才 spawn |
| `call_backend_tool` | `tool_id`、`tool_name`(必填)、`arguments`、`allowDestructive` | 路由调用（A12：命中 `destructiveTools` 子工具时需确认） |
| `list_backend_tools` | `tool_id`(必填)、`force_refresh` | 后端子工具（含 schema） |
| `start_backend` | `tool_id`(必填) | 启动后端 + 自动同步 |
| `stop_backend` | `tool_id`(必填) | 停止后端 |
| `get_backend_status` | `tool_id` | 单个或全部状态 |
| `get_backend_logs` | `tool_id`(必填)、`level`、`limit` | 日志查询 |
| `set_backend_log_level` | `tool_id`、`level`(必填) | 设置日志级别 |
| `sync_backend_tools` | `tool_id`(必填) | 手动同步子工具目录 |
| `list_launcher_actions` | `tool_id`(必填)、`keyword`、`limit`、`refresh` | 列出/搜索启动器动作（返回带 `lastSynced`/`source`，缓存超 5 分钟自动重扫） |
| `run_launcher_action` | `tool_id`(必填)、`action_id`、`action_name`、`param`、`wait`、`allowDestructive` | 执行启动器动作 |
| `sync_launcher_actions` | `tool_id`(必填) | 手动同步启动器动作清单 |
| `get_tool_help` | `tool_id`(必填)、`run`、`timeout`、`allowDestructive` | 帮助信息分层取数（文档链接 / 后端能力清单 / 启动器动作清单 / 命令预览，默认不 spawn）；`kind=app` 显式 `run=true` 才执行帮助命令并捕获输出 |
| `get_usage_stats` | `days`、`tool_id` | 调用使用统计（只读 `<runtime>/usage.log`，标 `readOnlyHint`）：按天 / 按对外工具 / 按目标 `tool_id` 聚合调用量、成败与耗时 |
| `discover_tools` | `run_commands`、`status` | 按 `data/discovery.json` 发现本机工具：路径存在 / 目录条目数 / 注册表登记值 / 命令是否在 PATH，默认**只读不 spawn**（`run_commands=true` 才执行版本命令），输出 `status` + `confidence` + `registered` + 逐条证据；标 `openWorldHint`（能力面含执行，故**不声明** `readOnlyHint`） |
| `list_packages` | `manager`、`state` | 列安装器已装的包（当前 `scoop`）：**纯读盘**（`apps/*/current/manifest.json`，断链回落版本目录，bucket 取 `install.json`，无 `bin` 时用 `shims/<包名>`），每包 `state`（ok / current_broken / no_manifest）+ `launchable`；标 `readOnlyHint` |
| `search_packages` | `keyword`(必填)、`manager`、`limit` | 在已添加 bucket 的清单里搜可安装的包：按 manifest 文件名筛、命中才读 JSON，**不 spawn**；排序 完全同名 → 前缀 → 包含；标 `readOnlyHint` |
| `launch_package` | `manager`、`package`(必填)、`entry`、`execute`、`wait` | 启动已装的包：入口按 快捷方式 → bin → shim 取，只挑「在盘上且扩展名可直接 spawn」的候选；默认只预览，`execute=true` 才与 `launch_tool` 同一条 spawn 路径；`openWorldHint`、**不**要求 `allowDestructive`（见 §6.6 包种） |
| `install_package` | `manager`、`package`(必填)、`execute`、`allowDestructive`、`timeout` | 装一个包：默认只回 `argv` 与 `alreadyInstalled`；`execute=true` + `allowDestructive=true` 才执行，跑完读回 `apps/<包>`（退出码 0 但读不到时明示「不要当成安装成功」）；标 `destructiveHint` |

**失败语义**：一律抛出 `ToolError`，由 MCP SDK 转成 `CallToolResult(isError=True)`，
正文是可解析的 JSON（`success` / `errorType` / `error`）。
详见 [`mcp-conformance.md`](mcp-conformance.md#21-严重问题iserror-从未置位-已修复)。

### 6.2 Resources（3 个）

| URI | 内容 |
|---|---|
| `awam://mcp-servers` | 所有 `kind=mcp` 的工具 |
| `awam://tools` | 所有已注册工具 |
| `awam://backends/status` | 所有后端运行状态 |

### 6.3 Prompts（3 个）

`getting_started`、`how_to_search`、`how_to_call_backend`
（示例参数已于本轮修正为 `tool_id`，见审核报告 D16）

---

## 7. 运行时行为

### 7.1 启动流程

```
python src/server.py
  → 计算 DATA_DIR = <项目根>/data（仅工具条目）
  → 计算 RUNTIME_DIR = %LOCALAPPDATA%\.awam-tool-manager（AWAM_RUNTIME_DIR 可覆盖）
  → migrate_legacy_runtime()：旧路径的缓存/意图文件迁入运行时目录（一次性）
  → 构造 JsonStore / ToolRegistry / BackendManager（同步，无 IO 校验）
  → 注册 MCP handlers
  → 打印已加载工具数量到 stderr
  → 创建后台任务 _prewarm_backends()：为各后端装好缺失的依赖（只装不启，异常全吞）
  → 创建后台任务 _restore_backends()：按期望状态文件恢复上次运行过的后端（A10 跨世代自愈，异常全吞）
  → stdio_server() → app.run() 事件循环
  → finally: 取消预热/恢复任务 → backend_manager.close_all()
```

### 7.2 关键设计点

| 设计 | 评价 |
|---|---|
| 日志统一输出 stderr，不污染 stdout 协议通道 | ✅ 正确且关键 |
| 所有工具返回统一 JSON 字符串 | ✅ 便于 AI 解析 |
| 后端连接懒加载 + 连接池缓存 | ✅ 合理 |
| 工具列表缓存（`TOOLS_CACHE_TTL` = 300s）+ `force_refresh` | ✅ 合理 |
| 子进程目录同步到磁盘 | ✅ 便于离线查询；空结果不覆盖已有缓存 |
| 后端调用统一超时（默认 30s，`connection.timeout` 可覆盖） | ✅ 避免子进程卡死拖垮服务 |
| `start` / `stop` 共用锁 + 独立清理锁 | ✅ 状态与真实连接一致，清理幂等 |
| 后端依赖自装 + 启动预热（`ensure_installed` / `ensure_all_installed`） | ✅ `install` 元数据从死数据变为真能力：缺失才装、只装一次、装不坏不阻断启动 |
| 默认零副作用：能 spawn 的参数必须显式给（`launch_tool.execute` / `get_tool_help.run` / `discover_tools.run_commands` / install 授权） | ✅ 同一条纪律的四个实例；返回体里的 `run` / `executed` 报的是**真的跑过没有**，不是调用方传了什么 |
| `store` 每次请求全量重读 JSON | ⚠️ 小规模可接受，无缓存层 |

---

## 8. 已同步项与遗留缺口

### 8.1 第一轮已修复 / 已同步 ✅（P0）

| 项 | 处理 |
|---|---|
| SKILL.md / README.md / TASKS.md / format.md 里的 launcher、adapters 描述 | ✅ 全部改为当前两层结构 |
| `list_launchers` / `get_launcher_info` 的虚假声明 | ✅ 从文档移除；未知工具改为返回协议层错误 |
| 参数名混乱（`mcp_tool_id` / `launcher_id`） | ✅ 统一为 `tool_id` |
| `requirements.txt` 无上界导致新环境装 2.x 后崩溃 | ✅ 锁定 `mcp>=1.29.0,<2.0.0`（D2 先锁 `<2.0.0`；A5 曾把下限提到 1.30，A9 实测 1.29 已具备全部所需能力后改回 1.29——生产环境 pyenv 3.12 现装 1.29.0） |
| 业务失败不被 MCP 客户端感知（`isError` 恒为 False） | ✅ 改为抛 `ToolError` |
| `read_resource` 返回 `TextContent`（缺 `.content` 属性必报错） | ✅ 改为 `ReadResourceContents` |
| Prompt 模板教 AI 用错误参数名 | ✅ 修正示例并补充错误类型说明 |

### 8.2 第二轮已修复 ✅（P1）

| 项 | 处理 |
|---|---|
| 同步失败时用空结果覆盖 backend-tools 缓存 | ✅ 空列表拒绝写盘，保留原缓存 |
| `_cleanup` 共用 try 导致子进程泄漏 | ✅ 两个 context 各自独立 try/finally |
| 清理可能并发（心跳失败 vs `stop`） | ✅ 引入 `_cleanup_lock`，清理串行且幂等 |
| 后端调用无任何超时保护 | ✅ `initialize`/`list_tools`/`call_tool`/`ping` 全部限时，超时标记连接不可用 |
| `stop()` 未加锁与 `start()` 竞态 | ✅ 共用 `_lock`；start 前先清残留 |
| 启动日志拼接假定 `args` 全为字符串 | ✅ 改为 `str(a)` |
| backend_client 零测试 | ✅ 新增 `tests/test_backend_client.py`（8 用例） |

### 8.3 第三轮已修复 ✅（P2）

| 项 | 处理 |
|---|---|
| JSON 读容错（D12） | ✅ `load_json` 捕获 `JSONDecodeError` / `OSError` + 校验顶层为 dict，告警后返回 `None`，坏文件不影响其他工具 |
| 原子写（D13） | ✅ 同目录 `mkstemp` → `fsync` → `os.replace`；写失败清理临时文件且保留原内容 |
| `get_backend_logs` 的 `limit=0` 边界（D9） | ✅ 显式判 `limit<=0 → []`（`-0 == 0` 陷阱） |
| 日志级别未真正过滤（D10） | ✅ `_log()` 按 `LOG_LEVEL_PRIORITY` 在入队前过滤 |
| 连接配置无法热更新（D14） | ✅ 空闲时按磁盘最新 `connection` 重建客户端并继承日志；运行中禁止热替换 |
| `lastSynced` 字面量 `"now"`（D15） | ✅ 写入 UTC `%Y-%m-%dT%H:%M:%SZ` |
| server 分发零测试（D19） | ✅ 新增 `tests/test_store.py`（8）+ `tests/test_server_dispatch.py`（14），合计 41 passed |

### 8.4 第四轮已修复 ✅（语义一致性与合规）

| 项 | 处理 |
|---|---|
| 搜索不含 `id`（D18） | ✅ `search_tools` 匹配范围加入 `id` |
| 文档与数据字段漂移（D17） | ✅ `format.md` 明确「两层数据 + 一层对外契约」，补齐 `install` / `actions` / `capabilities` |
| `launch_tool` 名不副实（D22） | ✅ 描述改为「只返回命令，不实际执行」并标 `readOnlyHint`（未改名，避免破坏已接入客户端）；2026-10-18 起补上显式 `execute=true` 执行路径，描述与 annotations 随之改为「默认只返回命令」+ `openWorldHint`（NE-6） |
| 无 schema 校验（A1） | ✅ 新增 `src/schema.py`（`jsonschema` 可选依赖），`registry` 跳过不合规条目并告警 |
| 安全元数据形同装饰（A2） | ✅ 只读工具下发 `readOnlyHint`；`call_backend_tool` 新增 destructive 门禁（`allowDestructive`） |
| 测试 | ✅ 新增 `test_registry_policy.py`（13 例），合计 54 passed |

### 8.5 第五轮已修复 ✅（协议层回环测试发现）

| 项 | 处理 |
|---|---|
| `read_resource` 用 `AnyUrl` 与字符串比较（D25） | ✅ 入口 `str(uri)` 归一化；此前三个 Resource **全部**读不出来 |
| `get_prompt` 返回类型不合规（D26） | ✅ 按版本返回 `GetPromptResult`（老 SDK 退回裸列表）；此前 prompts/get 全部失败 |
| 协议层契约无回归保护 | ✅ 新增 `tests/test_protocol_roundtrip.py`（7 例，真实 MCP 会话，不 mock SDK） |
| 其他必要测试 | ✅ BackendManager 错误分类 / `close_all` / 缓存 TTL、resources 与 prompts 定义、子工具查找，合计 **72 passed** |

### 8.6 第六轮已修复 ✅（A4：文档由 schema 反向生成）

| 项 | 处理 |
|---|---|
| 双真相源（A4） | ✅ `src/schema.py` 成为唯一真相源，`docs/format.md` 的两张字段表由 `scripts/generate_format_doc.py` 生成 |
| 文档手写漂移 | ✅ `tests/test_docs_sync.py` 四条防线：生成块逐字节一致 / 文档示例过校验 / `data/` 真实文件过校验且 `id` 与文件名一致 / schema 字段不得缺 `description` |
| `update` / `install` 子字段只写在文档里 | ✅ 已补进 schema 并纳入枚举校验（`update.method` 等） |
| 第二层数据无校验 | ✅ 新增 `BACKEND_CATALOG_SCHEMA` 与 `validate_backend_catalog()` |

**单向依赖方向**：`src/schema.py` →（生成）→ `docs/format.md` →（被测试校验）→
与 `data/*.json` 三方一致。改字段只改 schema，文档与测试会强制跟随。

### 8.7 后续项（部分已收口）

| 项 | 说明 |
|---|---|
| `structuredContent` | ✅ 已启用（mcp>=1.29，1.29 起 types.py 已定义、lowlevel server 已透传 `CallToolResult`）：`call_tool` 返回带 `structuredContent` 的 `CallToolResult`，`list_tools` 给每个工具挂统一 `outputSchema`（见 `SHARED_OUTPUT_SCHEMA`） |
| 依赖自装 + 启动预热（A6 ✅ 已收口；P0 后**默认不再执行安装**） | `install` 元数据接入执行：`ensure_installed()`（`_resolved_command` 剥壳解包 → `shutil.which` 检测 → 门禁 → 限时装一次 → 复检）+ `start()` 前置调用。启动预热在 P0 后**退化为纯检测**（只读 PATH、不执行外部命令），要恢复自动安装需部署者显式设 `AWAM_AUTO_INSTALL=1` 或条目 `install.auto=true` |
| 工具变更通知 | `notifications/tools/list_changed` 未发送（顶层工具集静态，影响小） |
| 标准 logging capability | 目前用自定义 Tool 查日志 |
| 本机工具发现（P1 ✅ 已落地） | `discover_tools` + `data/discovery.json`：声明式候选清单 + 四类只读探测（路径存在 / 目录条目数 / 注册表登记值 / PATH 查命令），结论 `installed` / `not_installed` / `unknown` 带 `confidence`，`status` / `kind` 与工具条目共用词表。**边界**：探测面是**声明出来的、不是穷举**——清单没写的安装位置发现不了，所以「未命中」只给中信心而不能当事实结论；版本号这一维要调用方显式 `run_commands=true` 才取 |
| 并发限流（A3 ✅ 已收口） | 单后端串行（`_call_lock`），并发调用报 `backend_busy`；要升级信号量 + 排队只需调 `MAX_CONCURRENT_CALLS` |
| 启动器（`kind=launcher`） | ✅ 已接入 Quicker 与 uTools：非 MCP，调用是一次性的（Quicker 走 `QuickerStarter.exe runaction:` 命令行回传；uTools 走 `utools://` 协议唤起，fire-and-forget 无回传）；动作清单走第二层数据（运行时目录 `launcher-actions/`），对外 `sync/list/run_launcher_action`。**边界**：Quicker 动作正文在加密 db，名称只能从 state 缓存尽力解析（可能 `source=partial`，只剩 ID）；uTools 清单从 `plugins/*.asar` 的 plugin.json 解析（声明完整，`source` 恒 `auto`），但协议调用无结果回传、payload 仅文本 |

> 完整清单见 [`code-review.md`](code-review.md) 与 [`mcp-conformance.md`](mcp-conformance.md)。

---

### 9.2 关键实现索引

| 位置 | 关键实现 |
|---|---|
| `server.py` 顶部 | mcp 依赖的兼容导入（`ReadResourceContents` 缺失时降级） |
| `errors.py` `ToolError` / `fail` / `ensure_success` | 业务错误 → MCP `isError` 的转换层（P1-04 从 server.py 搬入 errors.py，server 命名空间 re-export，`from server import ToolError` 仍是同一个类对象） |
| `tool_specs.py` `SERVER_TOOL_SPECS` / `build_mcp_tools()` | 22 个对外工具的声明表（SSOT）与 `Tool` 构造：annotations / outputSchema 在此统一挂载；`validate_specs()` 在 import 期自检 |
| `server.py` `call_tool` | `tools/call` 入口，含意外异常兜底 |
| `server.py` `dispatch_tool` | 22 个工具的查表分发（`policy.check_call` 策略前置 → `get_spec` → handler → 统一 `json.dumps`；未命中即 `unknown_tool`，策略拒绝即 `permission_denied`）。P3 起五个出口（成功 / `unknown_tool` / `permission_denied` / handler 业务错 / 意外异常）各调一次 `_record()` 记使用统计：业务错的 `errorType` 直接从异常正文取（与客户端看到的载荷同源），意外异常记 `internal_error`；埋点是旁路，返回值与异常语义一字未改（`tests/test_usage.py` 的 dispatch 层用例锁定） |
| `launcher_client.py` `LauncherClient` | 启动器（非 MCP）适配器：`_scan_quicker` 解析动作清单、**先 json.loads 再取字符串**才能拿到动作名；`run_action` 走一次性外部命令（`-c<秒> "runaction:<ID>?参数"`），输出按 GBK 解码、失败按文本判定（退出码恒为 0）；`sync_actions` 按 `launcher.type` 三分支（steam 先判，`dataDir` 可空、回退注册表），`_scan_steam` 把 acf 行折成 `app:<appid>` 动作（描述带库名与 MB，`source` 随失踪库降为 `partial`），`_run_steam` 在 spawn 前校验 ID 并委托 `launch_app` |
| `steam_library.py` `parse_vdf` / `resolve_root` / `installed_games` | P0 Steam 只读解析的三段：`parse_vdf()` 是 VDF tokenizer（`"key" "value"` / `"key" { … }`、成对 `\\` 转义、字符串化的数字保持原样），**只对真的坏掉的文件抛**——块缺右括号、键后没有值都判 `SteamError`，而显式写全的 `"key" {}` 是合法的空对象（截断和作者本意得区分开，半份清单比没有清单更坏）；`resolve_root()` 按 条目 `dataDir` → 注册表 `SteamPath` → 穷举兜底，兜底命中会在来源里写明「未经登记值确认」，且把注册表里的正斜杠归一化；`installed_games()` 以 acf 为唯一「装着」的证据，vdf 声明的 appid 只汇成 `declaredAppTotal`，盘上失踪的库进 `missingLibraries` 并附 note（本机实测：声明 3 库 / 27 appid，盘上 16 份 acf） |
| `app_launcher.py` `launch_app` | `kind=app` 的启动执行：`launch_tool` 传 `execute=true` 才走到这里；只支持 `launch.type=executable`，失败一律走返回载荷（`config_error` 预检不通过 / `runtime_error` 退出码非 0），不抛异常 |
| `tool_help.py` `resolve_help_command` / `run_help_command` | `get_tool_help` 的两段：前者把条目折成一条帮助命令（`help.command` > `launch.command`、`help.args` > `--help`，**绝不复用 `launch.args`**，timeout 归一化封顶 30s），后者只在显式 `run=true` 时被调用——`shutil.which` 预检 → `create_subprocess_exec` 捕获 stdout/stderr（UTF-8→GBK→replace 三级解码）→ 超时 kill 并报 `runtime_error`；`executed` 字段如实反映「是否真的 spawn 过」 |
| `discovery.py` `expand_path` / `resolve_executable` / `fold_verdicts` / `probe_command` / `_probe_registry_value` | P1 工具发现的五段：`expand_path()` 把 `{ENV}` 模板展开成绝对路径，进程环境与注册表回退两处都取不到才返回「没检查成」而不是相对路径；`resolve_executable()` 在 Windows 上把 `shutil.which` 的无扩展名垫片结果换成真目标（裸名优先是 `WinError 193` 的根因）；`fold_verdicts()` 把逐条证据折成 `hit` / `miss` / `inconclusive`（miss 要求**所有**能下结论的维度都说没有，且没有维度没检查成）；`probe_command()` 默认只解析 PATH 判可用性（不 spawn），`run=True` 才用绝对路径无 shell 执行、`stdin=DEVNULL`（子进程继承 server 的 stdio 管道会偷走 inbound JSON-RPC，真机上打死过整个网关）、按 `version_pattern` 取版本号，超时与 OSError 都折成结论返回而不抛；`_probe_registry_value()` 读安装器自己写的登记值，读数四档里只有「不存在 / 空 / 指向的路径已失效」算 miss，权限与展开失败算 inconclusive（Steam 装 `S:` 盘那次假阴性的修法） |
| `policy.py` `decide_destructive` / `check_call` | 统一破坏性门禁与部署策略（ARC-P1-05 Phase 1+2，取代 `tool_specs.py` 的 `_needs_destructive_confirm`）：mcp 子工具精确匹配（`destructiveTools`）/ launcher 动作级 glob（`destructiveActions`，大小写不敏感，匹配动作 id/name/type）/ app 条目级（`launch_tool execute=true` 与 `get_tool_help run=true` 两个执行入口共用同一判定，审计 `action` 跟随实际入口）/ 缺省回退条目级；`data/policy.json` 参与判决（deny 名单、白名单、`destructive.mode`：deny 时策略赢过客户端 `allowDestructive` → `permission_denied`）；未配置新字段/无策略文件时与旧行为逐字等价（`tests/test_policy_gate.py` 冻结旧逻辑对照 + `tests/test_policy_config.py` 默认策略行为锁定） / package 操作级（**没有条目可标**，方向是 fail-closed：`PACKAGE_CONFIRMED_OPS` 是「不需要授权」白名单而不是黑名单，`launch_package` 在名单内、`install_package` 与一切未登记操作都要 `allowDestructive`）；输出 `Decision` + 结构化审计行（stderr + `<runtime>/audit.log`，含 decision / rule_hit / client_declared / policy_mode） |
| `server.py` `read_resource` | 返回 `Iterable[ReadResourceContents]` |
| `server.py` `main` | stdio 事件循环 + 启动后台预热任务 `_prewarm_backends()`，`finally` 取消预热并关闭全部后端 |
| `store.py` `load_json` / `save_json` | JSON 读写：读有容错（坏文件跳过），写走 `mkstemp` + `os.replace` 原子替换 |
| `registry.py` `search_tools` | 关键词匹配 id / name / displayName / description / tags（含 `id`，避免「搜得到工具却搜不到它的 ID」） |
| `registry.py` `_apply_filters` | `kind` 之外的筛选维度：`status` 精确（缺省口径与投影一致，按 `installed` 计）、`publisher` 子串大小写不敏感；两者**先归一化再判空**，纯空白等于不设筛选。`list_all_tools` / `search_tools` 共用，搜索是「先关键词命中再收窄」 |
| `usage.py` `record_call` / `read_rows` / `aggregate` | P3 使用统计的三段：`record_call()` 只写 `ts`/`tool`/`target`/`ok`/`ms`（失败才带 `errorType`），**不写参数**，`OSError` 一律吞掉转 stderr 告警；`read_rows()` 把「文件不存在」当空、坏行跳过并汇总告警一次，`days` 走日历日集合（含今天）而不是 timedelta 减法；`aggregate()` 是不做 IO 的纯函数（单测直接喂行），`targets` 只收带 `tool_id` 的调用——「本机常用工具榜」看的是它而不是 `byName` |
| `backend_client.py` `BackendClient.start` | 子进程启动、连接、预热工具列表、起心跳；spawn 前先 `ensure_installed()`，被门禁拦下时返回 `install_required` / `install_not_allowed` 且不 spawn |
| `pkgman.py` `installed_one` / `launch_target` / `search_packages` / `run_argv` | P2-07 包管理面的四段：`installed_one()` 读一个 app 目录，`current` 断链时回落最新的版本目录并标 `current_broken`（本机 14 个包有 6 个是这种），bucket 从同版本目录的 `install.json` 取；`launch_target()` 只从行字段取候选（快捷方式 → bin → shim），每项带 `exists` / `runnable` 两个标记，挑不出时返回 `code`（`NO_SUCH_ENTRY` / `MISSING_FILE` / `NOT_SPAWNABLE` / `NO_LIVE_ENTRY` / `NO_ENTRIES`）与全部候选——让调用方看「差在哪」而不是猜一个跑起来，`launchable` 与它共用同一批候选，一行数据不许有两种答案；`search_packages()` 按 bucket manifest 的**文件名**筛、命中才读 JSON（4287 份清单全量读是几百毫秒级浪费）；`run_argv()` 是唯一真的起进程的地方：`stdin=DEVNULL` + `anyio.fail_after`（不用 `asyncio.wait_for` 做主等待，也不用 `subprocess.run`——装包是分钟级动作，同步等待会冻住事件循环），stdout/stderr 各留尾部 4000 字符 |
| `backend_client.py` `BackendClient.ensure_installed` | 依赖安装：剥壳解包启动命令 → `shutil.which` 检测 → **P0 门禁（授权 + 命令白名单）** → 限时装一次（`INSTALL_TIMEOUT`）→ 复检；幂等，装不坏不阻断 |
| `backend_client.py` `BackendManager.ensure_all_installed` | 启动预热：遍历所有 MCP 工具**只检测**（P0 后不执行安装），单个失败不影响整体；汇总 `blocked` 计数供启动日志报告 |
| `backend_client.py` `install_auto_authorized_by_env` / `_install_authorized` / `_install_command_allowed` | P0 门禁三件套：环境变量授权、条目 opt-in、命令白名单与 shell 元字符检查 |
| backend_client.py 新增的 `_cleanup_lock` 保证清理串行且幂等（第一轮修复前两个 context 共用 try） |
| `backend_client.py` `BackendClient.call_tool` | 转发调用，`wait_for` 限时，超时标记连接不可用 |
| `backend_client.py` `BackendManager.get_client` | 连接池；以磁盘配置为真相源，空闲时按新 `connection` / `install` 重建 |
