# 原生 MP4 审核入口

入口沿用算力中心的本机 SSH 隧道与现有 `Authorization: Bearer <key>`。所有路径均需鉴权。同一个请求编号只供创建它的密钥查询和提交。调用方在上传前保存一个 UUID，并在续查时复用。

| 方法和路径 | 请求和返回 |
| --- | --- |
| `GET /api/native-video/capabilities` | 返回 `protocol: native-mp4-review-v1`、`available`、`requested_model`、`max_bytes`。服务未接入已验证的上游时，返回 `available: false` 和原因。 |
| `POST /api/native-video/reviews/{id}/media` | 以 `video/mp4` 原样发送有声音和画面的 MP4。提交单一 `Content-Length` 及 `X-Content-SHA256`。上限 100 MiB。服务流式复算文件指纹并验证音视频。相同编号和原件复用回执。 |
| `POST /api/native-video/reviews/{id}` | JSON 字段为 `model`、`sha256`、`prompt`。当前只接受 `gemini-3.8-flash`。一份原件加同一提示只派发一次。首次接受返回 `202`，原请求结果可续查。 |
| `GET /api/native-video/reviews/{id}` | 返回 `uploaded`、`running`、`completed`、`failed` 或 `unknown`。每份回执带 `request_id`、`requested_model`、`actual_model` 及 `media`。网络中断后先查原编号。 |

`media` 包含 `sha256`、`bytes`、`mime_type`、`audio_present`。通过的回执还包括上游实报的 `provider_model`、账号标识 `account_ref`、响应编号 `response_id`、同结构的上游输入凭证 `input_media` 和有效 `review`。掉线结果待核、错模型、文件或音轨凭证不符都会阻断审核。上游未就绪时写接口返回 `503`，同一任务可继续查已有回执。已运行的请求遇进程中断会保留编号并报告 `unknown`。

## 上线依赖

网关默认停在 `native_video_provider_unverified`。真实 adapter 必须使用算力中心已经授权的服务端账号，直接交付完整原始 MP4，提供可核验的实际模型、账号与响应编号，确认上游接收的原文件指纹、大小及音轨，并保留可查询的派发记录。模型需要固定为 3.8 Flash，禁止回退。模型回覆里自行声明的型号或文件指纹不能作为上游凭证。网关里的测试 adapter 仅用于隔离验收。

启用前，先由真实上游盲测短片的一处画面与一处仅出现在音轨里的内容，再核验两份约 100 秒成片的输入指纹和内容，以及错误模型和断线续查。用同一服务端运行方式复验，确认可调用小样后，再考虑开放能力。Google 公告计划在 2026 年 11 月 30 日移除 Antigravity 的 3.8 Flash。 [Google 模型说明](https://antigravity.google/docs/models/#reasoning-model)
