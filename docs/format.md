# awam-tool-manager 格式规范

参考 MCP Tool Schema、WinGet Manifest 设计。

> 历史变更：早期版本曾有「启动器配置层」（`data/launchers/`）作为第 1 层，
> 现已废弃移除。MCP 不再是独立层级，而是工具的 `kind` 取值之一。

---

## 数据层 1：工具条目（Tool Entry）

> **只有这两个"层"是数据层**：`data/tools/`（人工维护，在仓库内）与后端子工具缓存
> `backend-tools/`（自动生成，在运行时目录 `%LOCALAPPDATA%\.awam-tool-manager/`，A14 起迁出仓库）。
> 下面第 3 节列出的「对外 MCP 工具」是**对外契约**，不是存储在磁盘上的数据层，
> 早期文档把它算作第 3 层容易造成"三层架构"的误解。

**一个工具 = 一个 JSON 文件**，存放在 `data/tools/` 目录，文件名即 `tool_id`。
条目在加载时会按本节规范校验（`src/schema.py`），不合规的条目会被跳过并向 stderr 告警。

### 字段说明

> **本表由 `src/schema.py` 生成，请勿手改。**
> 改字段请改 `src/schema.py` 的 `TOOL_ENTRY_SCHEMA`，然后运行
> `python scripts/generate_format_doc.py`（`--check` 可只做漂移检查）。
> 测试 `tests/test_docs_sync.py` 会在文档与 schema 不一致时直接失败。

<!-- BEGIN:generated:tool-entry -->

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | `string` | ✅ | 工具唯一 ID，**必须与文件名一致**（须匹配 `^[A-Za-z0-9._-]+$`） |
| `name` | `string` | ✅ | 工具名称（英文/技术名） |
| `displayName` | `string` | - | 显示名称（中文/用户友好），缺省回落 `name` |
| `kind` | `string` | ✅ | 工具类型：`app` 普通应用（`launch_tool` 返回命令，显式 `execute=true` 时启动）、`mcp` 可建后端连接、`launcher` 启动器（非 MCP 协议，但有可列/可搜/可调用的动作清单，见 `launcher`）（取值：`app` / `mcp` / `launcher`） |
| `description` | `string` | - | 工具功能描述（参与搜索匹配） |
| `status` | `string` | - | 安装状态（取值：`installed` / `not_installed` / `unknown`） |
| `version` | `string` | - | 版本号 |
| `publisher` | `string` | - | 发布者 |
| `website` | `string` | - | 官方网站 |
| `docUrl` | `string` | - | 文档地址 |
| `icon` | `string` | - | 图标路径或 URL |
| `launch` | `object` | - | **仅 `kind=app`**：启动方式，`launch_tool` 据此返回命令；`execute=true` 时按此 spawn 进程 |
| `launch.type` | `string` | - | 启动方式类型（取值：`executable` / `protocol` / `command`） |
| `launch.command` | `string` | - | 启动命令或可执行文件路径 |
| `launch.args` | `array` | - | 启动参数（元素建议统一写字符串） |
| `help` | `object` | - | 帮助信息获取方式，`get_tool_help` 据此取参数；缺省用 `launch.command` + `--help` |
| `help.command` | `string` | - | 专用帮助命令（缺省回落 `launch.command`） |
| `help.args` | `array` | - | 帮助参数（缺省 `--help`；Windows 图形程序常为 `/?`） |
| `help.timeout` | `number` | - | 执行帮助命令的超时秒数（封顶 30）（缺省 `5`；必须 > 0） |
| `connection` | `object` | - | **仅 `kind=mcp`**：后端 MCP 连接方式，`BackendClient` 据此拉起子进程 |
| `connection.type` | `string` | ✅ | 连接类型，目前仅支持 stdio（取值：`stdio`） |
| `connection.command` | `string` | ✅ | 拉起子进程的命令 |
| `connection.args` | `array` | - | 子进程参数 |
| `connection.env` | `object` | - | 子进程环境变量 |
| `connection.timeout` | `number` | - | 后端操作超时（秒），覆盖 initialize / list_tools / call_tool / ping（缺省 `30`；必须 > 0） |
| `update` | `object` | - | 更新方式 |
| `update.method` | `string` | - | 更新渠道（取值：`winget` / `scoop` / `npm` / `manual`） |
| `update.packageId` | `string` | - | 包管理器中的包 ID |
| `install` | `object` | - | 安装方式；`start_backend` 检测到启动命令缺失时，仅在**显式授权**后才会执行一次 `command`（默认不执行，见安全约定），与 `update` 并存 |
| `install.method` | `string` | - | 安装渠道（取值：`npm` / `winget` / `scoop` / `manual`） |
| `install.command` | `string` | - | 安装命令 |
| `install.auto` | `boolean` | - | 是否允许自动执行 `command`；默认 false。需显式设为 true，或设环境变量 AWAM_AUTO_INSTALL=1，否则 install.command 永不被自动执行 |
| `launcher` | `object` | - | 启动器适配配置；仅 `kind=launcher` 有意义，决定如何列出与调用它的动作 |
| `launcher.type` | `string` | - | 适配器类型：`quicker` 走 QuickerStarter 命令行（有 stdout 回传）；`utools` 走 utools:// 协议唤起（fire-and-forget，无回传，结果只在 uTools UI）；`steam` 读安装器自己写的库清单（`libraryfolders.vdf` + `appmanifest_*.acf`）出游戏列表，调用是 `steam.exe -applaunch <appid>`（退出码只代表指令已投递，游戏起没起无从回传）（取值：`quicker` / `utools` / `steam`） |
| `launcher.command` | `string` | - | 启动器可执行文件路径（如 QuickerStarter.exe；utools 协议调用不需要；steam 省略时按注册表 SteamPath 推 steam.exe） |
| `launcher.dataDir` | `string` | - | 启动器数据目录（Quicker 的 states 所在目录；uTools 的插件 asar 所在目录，utools 适配器会展开 %APPDATA% 等环境变量；steam 是客户端安装根，省略则回退 `HKCU\Software\Valve\Steam` 的 SteamPath） |
| `launcher.timeout` | `number` | - | 单次动作调用的最长等待（秒）（缺省 `20`；必须 > 0） |
| `tags` | `array<string>` | - | 标签分类（参与搜索匹配） |
| `actions` | `array<string>` | - | 支持的动作（如 `launch` / `search`） |
| `capabilities` | `array<string>` | - | 能力标签（如 `search` / `index`） |
| `readOnly` | `boolean` | - | 是否只读（对应 MCP 的 `readOnlyHint`） |
| `destructive` | `boolean` | - | 是否有破坏性（对应 `destructiveHint`，`call_backend_tool` 会据此拦截） |
| `destructiveTools` | `array<string>` | - | 子工具级破坏性清单：只有列在这里的后端子工具需要 `allowDestructive` 确认；未配置时回退到条目级 `destructive`（即整个后端的所有调用都要确认） |
| `destructiveActions` | `array<string>` | - | 启动器动作级破坏性清单（glob，大小写不敏感）：按动作的 id/name/type 匹配，命中才需要 `allowDestructive` 确认；未配置时回退到条目级 `destructive`（即该启动器的所有动作都要确认） |
| `metadata` | `object` | - | 扩展元数据，键值对自由 |
| `createdAt` | `string` | - | 创建时间，ISO 8601 |
| `updatedAt` | `string` | - | 更新时间，ISO 8601 |

<!-- END:generated:tool-entry -->

### 示例：`kind=app`

```json
{
  "id": "everything",
  "name": "Everything",
  "displayName": "Everything 文件搜索",
  "kind": "app",
  "status": "installed",
  "description": "Windows 极速文件搜索工具，毫秒级搜索百万文件",
  "version": "1.4.1",
  "publisher": "voidtools",
  "website": "https://www.voidtools.com/",
  "docUrl": "https://www.voidtools.com/support/everything/",
  "launch": {
    "type": "executable",
    "command": "C:\\Program Files\\Everything\\Everything.exe",
    "args": []
  },
  "update": {
    "method": "winget",
    "packageId": "voidtools.Everything"
  },
  "tags": ["文件搜索", "系统工具", "效率"],
  "readOnly": false,
  "destructive": false,
  "createdAt": "2026-10-15T16:50:00Z",
  "updatedAt": "2026-10-15T16:50:00Z"
}
```

### 示例：`kind=mcp`

```json
{
  "id": "everything-search-mcp",
  "name": "Everything Search MCP",
  "displayName": "Everything 文件搜索 MCP",
  "kind": "mcp",
  "status": "installed",
  "description": "Everything 文件搜索的 MCP 接口，提供文件搜索、文件信息查询能力",
  "version": "latest",
  "publisher": "community",
  "website": "https://www.npmjs.com/package/everything-search-mcp",
  "tags": ["文件搜索", "MCP", "系统工具"],
  "connection": {
    "type": "stdio",
    "command": "cmd.exe",
    "args": ["/c", "npx", "-y", "everything-search-mcp"]
  },
  "update": {
    "method": "npm",
    "packageId": "everything-search-mcp"
  },
  "readOnly": true,
  "destructive": false
}
```

---

## 数据层 2：后端子工具缓存（Backend Tool Catalog）

由 `sync_backend_tools` 自动生成并写入**运行时目录**的 `backend-tools/{tool_id}.json`
（`%LOCALAPPDATA%\.awam-tool-manager/`，A14 前在 `data/backend-tools/`），**不建议手工编辑**。
结构同样由 `src/schema.py` 的 `BACKEND_CATALOG_SCHEMA` 定义并生成本表。

<!-- BEGIN:generated:backend-catalog -->

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `parent` | `string` | ✅ | 父 MCP 工具的 ID |
| `lastSynced` | `string` | - | 上次同步时间，ISO 8601；用于判断缓存有多旧 |
| `source` | `string` | - | 来源，表示由后端自动拉取而非人工维护（取值：`auto`） |
| `tools` | `array<object>` | ✅ | 后端 MCP 提供的子工具列表 |
| `tools[].name` | `string` | ✅ | 子工具名，即 `call_backend_tool` 的 `tool_name` 参数 |
| `tools[].description` | `string` | - | 子工具描述 |
| `tools[].inputSchema` | `object` | - | 透传自后端 MCP 的 JSON Schema |

<!-- END:generated:backend-catalog -->

```json
{
  "parent": "everything-search-mcp",
  "lastSynced": "2026-10-15T18:00:00Z",
  "source": "auto",
  "tools": [
    {
      "name": "search_files",
      "description": "Search for files and folders using Everything.",
      "inputSchema": {
        "type": "object",
        "properties": { "query": { "type": "string" } },
        "required": ["query"]
      }
    }
  ]
}
```

---

## 数据层 2.5：启动器动作清单（Launcher Action Catalog）

由 `sync_launcher_actions` 自动生成并写入**运行时目录**的
`launcher-actions/{tool_id}.json`（与数据层 2 同构），**不建议手工编辑**。
结构由 `src/schema.py` 的 `LAUNCHER_ACTIONS_SCHEMA` 定义（本表为手写，
未纳入生成器，改结构时请同步更新此处与 schema）。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `parent` | `string` | ✅ | 父启动器工具的 ID（`kind=launcher`） |
| `lastSynced` | `string` | - | 上次同步时间，ISO 8601；缓存超过 `ACTIONS_CACHE_TTL`（300s）后 list/run 会自动重扫 |
| `source` | `string` | - | 来源（取值：`auto` / `partial`，`partial` 表示部分动作只解析出 ID） |
| `actions` | `array<object>` | ✅ | 启动器动作列表 |
| `actions[].id` | `string` | ✅ | 动作 ID（Quicker 的 UUID；uTools 为 `<插件名>/<关键词>`），`run_launcher_action` 的 `action_id` 参数 |
| `actions[].name` | `string` | - | 动作名称（尽力而为：来自 state 缓存与运行日志，可能缺失） |
| `actions[].type` | `string` | - | 动作类型（来自 state 缓存） |
| `actions[].app` | `string` | - | 动作面向的应用（来自 state 缓存） |
| `actions[].description` | `string` | - | 动作描述（来自 state 缓存） |

---

## 发现候选清单（`data/discovery.json`）

> **它不是数据层**：`data/tools/` 描述「已经登记了什么」，这份清单描述「该去找什么」。
> 因此它不进 `src/schema.py`（不生成字段表），校验源是 `src/discovery.py` 的
> `validate_manifest`；路径可用 `AWAM_DISCOVERY_FILE` 覆盖。
> 失败时机也与策略文件刻意不同：坏清单只让 `discover_tools` 这一次调用返回
> `config_error`，不会把整个服务拖到起不来（发现不是安全面）。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `version` | `int` | - | 清单格式版本，当前为 1，加载侧不参与判定 |
| `candidates` | `array` | 是 | 候选列表；空数组等于「没声明要找什么」，不是「本机没有工具」 |
| `candidates[].tool_id` | `string` | 是 | 与工具条目同一套 id；非空且不重复，用于对照 `registered` |
| `candidates[].name` | `string` | 是 | 人读名称，非空 |
| `candidates[].kind` | `string` | - | 取值与工具条目共用词表（`app` / `mcp` / `launcher`），缺省按 `app` |
| `candidates[].purpose` | `string` | - | 一句话用途，缺省空串 |
| `candidates[].probes` | `array` | - | 只读探测面，至少声明一条 |
| `candidates[].probes[].probe` | `string` | 是 | `path_exists` / `registry_value`（都能下结论）/ `dir_count`（只补充证据） |
| `candidates[].probes[].path` / `paths` | `string` / `array` | 是* | `{ENV}` 模板路径，多个位置逐个试，第一个存在即命中（仅 `path_exists` / `dir_count` 用） |
| `candidates[].probes[].hive` | `string` | - | 仅 `registry_value`：`HKCU`（默认）/ `HKLM`，写别的判清单非法 |
| `candidates[].probes[].key` | `string` | 是* | 仅 `registry_value`：根键下的键路径，如 `Software\\Valve\\Steam` |
| `candidates[].probes[].value` | `string` | 是* | 仅 `registry_value`：值名，如 `SteamExe` |
| `candidates[].probes[].require_exists` | `bool` | - | 仅 `registry_value`：默认 `true`，还要验证值指向的路径真的存在；`false` 时只问「登记在不在」（版本号这类非路径值） |
| `candidates[].probes[].label` | `string` | - | 证据行显示名，缺省取第一条路径（`registry_value` 取 `hive\\key\\value`） |
| `candidates[].probes[].limit` | `int` | - | 仅 `dir_count`：最多列几条（默认 60，封顶 200，`<=0` 返回空） |
| `candidates[].command` | `object` | - | 命令面；`argv` 非法（空 / 非字符串数组）会被校验拒绝 |
| `candidates[].command.argv` | `array` | 是 | 无 shell 的参数数组，`argv[0]` 用 PATH 查找判定可用性（Windows 上把无扩展名垫片换成带可执行后缀的真目标，否则 `CreateProcess` 报 `WinError 193`；真执行时 `stdin=DEVNULL`，不让子进程继承服务的 stdio 管道） |
| `candidates[].command.preview` | `string` | - | 人读预览，缺省由 `argv` 拼接 |
| `candidates[].command.version_pattern` | `string` | - | 取版本号的正则（须有捕获组），加载时就编译校验 |

> 标 `是*` 的字段按 `probe` 类型二选一必填：`path_exists` / `dir_count` 要
> `path` 或 `paths`，`registry_value` 要 `key` 和 `value`。校验按类型分支检查，
> 缺了直接判清单非法（`config_error`），不静默降级。

**登记值探针补的是声明式路径的结构盲区**：`path_exists` 只能验「清单写过的位置」，
装在非常规盘就全落空——本机 Steam 装在 `S:\Program Files (x86)\Steam`，清单原先只声明
`C:\Program Files` 两处，于是明明装着却判成 `not_installed`（中信心）。`registry_value`
读安装器自己维护的登记值（Steam 在 `HKCU\Software\Valve\Steam\SteamExe`），不依赖穷举
盘符。**清单里每个候选都该有一条不依赖盘符的证据**。读数分四档，档位决定结论：
键/值不存在、值不是非空字符串 → `miss`（这是事实信号：登记记录没有）；权限不足、
非 Windows、`REG_EXPAND_SZ` 展开后仍残留 `%…%` → `inconclusive`（只是没检查成，
不能伪装成没装）；登记值存在但指向的路径已不存在 → `miss`（僵尸登记）。

**路径模板**：`{ProgramFiles}`、`{APPDATA}`、`{LOCALAPPDATA}`、`{USERPROFILE}`，
Windows 变量名里的括号也支持（`{ProgramFiles(x86)}`）。token 取值有两个来源：
先读进程环境，取不到再回退查 Windows 注册表（用户 `HKCU\Environment` → 机器
`Session Manager\Environment`，`ProgramFiles` 系列另查 `ProgramFilesDir`）。
回退不是可选加固——MCP 客户端按白名单裁剪 server 的环境变量，`ProgramFiles` /
`SCOOP` 这类常常压根不在进程环境里。两处都取不到 → **该位置判「没检查成」**
（`inconclusive`），绝不回退成相对路径去试存在性——那会把「没设环境变量」
伪装成「这个路径不存在」。

**结论三态**（`status` 与工具条目共用词表，可直接回填 `list_tools` 的筛选）：

| 结论 | `status` | `confidence` | 成立条件 |
|---|---|---|---|
| 命中 | `installed` | `high` | 任一路径存在、登记值指向的路径存在，或命令在 PATH 上 |
| 未命中 | `not_installed` | `medium` | 所有能下结论的维度都说没有，且没有维度没检查成 |
| 没检查成 | `unknown` | `none` | 至少一个声明位置因环境变量未设置、注册表读不到或探针类型未实现而没探测成 |

`dir_count` 永远不参与结论（目录空不等于没装）。命令可用性只查 PATH（`shutil.which`），
**不 spawn**；执行版本命令取版本号要显式传 `run_commands=true`，与 `launch_tool` 的
`execute` / `get_tool_help` 的 `run` 是同一条门禁纪律。

### 示例

```json
{
  "version": 1,
  "candidates": [
    {
      "tool_id": "scoop",
      "name": "Scoop",
      "kind": "app",
      "purpose": "Windows 命令行软件包管理器",
      "probes": [
        { "probe": "path_exists", "paths": ["{USERPROFILE}\\scoop\\shims\\scoop.exe", "{USERPROFILE}\\scoop\\shims\\scoop.cmd"], "label": "默认安装根目录" },
        { "probe": "dir_count", "path": "{USERPROFILE}\\scoop\\apps", "limit": 40, "label": "已安装 formula" }
      ],
      "command": { "argv": ["scoop", "--version"], "version_pattern": "CURRENT:\\s+([0-9.]+)" }
    },
    {
      "tool_id": "steam",
      "name": "Steam",
      "kind": "app",
      "purpose": "Steam 客户端",
      "probes": [
        { "probe": "registry_value", "hive": "HKCU", "key": "Software\\Valve\\Steam", "value": "SteamExe", "label": "Steam 自己登记的 steam.exe 位置" },
        { "probe": "path_exists", "paths": ["{ProgramFiles(X86)}\\Steam\\steam.exe"], "label": "默认安装位置" }
      ]
    }
  ]
}
```

---

## 对外契约：MCP 工具

本技能作为 MCP Server 暴露给 AI 客户端的工具列表（**不是磁盘上的数据层**）。
后端相关工具统一使用 **`tool_id`** 指定目标工具。
可静态判定的工具都带 annotations（`readOnlyHint` / `idempotentHint` / `openWorldHint`）。

| 工具名 | 参数 | 说明 |
|---|---|---|
| `search_tools` | `keyword`(必填)、`kind`、`status`、`publisher` | 按关键词搜索工具（匹配 `id` / 名称 / 描述 / 标签），三个筛选维度取交集 |
| `list_tools` | `kind`、`status`、`publisher` | 列出所有工具（`status` 精确匹配、条目未写时按 `installed` 计；`publisher` 子串、大小写不敏感；空值/纯空白等于不设筛选） |
| `get_tool_info` | `tool_id`(必填) | 查看工具详情 |
| `launch_tool` | `tool_id`(必填)、`execute`、`wait`、`allowDestructive` | 返回启动命令；**默认只返回、不执行**，显式 `execute=true` 才启动进程（条目 `destructive` 时需 `allowDestructive=true`；`wait` 是执行后的早退观察窗口，默认 5s、封顶 30s） |
| `call_backend_tool` | `tool_id`、`tool_name`(必填)、`arguments`、`allowDestructive` | 路由调用后端子工具（目标标记 `destructive` 时需 `allowDestructive=true`） |
| `list_backend_tools` | `tool_id`(必填)、`force_refresh` | 列出后端子工具 |
| `start_backend` | `tool_id`(必填) | 启动后端并同步目录 |
| `stop_backend` | `tool_id`(必填) | 停止后端 |
| `get_backend_status` | `tool_id` | 查看后端状态 |
| `get_backend_logs` | `tool_id`(必填)、`level`、`limit` | 查看后端日志 |
| `set_backend_log_level` | `tool_id`、`level`(必填) | 设置日志级别 |
| `sync_backend_tools` | `tool_id`(必填) | 同步子工具目录 |
| `sync_launcher_actions` | `tool_id`(必填) | 手动同步启动器动作清单 |
| `list_launcher_actions` | `tool_id`(必填)、`keyword`、`limit`、`refresh` | 列出/搜索启动器动作（缓存超过 5 分钟自动重扫） |
| `run_launcher_action` | `tool_id`(必填)、`action_id`/`action_name`(至少一个)、`param`、`wait`、`allowDestructive` | 执行启动器动作（条目或目标标记 `destructive` 时需 `allowDestructive=true`；配 `destructiveActions` 时按动作 id/name/type 的 glob 判定，仅命中的动作需确认） |
| `get_tool_help` | `tool_id`(必填)、`run`、`timeout`、`allowDestructive` | 取工具帮助信息：文档链接 + 能力清单（`kind=mcp` 子工具 / `kind=launcher` 动作）+ 帮助命令预览。**默认只读数据、不 spawn**；`kind=app` 显式 `run=true` 才执行 `help.command`/`launch.command` + `help.args`（缺省 `--help`）并捕获输出（条目 `destructive` 时需 `allowDestructive=true`；`timeout` 默认 5s、封顶 30s） |
| `get_usage_stats` | `days`、`tool_id` | 查询调用使用统计（只读 `<runtime>/usage.log`）：按天 / 按对外工具 / 按目标 `tool_id` 聚合调用量、成败与耗时。`days` 按日历日含今天、缺省 7、越界收敛到 1~365；`tool_id` 与记录里的 `target` 精确匹配 |
| `discover_tools` | `run_commands`、`status` | 按 `data/discovery.json` 发现本机工具：`path_exists` / `registry_value` / `dir_count` / 命令是否在 PATH 四类**只读**探测，输出 `status`（installed / not_installed / unknown）+ `confidence` + `registered` + 逐条证据。**默认不 spawn**，`run_commands=true` 才执行清单里的版本命令取版本号；`status` 带 `enum`，只裁剪返回的候选，`summary` 仍是整份清单的计数 |

> **统计的覆盖范围是「经本 server 分发的调用」**：`dispatch_tool` 的每个出口
> （成功、`unknown_tool`、`permission_denied`、handler 业务错、意外异常）各记一行
> `ts` / `tool` / `target` / `ok` / `ms`，失败行才附带同载荷里的错误类型键。
> 用户直接启动工具、其他进程或其他日期的历史调用都不在计数内，因此 `total=0`
> 时返回体会附 `note` 说明口径。刻意**不记调用参数**：参数里可能带本机路径、
> 关键词、动作正文。
> 聚合视图嵌在 `stats` 下而非平铺——`aggregate` 自身有个 `success`（成功次数，int），
> 平铺会把载荷信封的 `success: true`（bool，`SHARED_OUTPUT_SCHEMA` 唯一必填）覆盖掉。

> **`status` 带 `enum`，拼错的值在校验层就是 `isError`**（`Input validation error: ... is not one of [...]`），
> 不会静默返回空列表——筛选类接口最坏的一种失败是让调用方把「参数打错」读成「本机没装」。
> `kind` 目前没有 `enum`（历史契约，`NE-5` 只修过它的文案），传错值仍是空列表；
> 给 `kind` 补 `enum` 会让下发 schema 变化并要求外部客户端改正，属独立改动，本期未做。
>
> **能筛的维度都能看见**：`list_tools` / `search_tools` 的每条结果都投影
> `id` / `name` / `kind` / `description` / `status` / `publisher`（条目没写的投影成
> 缺省值 `installed` / 空串，不是缺键），调用方可以直接把看到的值回填给筛选参数。
> `discover_tools` 走的是同一套 `status` / `kind` 词表（反向也成立：发现结果的
> `status` 能直接接住筛选参数），但**它的探测面不是穷举**——清单没声明的安装位置
> 发现不了，所以「未命中」只是中信心结论，返回体固定附 `note` 自证这一条。这条
> 不是理论风险：Steam 装在 `S:\Program Files (x86)` 时，只声明 `C:\Program Files`
> 的清单把它判成了 `not_installed`，因此加了 `registry_value` 探针并要求
> **每个候选都有一条不依赖盘符的证据**。

### Resources

| URI | 内容 | mimeType |
|---|---|---|
| `awam://tools` | 所有已注册工具 | application/json |
| `awam://mcp-servers` | 所有 `kind=mcp` 的工具 | application/json |
| `awam://backends/status` | 所有后端运行状态 | application/json |

### 错误返回约定

失败时返回 MCP 协议层错误（`isError=true`），正文为 JSON：

```json
{
  "success": false,
  "errorType": "connection_error",
  "error": "无法连接后端 MCP: 后端未运行，当前状态: stopped"
}
```

`errorType`：`not_found` / `unknown_tool` / `unknown_resource` / `unknown_prompt`
/ `invalid_params` / `connection_error` / `runtime_error` / `config_error`
/ `destructive_blocked` / `permission_denied` / `backend_busy` / `backend_installing`
/ `internal_error` / `launcher_error` / `install_required` / `install_not_allowed`
/ `unknown_error`

---

## 设计原则

- 符合 MCP 官方 Tool Schema 规范，`inputSchema` 必须是合法 JSON Schema
- 元工具模式：只暴露少量高层工具，内部路由到具体实现
- `readOnly` / `destructive` 与 MCP Tool annotations 语义对齐：
  只读工具已下发 `readOnlyHint`，`destructive` 已在 `call_backend_tool` 转发前设门禁
- **单一真相源**：`src/schema.py` 既是运行期校验依据，也是上面两张字段表的生成源。
  改字段只改 schema，再跑 `python scripts/generate_format_doc.py`；
  本文件其余部分（示例、对外契约、错误约定）仍手写，
  但示例会被 `tests/test_docs_sync.py` 拿去跑校验，不会悄悄腐烂
