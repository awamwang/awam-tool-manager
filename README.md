# awam-tool-manager

统一工具管理器 - 维护启动器工具集名单，提供统一的工具注册、搜索、启动能力。

## 快速开始

### 安装依赖

```bash
pip install -r requirements.txt
```

### 运行 MCP Server

```bash
cd src
python server.py
```

### 在 MCP 客户端中配置

```json
{
  "mcpServers": {
    "awam-tool-manager": {
      "command": "python",
      "args": ["G:\\Projects\\19AI\\skills\\awam-tool-manager\\src\\server.py"]
    }
  }
}
```

## 已接入启动器

- [x] Everything（文件搜索）
- [ ] Steam（游戏平台）- 待接入
- [ ] WinGet（包管理器）- 待接入
- [ ] npm（包管理器）- 待接入
- [ ] Scoop（包管理器）- 待接入
- [ ] uTools（效率启动器）- 待接入
- [ ] WeGame（游戏平台）- 待接入

## 文档

- [格式规范](docs/format.md)
