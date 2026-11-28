# 原生 MP4 审核入口

入口沿用算力中心的本机 SSH 隧道与现有 `Authorization: Bearer <key>`。所有路径均需鉴权。同一个请求编号只供创建它的密钥查询和提交。调用方在上传前保存一个 UUID，并在续查时复用。

| 方法和路径 | 请求和返回 |
| --- | --- |
| `GET /api/native-video/capabilities` | 返回 `protocol: native-mp4-review-v1`、`available`、`requested_model`、`max_bytes`。服务未接入已验证的上游时，返回 `available: false` 和原因。 |
| `POST /api/native-video/reviews/{id}/media` | 以 `video/mp4` 原样发送有声音和画面的 MP4。提交单一 `Content-Length` 及 `X-Content-SHA256`。上限 100 MiB。服务流式复算文件指纹并验证音视频。相同编号和原件复用回执。 |
| `POST /api/native-video/reviews/{id}` | JSON 字段为 `model`、`sha256`、`prompt`。当前只接受 `gemini-3.8-flash`。一份原件加同一提示只派发一次。首次接受返回 `202`，原请求结果可续查。 |
| `GET /api/native-video/reviews/{id}` | 返回 `uploaded`、`running`、`completed`、`failed` 或 `unknown`。每份回执带 `request_id`、`requested_model`、`actual_model` 及 `media`。网络中断后先查原编号。 |

`media` 包含 `sha256`、`bytes`、`mime_type`、`audio_present`。通过的回执包含 Google 实报的 `provider_model` 与 `response_id`、服务端账号指纹 `account_ref`、`input_media`、`provider_evidence` 和 `review`。`input_media` 来自算力中心在 HTTPS 请求编码时计算的原件字节数与指纹，以及入库时解码核实的音轨。`provider_evidence` 保存请求方式、HTTP 状态、Google 原始响应指纹，以及 Google 提供的输入模态用量。Google 的公开响应没有原件指纹字段。音轨内容由实测盲读复核。掉线结果待核、错模型、送审文件不符、缺少上游回执都会阻断审核。已提交的请求可凭原编号续查。进程中断会保留编号并报告 `unknown`。

## 上线依赖

网关默认停在 `native_video_provider_unverified`。隔离服务使用的 Python 环境需预置 `google-auth[requests]`，并可读取已有 Google Cloud 项目和可识别账号的应用默认凭据（ADC）。分别将 `SW_NATIVE_VERTEX_ENABLED` 与 `SW_NATIVE_VERTEX_PROJECT` 设置为 `1` 和该项目 ID。项目须已启用 Gemini Enterprise Agent Platform API，ADC 身份须有 `aiplatform.endpoints.predict` 权限。算力中心密钥继续验证客户端身份。Google 身份和项目经算力中心服务进程配置。 [Google Cloud 鉴权与权限](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/start)

适配器调用 `aiplatform.googleapis.com` 的 `v1` `GenerateContent`，模型固定为 `gemini-3.8-flash`，从原件直接编码一个 `inlineData` 视频部分，保留整个 MP4 容器和音轨。验收应先用同一短样盲读画面码和音轨词，核对 Google 返回的模型与响应编号及网关记录的原件指纹，再用同一回执交 Hydrogen 高档终审。隔离测试的回包只用于验证接口接线。 [模型和输入限制](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/3-8-flash) [Google 响应字段](https://docs.cloud.google.com/gemini-enterprise-agent-platform/reference/rest/v1/GenerateContentResponse)
