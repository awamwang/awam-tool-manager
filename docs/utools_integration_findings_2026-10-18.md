# uTools 接入勘察报告（2026-10-18）

> 任务：uTools 已装于本机（`C:\Users\Administrator\AppData\Local\Programs\utools\uTools.exe`，v7.8.0），
> 目标「找到它所有的工具、获取参数并调用」，并评估走 Quicker 式的命令行调用路径。
> 本报告为**勘察结论 + 适配方案设计**，不含实现代码。配套数据：`utools_plugins_catalog_2026-10-18.json`。

## TL;DR

- uTools **内置 MCP server 已启用**（`http://127.0.0.1:3501/mcp`，StreamableHTTP + `x-mcp-key` 头鉴权），
  当前暴露 **25 个工具**（来自 6 个声明了 MCP 的插件），schema 完整、实测可调——但这确实只是冰山一角。
- 全量能力目录：**44 个插件、163 个功能特性**（`plugin.json` 的 `features[].cmds`），
  用纯 Python 解析 asar 即可提取（asar = Chromium Pickle 封装，已实测可解析）。
- 命令行路径存在且可用：**`utools://<插件名>/<功能关键词>?<URL编码的文本>`** 协议唤起
  （已实测：调用后插件 renderer 进程被拉起）。等价于 Quicker 的 `runaction:名称?参数`。
- 结论：走 awam 既有的 `kind=launcher` 适配器模式接入 uTools **完全可行**，设计见 §5。

## 1. 两条调用通道的现状

### 1.1 MCP 通道（有返回、有 schema，但覆盖面小）

| 项 | 值 |
|---|---|
| 端点 | `POST/GET/DELETE http://127.0.0.1:3501/mcp`（StreamableHTTP，仅 127.0.0.1） |
| 鉴权 | 请求头 `x-mcp-key: <key>`（key 存于 uTools 加密设置，本机当前值见 §7） |
| 工具数 | **25**（6 个插件声明） |
| 启用状态 | 本机已启用（`settings` 里 `enabledMcpServer: true`） |

25 个工具按插件分布：图片批量处理 12、ToDo 待办 5、Markdown 笔记 4、二维码助手 2、OCR 文字识别 1、本地搜索（Everything）1。

**实测证据**（QRCode 往返，2026-10-18 01:46）：
- `tools/call utools.QRCode.qrcode_generator {text:"HELLO-AWAM-E2E-20261018"}` → 生成 `Downloads\qrcode_1792259631713.png`
- `tools/call utools.QRCode.qrcode_decoder {inputPath:...}` → 返回 `{"text":"HELLO-AWAM-E2E-20261018"}` ✅ 逐字一致

**为什么只有 25 个**：uTools 的 MCP 工具不是从 plugin.json 的 features 自动生成的，而是插件作者在
`plugin.json.tools` 里显式声明（name/description/inputSchema/outputSchema）+ 运行时 `utools.registerTool()`
注册 handler。44 个插件里只有 6 个做了这件事。其余 38 个插件（138 个功能）只能走 UI/协议路径。

### 1.2 协议 CLI 通道（覆盖面全，但 fire-and-forget）

uTools 注册了 `utools://` 协议（`app.setAsDefaultProtocolClient("utools")`）。主进程收到
`utools://X` 后的处理链（读自 app.asar 反编译的 main.js）：

```
argv → decodeURIComponent(去前缀) → schemeRedirect(s)
  → 按第一个 "/" 拆成 label[]，第一个 "?" 后是 payload
  → label = [插件名, 功能关键词]；payload 字符串 → {type:"text", data:...}
  → 按插件名过滤 pluginContainer → redirectFeature({pluginIds, label, payload, isScheme})
  → 主窗口 executeJavaScript 里跳转功能；插件未运行会被拉起；找不到功能弹系统通知
```

**调用格式**：

```
cmd /c start "" "utools://<URL编码插件名>/<URL编码功能关键词>?<URL编码的文本payload>"
```

**实测证据**（2026-10-18 01:41）：
- `cmd //c start "utools://任务清单/任务清单"` → uTools 新 renderer 进程（PID 38688）诞生且存活，
  命令行确认是 `--type=renderer` 插件进程 ✅
- 带 payload 的调用（`utools://翻译/翻译?hello world`）流程已从代码级验证完整（§4 引用的 redirect 实现），
  但**无 stdout 返回**——结果只呈现在 uTools UI 里。

**payload 编码注意**：整串先被 `decodeURIComponent` 一次再拆 `/` 和 `?`，所以：
- 中文/空格必须编码；
- payload 里的 `?` 只有第一个是分隔符，其余归 payload——安全；
- payload 里的 `/` 在 `?` 之后，不破坏 label 拆分——安全；
- 经 URL 只能传 `text` 型 payload（`files`/`img` 型仅 MCP/UI 可达）。

## 2. 全量能力目录（本报告的核心数据）

从 `%APPDATA%/Roaming/uTools/plugins/*.asar` 提取（44 个插件全部解析成功）：

| 维度 | 数量 |
|---|---|
| 插件 | 44（其中 6 个同时有 MCP 工具） |
| 功能特性（features，协议可调） | **163** |
| MCP 工具（有 schema、有返回） | **25** |

功能数 top5：资源搜索 Lite（40）、闪电启动（12）、编码小助手（10）、颜色助手（8）、关闭进程（6）。

样例（资源搜索 Lite）：`code=list, keywords=["All Site","所有站点","全部站点"]` 等——
注意部分功能的 `code` 本身就是 UUID（用户自建站点条目），调用时以 keywords 匹配为准。

**asar 解析（纯 Python，无新依赖）**：文件格式 = 8 字节 Pickle（含 header JSON 长度）+ Pickle 包裹的
JSON header（`files:{文件名:{offset,size,unpacked}}`）+ 文件数据串。已实测可稳定提取 plugin.json；
`offset/size` 在 header 里是**字符串**需转 int。unpacked 文件走 `.asar.unpacked/` 旁路（本场景用不到）。

## 3. 与 Quicker 适配器的对照

| 维度 | Quicker（已接入） | uTools（本报告方案） |
|---|---|---|
| 目录来源 | `states/state_<uuid>.json` + 日志补名 | `plugins/*.asar` 内 `plugin.json.features[].cmds` |
| 调用命令 | `QuickerStarter.exe -c<秒> "runaction:<ID或名>?参数"` | `cmd /c start "" "utools://<插件名>/<关键词>?<文本>"` |
| 结果捕获 | stdout 回传（退出码不可信，按失败标记判） | **无**——协议唤起是 fire-and-forget |
| 参数类型 | 文本 | 仅文本（files/img 仅 MCP/UI） |
| 等待语义 | `-c<秒>` 阻塞等回传 | 不等；调用成功 = 协议被系统接收 |
| 附加通道 | — | 25 个 MCP 工具走 HTTP（有返回，但 awam BackendClient 目前只支持 stdio，需另立项） |

## 4. 适配方案设计（映射到 awam 现有架构）

沿用 Quicker 的 `kind=launcher` 三件套，**不动协议层**：

### 4.1 数据条目 `data/tools/utools.json`

```json
{
  "id": "utools",
  "kind": "launcher",
  "launcher": {
    "type": "utools",
    "dataDir": "%APPDATA%\\uTools\\plugins",
    "timeout": 10
  },
  "metadata": {
    "notes": "调用走 utools:// 协议（cmd start）；fire-and-forget 无返回，结果在 uTools UI"
  }
}
```

### 4.2 `src/launcher_client.py` 增加 utools 分支

- `_scan_utools()`：遍历 `dataDir/*.asar` → 提取 plugin.json → actions 列表
  （`id = "<pluginName>/<关键词>"`、`name = explain`、`keywords`、`source=auto`——
  asar 里的声明是完整的，不存在 Quicker 那种「尽力而为补名」问题）；
- `run_action()`：utools 分支构造 `cmd /c start "" "utools://..."`；
  - 子进程 `stdin/stdout/stderr=DEVNULL`（遵守 A8：绝不继承 server 管道）；
  - 成功判定：`start` 退出码 0 即「协议已投递」；返回体带 `fireAndForget: true` 警告调用方无结果回传；
  - 建议保留 `wait` 参数但语义改为「投递后观察窗口」——仅用于等 renderer 起来（实测 ~0.5s 内）。
- `LauncherClient.__init__` 的 adapter 分派 + `sync_actions` 白名单加 `utools`。

### 4.3 对外工具（零改动）

`sync_launcher_actions` / `list_launcher_actions` / `run_launcher_action` 直接复用；
SKILL.md 的工具表描述已覆盖 launcher 通用语义。

### 4.4 缓存与 TTL

`launcher-actions/utools.json` 走既有 A13 自动刷新（ACTIONS_CACHE_TTL=300s）；asar 内容静态，
刷新成本 = 44 次小文件头解析（每个 asar 只读前 ~8KB + header，实测全量 <2s）。

## 5. 关键限制（必须写进 metadata，防误用）

1. **无结果回传**：协议调用是「唤起并填入」，执行结果只在 uTools UI。要结果只有两条路：
   a) 该插件恰好声明了 MCP 工具（25 个）；b) 后续给 awam 的 BackendClient 加 HTTP MCP 传输
   （架构评审 ARC-P2-05 相关面，另立项）。
2. **payload 仅文本**：文件类功能（超级面板的 files/img）经 URL 不可达。
3. **依赖 uTools 运行中**：未运行则协议会拉起它（首调慢）。
4. **MCP key 会变**：用户在 uTools 设置里重置 MCP 服务会换 key；awam 侧若将来接 HTTP MCP，
   key 需可配置（`AWAM_UTOOLS_MCP_KEY` 或条目配置）。
5. **插件市场动态**：用户装/卸插件会改变目录；A13 的 300s TTL + `refresh=true` 已覆盖。

## 6. 实施任务分解（建议）

| # | 任务 | 估量 | 依赖 |
|---|---|---|---|
| U1 | `data/tools/utools.json` 条目 + schema 校验通过 | 小 | 无 |
| U2 | `launcher_client.py`：纯 Python asar 读取 + `_scan_utools` | 中 | U1 |
| U3 | `run_action` utools 分支（协议构造 + DEVNULL + fire-and-forget 语义） | 中 | U2 |
| U4 | 测试：目录解析（fixture 造 asar）、协议构造（URL 编码用例）、run 语义（mock subprocess） | 中 | U2/U3 |
| U5 | 本机实调验证（选 1~2 个无害功能真调一次）+ 文档同步 | 小 | U3 |

U4 的 asar fixture 用本报告验证过的 Pickle 格式构造即可，不引入 @electron/asar 依赖。

## 7. 本机连接信息（复用备查）

- MCP：`http://127.0.0.1:3501/mcp`，头 `x-mcp-key: c8f735380f88e46a701e441388fb3a46`（2026-10-18 时值）
- 插件目录：`C:\Users\Administrator\AppData\Roaming\uTools\plugins\`（44 个 asar）
- uTools 数据根：`C:\Users\Administrator\AppData\Roaming\uTools\`（settings / LevelDB `database/<hash>/`）
- 协议样例：`utools://%E4%BB%BB%E5%8A%A1%E6%B8%85%E5%8D%95/%E4%BB%BB%E5%8A%A1%E6%B8%85%E5%8D%95`（任务清单）

## 8. 勘察过程留痕（可复现命令）

```bash
# MCP 连通（initialize → tools/list）
curl -X POST http://127.0.0.1:3501/mcp -H "x-mcp-key: <key>" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"0.1"}}}'

# 协议唤起（Git Bash）
cmd //c start "utools://%E4%BB%BB%E5%8A%A1%E6%B8%85%E5%8D%95/%E4%BB%BB%E5%8A%A1%E6%B8%85%E5%8D%95"

# 端口归属（uTools 主进程 = MCP 监听者）
netstat -ano | grep 3501
```

> 注：`x-mcp-key` 存于 uTools 加密设置（原生 addon `localEncrypt2`，裸 Node 加载会崩，勿尝试硬解）；
> 从 uTools「设置 → AI 设置 → MCP 服务」页面可直接复制完整配置。
