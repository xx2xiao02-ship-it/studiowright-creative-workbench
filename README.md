# 算力中心 StudioWright

一个连接 Flow 创作服务、官方 Gemini 和音乐服务的算力中心，包含 Ubuntu 服务端和 Windows 桌面操作端。Windows 端使用独立窗口，支持系统托盘和局域网发现。

当前源码版本：**v0.3.7**。见 [版本记录](CHANGELOG.md) 与 [API 接入说明](docs/API.md)。当前源码后的修复仍以 `main` 分支提交为准。

## 安装使用

Windows 和 Ubuntu 安装包在本仓库的「Releases」下载。见 [安装说明](docs/安装说明.md) 与 [API 接入说明](docs/API.md)。Ubuntu 可复用已运行的服务，Windows 安装时可选择创建桌面快捷方式。

[Windows 与 Ubuntu 部署手册](docs/DEPLOYMENT.md)包含源码目录、环境准备、账号接入、打包、验收和升级回退方法。

[代码地图与维护路线](docs/CODEBASE.md)包含各模块入口、运行版本核对结果和整理优先级。

Windows、Ubuntu、统一 API、账号治理、Gemini、Flow 与素材库的关系见 [全项目工作拓扑](docs/WORKFLOW.md)，含调度、恢复及内容交接分图。

## 项目内容

- `packaging/ubuntu/snapshot/flow/source`：最新的 `sw` 工作流 CLI，包含 Flow 和 Flow Music 驱动。
- `packaging/ubuntu/snapshot/flow/console`：Flow 账号、任务、素材库和后台服务模块。
- `packaging/ubuntu/snapshot/workbench`：统一算力中心、官方 Antigravity Gemini、素材分析和音乐任务模块。
- `packaging/music`：外部 FlowMusic2API 的会话续期维护工具。
- `packaging/windows`：Windows 独立窗口、安装程序、账号助手及图标源码。

Ubuntu 与 Windows 打包脚本分别为 `packaging/ubuntu/build_server.py` 和 `packaging/windows/build_client.py`。自行重新打包时，先为 Ubuntu 准备 `wheelhouse-linux/flow` 中与锁定清单匹配的离线依赖；Windows 需 .NET 编译器、`client-payload/runtime` 下的 Python 嵌入式运行环境及 WebView2 SDK。`fetch_webview.py` 可下载并校验 WebView2 SDK。Windows 脚本会从源码编译桌面端和安装器。

## 数据和许可

仓库与安装包不包含现有账号登录信息、密钥、素材和对话记录。迁移这些数据需要另行处理，不能仅靠安装包恢复。

本项目包含第三方组件，请遵循各自许可证。Flow 源码的许可证位于 `packaging/ubuntu/snapshot/flow/source/LICENSE`，noVNC 等组件保留各自的许可文件。
