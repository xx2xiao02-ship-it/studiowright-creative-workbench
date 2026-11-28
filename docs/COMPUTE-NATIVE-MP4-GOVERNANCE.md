# 算力中心原生 MP4 审核治理单

## 当前结论

- 当前生产算力服务仍是 `42f9176` 对应的工作台版本，`/api/native-video/capabilities` 返回 `404`。正式服务没有改动。
- 算力中心已有网关密钥可用于鉴权，不能替代 Google Vertex 的授权身份。
- 通过 SSH 只读核查，目标 Linux 服务未发现 `google-auth`、`google-genai`、ADC 文件、服务账号文件、`gcloud` 命令或 `SW_NATIVE_VERTEX_PROJECT` 配置。没有可证明的 Vertex 视频推理授权。
- 7 秒样片 `4286ba89087164c897f4028e60ce21297912c0410ce45decb2644e028be1eda9` 已确认是 47,342 字节、7 秒、H.264 视频和 AAC 音频。原片可以完整解码。

因此，真实 Flash 和 Hydrogen 本轮不能验收，正式入口不能开放，100 秒任务保持暂停。

## 已实现的隔离适配器

提交 `b6750a5` 在网关内增加默认关闭的 Vertex GenerateContent 适配器。

- 仅接受 `gemini-3.8-flash`，不回退模型。
- 通过 `POST /v1/projects/{project}/locations/global/publishers/google/models/gemini-3.8-flash:generateContent` 发送完整原始 MP4。
- 只发送一个 `inlineData` 的 `video/mp4` 部分，保留容器内视频和音轨。
- 通过 `modelVersion`、`responseId`、ADC 身份指纹、输入字节数、输入 SHA-256、音轨标志和 Google 原始回执指纹形成可查回执。
- `SW_NATIVE_VERTEX_ENABLED=1` 且 `SW_NATIVE_VERTEX_PROJECT` 合法时才尝试装配适配器。缺依赖、缺 ADC 或身份不完整时保持关闭。

## 上线前置条件

算力中心管理员需要提供已有的 Google Cloud 项目和已授权服务端身份。身份至少需要 `aiplatform.endpoints.predict`，项目需要启用 Gemini Enterprise Agent Platform API。把 `google-auth[requests]` 安装进目标 Linux 的受控运行环境，并把项目 ID 和开关配置到服务进程。禁止把密钥写入代码、提交记录、日志或回执。

## 验收顺序

1. 先检查 `GET /api/native-video/capabilities` 返回 `available: true`，模型为 `gemini-3.8-flash`。
2. 上传同一 7 秒原片，核对 `sha256`、字节数、`video/mp4` 和 `audio_present: true`。
3. 用原片提示要求读出画面码 `DC4D5B3C` 和音轨词 `waterfall`、`cobalt`、`cinnamon`。
4. 只有 Google 实际回执同时提供正确模型、响应编号、输入证据和通过的画面及音轨实读，Flash 才算通过。
5. 把同版 Flash 回执作为材料交给 `gemini-4-hydrogen-high`，保存 Hydrogen 实际型号、请求编号和 `passed` 或 `revise`。
6. Flash 或 Hydrogen 任一条件缺失时，保留原请求编号，状态为 `unknown` 或失败，不切换正式入口。

## 回滚

回滚只需停止使用该隔离分支，恢复 `42f9176` 的默认关闭网关。无需修改工作台、无需删除请求记录、无需触碰暂停任务。适配器开关关闭或 ADC 不可用时，能力接口自动返回不可用原因。

## 证据

- 隔离接入证据位于 `outputs/算力中心-MP4-隔离复验证据.json`。
- Google 官方文档说明 `gemini-3.8-flash` 支持 `GenerateContent`、`inlineData` 和带音频视频输入。模型响应包含 `modelVersion` 和 `responseId`。
