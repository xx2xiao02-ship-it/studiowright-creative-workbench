# 算力中心代码整理与维护路线

## 1. 项目目标

Windows 工作台提供一个操作入口。Ubuntu 负责账号浏览器、任务执行和数据保存。统一 API 给外部程序使用。维护时应优先保证能找到当前运行的代码，能从同一份 Git 源码重新安装，能发现登录或连接失败的具体原因，能在升级出问题时只回退受影响的服务。

[部署手册](DEPLOYMENT.md)记录安装和回退步骤，[工作拓扑](WORKFLOW.md)记录各服务间的调用关系，[API 文档](API.md)记录外部调用约定。

## 2. 代码入口

下列 `workbench/`、`flow/console/`、`flow/source/` 位于 `packaging/ubuntu/snapshot/`。

| 模块 | 主要入口 | 日常改动位置 |
| --- | --- | --- |
| Windows 桌面端 | [`client/Desktop.cs`](../packaging/windows/client/Desktop.cs)、[`client/PanelSsh.cs`](../packaging/windows/client/PanelSsh.cs) | 桌面窗口、WebView、主机发现与连接、SSH 转发和重连 |
| Windows 安装与账号助手 | [`client/Setup.cs`](../packaging/windows/client/Setup.cs)、[`client-payload/`](../packaging/windows/client-payload)、[`build_client.py`](../packaging/windows/build_client.py) | 安装、旧连接复用、Chrome 账号管理、扩展与本机通信 |
| Ubuntu 工作台 | [`workbench/server.py`](../packaging/ubuntu/snapshot/workbench/server.py)、[`index.html`](../packaging/ubuntu/snapshot/workbench/index.html)、[`app.js`](../packaging/ubuntu/snapshot/workbench/app.js)、[`style.css`](../packaging/ubuntu/snapshot/workbench/style.css) | 操作台、账号池、任务记录、项目、素材、API 文档页面和后台协调 |
| 统一 API | [`workbench/unified_api.py`](../packaging/ubuntu/snapshot/workbench/unified_api.py)、[`unified_keys.py`](../packaging/ubuntu/snapshot/workbench/unified_keys.py) | API 鉴权、按任务类型分流和结果查询 |
| Flow 账号与任务 | [`flow/console/`](../packaging/ubuntu/snapshot/flow/console)、[`workbench/flow_readiness_recovery.py`](../packaging/ubuntu/snapshot/workbench/flow_readiness_recovery.py) | 本机账号、任务存储、窗口绑定、就绪检查、安全恢复和积分读取 |
| Flow 执行 | [`flow/source/src/studiowright/`](../packaging/ubuntu/snapshot/flow/source/src/studiowright) | `sw` 命令、Chrome 连接、页面操作、生成和下载 |
| Gemini | [`workbench/official_pool.py`](../packaging/ubuntu/snapshot/workbench/official_pool.py)、[`official_runtime.py`](../packaging/ubuntu/snapshot/workbench/official_runtime.py)、[`official_quota.py`](../packaging/ubuntu/snapshot/workbench/official_quota.py) | 账号池、请求排队、授权、模型调用、额度读取和素材分析 |
| 音乐 | [`workbench/music_jobs.py`](../packaging/ubuntu/snapshot/workbench/music_jobs.py)、[`music_recovery.py`](../packaging/ubuntu/snapshot/workbench/music_recovery.py)、[`packaging/music/`](../packaging/music) | 音乐任务进度、结果回收、积分状态和外部 FlowMusic2API 补丁 |
| 桌面定位和输入 | [`snapshot/`](../packaging/ubuntu/snapshot)、[`workbench/wayland_desktop_driver.py`](../packaging/ubuntu/snapshot/workbench/wayland_desktop_driver.py) | GNOME 窗口定位、Wayland 输入、前台输入排队和开机就绪检查 |
| Ubuntu 安装 | [`build_server.py`](../packaging/ubuntu/build_server.py)、[`server/`](../packaging/ubuntu/server) | 快照、离线依赖、服务配置、局域网发现、安装和复用模式 |
| 测试与说明 | [`tests/`](../tests)、[`docs/`](.) | 功能回归、打包校验、安装文档、API 和版本记录 |

## 3. Windows、Ubuntu 与 Git 核对结果

2026-11-25 在 GitHub `main` 的 `0d2be76` 上核对。Windows 的工作台、统一 API 与账号助手健康接口可访问，已安装的四个 Python 辅助入口与基线源码一致。桌面程序没有可用于反查提交的版本标识。Windows 自动复用连接还会调用旧目录 `unified-workbench/` 里的启动入口，不能仅凭目录名称判断可以删除。

Ubuntu 的 Flow、工作台、统一 API、发现和独立调度服务均在运行。页面标记显示 `v0.3.5`，Git `VERSION` 为 `0.3.7`。版本标记与文件差异共同说明，发布前需要核对各服务的实际来源。当前还在运行的独立调度服务由主机单独管理。

以部署位置对应的 195 个文件做只读核对。文本比较忽略 Windows 和 Ubuntu 换行格式差异。构建专用的离线依赖锁定表单独计入打包清单。

| 位置 | 一致 | 内容不同 | 预期路径未找到 |
| --- | ---: | ---: | ---: |
| 工作台 | 54 | 8 | 1 |
| Flow 后台 | 33 | 8 | 1 |
| Flow 执行层 | 24 | 2 | 0 |
| 第三方远程工具 | 57 | 0 | 3 |
| GNOME 扩展和输入助手 | 4 | 0 | 0 |
| 合计 | 172 | 18 | 5 |

- 工作台有差异的文件为 `activation.py`、`app.js`、`flow_check_readiness.py`、`index.html`、`live_pool.py`、`material_dispatch.py`、`official_pool.py`、`server.py`。
- Flow 后台有差异的文件为 `account_insights.py`、`adapter.py`、`browser_rendering.py`、`console_store.py`、`flow_proxy_guard.py`、`flow_window_maintenance.py`、`login_readiness.py`、`server.py`。打包脚本会对部分文件写入账号助手兼容改动，核对差异时应将这些改动考虑在内。
- Flow 执行层涉及 `src/studiowright/chrome.py`、`src/studiowright/flow/page.py`。
- 预期路径未找到的是 `workbench/boot_flow_readiness.py`、`flow/console/page_activity_guard.py` 以及三个第三方 `.deb` 构建归档。归档缺失只反映文件位置，服务实际安装方式需单独核对。

## 4. 当前整理

校正快照清单中 9 个旧文件指纹，补入 5 个已在 Git 管理的文件。打包时先核对源码和清单，只把清单内的快照文件写入安装包。测试验证修改后未更新的快照会被拒绝，未跟踪的本地设置也不会进入快照包。该检查作用于发布构建，运行服务保持原部署。

Windows 的旧副本、历史安装包、账号浏览器资料与 Ubuntu 数据先保留。把目录列为可归档前，需要检查桌面入口、服务配置、任务模块和构建脚本是否还引用它。线上任务、素材、数据库、密钥和授权资料单独备份与迁移。

## 5. 按这个顺序继续整理

| 优先级 | 要做的事 | 验收方式 |
| --- | --- | --- |
| 1 | 逐个服务核对 18 个差异，吸收线上必要修复，确认第三方依赖和独立调度的安装来源 | 建立包含服务、路径、版本和回退位置的清单。从指定 Git 提交打包并在隔离环境完成一轮部署验收 |
| 2 | 把 SSH 快捷入口和音乐后台的主机专用路径移入配置。给 Windows 界面和 Ubuntu 健康接口加实际提交号 | 换一台测试机，仅按部署手册填配置即可安装和连接。连接断开能诊断、能重连，发布后能查到两端提交号 |
| 3 | 先按操作台、账号、Flow、Gemini、音乐和 API 文档拆分 `workbench/app.js`，再按功能整理 `server.py` | 每次拆一个功能，使用真实入口验证渲染、交互和任务流程。前后端请求地址和既有数据保持兼容 |
| 4 | 合并行为已证实相同的 Flow 规则，加上 Windows 重连和任务恢复的回归验证 | `task_window_registry.py` 两处基线内容相同，可从这里开始。`dispatch_limit.py`、`flow_references.py`、`flow_window_policy.py` 和 `task_coordinator.py` 两处实现有差异，先补测试确认各自用途 |
| 5 | 归档确认无引用且可从 Git 或发布包恢复的旧代码与构建副本 | 归档后复查连接、启动与打包。线上数据和用户文件继续独立保管 |

升级验收覆盖 Windows 连接与重连、Flow 原窗口复用和用户登录提示、Gemini 并发和重试、音乐按原任务取回结果、统一 API 已有接入。遇到草稿、进行中的任务或等待验证的账号，验收应确认系统按原规则保留现场和状态。安装、升级、回退步骤见 [部署手册](DEPLOYMENT.md)。
