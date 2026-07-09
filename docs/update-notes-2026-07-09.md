# 更新说明（2026-07-09）

## 概览

本次工作区差异主要围绕四条主线：

- 修复 OpenAI/Auth0 注册流程最终 `create_account` 阶段的协议缺口，补齐 `authorize/continue` 与官方 Sentinel SDK 生成的双 token 头。
- 提升图片生成链路可靠性，新增 100 秒超时、继续等待、手动停止、冗余并发生成、失败重试和账号质量评分。
- 调整前端生图页体验，移除顶部“无限画布”入口，并在生图页右侧新增文字聊天面板。
- 在生图输入框增加“优化”能力，复用右侧文字聊天配置请求 GPT 优化当前生图提示词。

当前 `git diff --stat` 显示：20 个已跟踪文件变更，约 1313 行新增、320 行删除。另有 5 个未跟踪路径需要一并确认是否纳入提交。

## Git 差异范围

已修改文件：

- `Dockerfile`
- `api/image_tasks.py`
- `config.json`
- `services/account_service.py`
- `services/config.py`
- `services/image_task_service.py`
- `services/openai_backend_api.py`
- `services/protocol/conversation.py`
- `services/register/openai_register.py`
- `services/register_service.py`
- `test/test_register_proxy_runtime.py`
- `utils/sentinel.py`
- `web/src/app/image/components/image-composer.tsx`
- `web/src/app/image/components/image-results.tsx`
- `web/src/app/image/page.tsx`
- `web/src/app/settings/components/config-card.tsx`
- `web/src/app/settings/store.ts`
- `web/src/components/top-nav.tsx`
- `web/src/lib/api.ts`
- `web/src/store/image-conversations.ts`

新增未跟踪文件：

- `docs/update-notes-2026-07-09.md`
- `package.json`
- `utils/sentinel_sdk_runner.js`
- `web/src/app/image/components/image-chat-panel.tsx`
- `web/src/app/image/lib/chat-completions.ts`

## 主要更新

### 1. 注册流程协议修复

注册流程不再只依赖旧逻辑直接请求 `create_account`。在 `services/register/openai_register.py` 中，邮箱和密码提交成功后会读取 `continue_url`，并执行一次 `authorize/continue` 跳转，使流程更接近浏览器真实注册路径。

最终 `create_account` 阶段改为使用 `oauth_create_account` flow 生成 Sentinel 相关头：

- `OpenAI-Sentinel-Token`
- `OpenAI-Sentinel-SO-Token`

`utils/sentinel.py` 新增官方 Sentinel SDK 发现、下载、`/backend-api/sentinel/req` 请求、SDK 生成最终 token、SO token 生成与日志摘要能力。日志只记录 token 长度、SDK 版本、是否生成 SO token，不打印 token 明文。

`utils/sentinel_sdk_runner.js` 新增 Node 运行器，用于模拟浏览器环境执行当前官方 Sentinel SDK，并复用 SDK 内部逻辑生成 token。SO token observer 等待时间按官方前端逻辑使用 5000ms。

`Dockerfile` 新增 `nodejs`，因为 Sentinel SDK runner 需要 Node 运行环境。

`services/register_service.py` 和 `services/register/openai_register.py` 将邮箱 API 默认不走注册代理，避免验证码接收和注册代理绑定导致额外延迟或失败。

### 2. 生图模型与底层 slug 调整

`services/openai_backend_api.py` 将外部 `gpt-image-2` 映射到配置项 `image_default_model_slug`，默认值为：

```json
"image_default_model_slug": "gpt-5-5-thinking"
```

外部页面/API 仍然选择 `gpt-image-2`，底层 picture_v2 使用可配置的内部模型 slug。新增 `image_fallback_model_slug`，默认 `gpt-5-3`，用于后续兜底策略扩展。

### 3. 生图超时、继续等待与手动停止

`image_poll_timeout_secs` 默认从 120 秒调整为 100 秒。后端会定期把超过配置时间仍未完成的图片任务标记为失败，并返回明确超时提示。超时后用户可以继续等待、重试或直接发起新任务。

`api/image_tasks.py` 新增：

- `POST /api/image-tasks/{task_id}/resume-poll`
  - 请求体新增 `conversation_id`，用于本地任务缺失上游会话 ID 时兜底恢复轮询。
- `POST /api/image-tasks/{task_id}/stop`
  - 支持手动停止正在生成或排队中的图片任务。

`services/image_task_service.py` 修复继续等待时报错 `OpenAIBackendAPI.__init__() got an unexpected keyword argument 'proxy_url'` 的问题，恢复轮询时改为直接初始化 `OpenAIBackendAPI()`。

### 4. 生图冗余并发与失败兜底

新增后台冗余生成机制。开启后，一个前台图片任务可以在后台同时发起多份生成请求：

- 有任意一份成功，就返回成功结果。
- 多份都成功，则前端显示多张图。
- 单轮全部失败，进入下一轮重试。
- 最多重试轮数由配置控制。

相关配置：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `image_redundant_generation_enabled` | `true` | 是否开启后台冗余生成 |
| `image_redundant_copies` | `2` | 每轮并发生成份数，后端限制 1-4 |
| `image_redundant_max_attempts` | `3` | 最大重试轮数，后端限制 1-5 |

`services/protocol/conversation.py` 将图片轮询超时统一使用 `image_poll_timeout_secs`，不再在文本回复场景强制拉长到 300 秒，避免前台长时间卡在“生成中”。

### 5. 账号选择与图片额度质量评分

`services/account_service.py` 新增图片维度统计字段：

- `image_success_count`
- `image_failure_count`
- `image_consecutive_failures`
- `last_image_success_at`
- `last_image_failure_at`

图片账号选择从简单轮询改为评分排序，综合考虑：

- 当前账号图片在途数
- 本地额度或额度未知状态
- 账号套餐类型
- 图片成功次数
- 图片失败次数
- 连续失败次数
- 最近失败惩罚
- 最近成功加权
- 账号休息时间

这能降低连续使用失败账号的概率，并让近期成功的账号获得更高优先级。

### 6. 生图前端体验优化

`web/src/app/image/page.tsx` 和 `web/src/app/image/components/image-results.tsx` 更新了生图任务展示和控制：

- 生成中图片显示实时计时。
- 生成中图片支持手动停止。
- 继续等待时会携带本地保存的 `upstreamConversationId`，避免 `task has no conversation_id`。
- 同一会话内不同 turn 使用独立队列 key，避免任务互相阻塞。
- 支持一个后端任务返回多张成功图片。
- 生图失败不会阻塞用户再次发起新的图片生成。

`web/src/store/image-conversations.ts` 新增 `upstreamConversationId` 持久化字段，用于继续等待和恢复轮询。

### 7. 生图页右侧文字聊天面板

新增 `web/src/app/image/components/image-chat-panel.tsx`，并接入生图页右侧区域。聊天请求公共逻辑抽到 `web/src/app/image/lib/chat-completions.ts`，供右侧聊天和生图提示词优化复用。该面板支持：

- 从 `/v1/models` 拉取模型列表，并提供默认模型兜底。
- 选择模型或输入自定义模型。
- 选择思考程度：默认、低、中、高、超高。
- 流式调用 `/v1/chat/completions`。
- 停止生成。
- 上传图片作为多模态输入。
- 上传文本类文件作为上下文。
- 复制消息。
- 清空当前聊天。

右侧聊天的模型、自定义模型和思考程度会写入本地 `localStorage`，刷新后仍保留；聊天记录本身只存在前端状态中，刷新页面后不会持久化。

### 8. 生图提示词优化

`web/src/app/image/components/image-composer.tsx` 在生图输入框右上角新增“优化”按钮：

- 点击后会读取当前生图输入框内容，请求 `/v1/chat/completions` 进行优化。
- 优化调用复用右侧文字聊天的模型、自定义模型和思考程度配置。
- 默认优化提示词为：`请帮我优化这个提示词以达到更专业更优秀的效果，如果有短板帮我优化。`
- 优化提示词可在按钮旁的配置入口修改。
- 修改后的优化提示词会写入本地 `localStorage`，刷新页面不会丢失。
- 优化过程中按钮显示“优化中”，再次点击可中止请求。
- 模型返回结果会直接替换生图输入框内容，失败时保留原输入。

公共聊天调用封装在 `web/src/app/image/lib/chat-completions.ts`，提供：

- `ImageChatConfig`
- `DEFAULT_CHAT_MODELS`
- `getEffectiveChatModel`
- `streamChatCompletion`

这样右侧聊天和提示词优化共用同一套鉴权、流式解析、错误处理与模型选择逻辑。

### 9. 顶部导航清理

`web/src/components/top-nav.tsx` 移除了“无限画布”入口、第三方应用配置加载和跳转确认弹窗。顶部导航保留原有角色导航、移动端菜单、会话校验和退出登录。

## 配置变更

`config.json` 新增或调整：

```json
{
  "image_poll_timeout_secs": 100,
  "image_default_model_slug": "gpt-5-5-thinking",
  "image_fallback_model_slug": "gpt-5-3",
  "image_redundant_generation_enabled": true,
  "image_redundant_copies": 2,
  "image_redundant_max_attempts": 3
}
```

设置页同步新增对应编辑项，保存配置时会做最小/最大范围保护。

前端新增本地存储项：

| 存储键 | 说明 |
| --- | --- |
| `chatgpt2api:image_chat_model` | 右侧文字聊天和提示词优化共用的模型选择 |
| `chatgpt2api:image_chat_custom_model` | 自定义聊天模型 slug |
| `chatgpt2api:image_chat_reasoning` | 右侧文字聊天和提示词优化共用的思考程度 |
| `chatgpt2api:image_optimize_instruction` | 生图提示词优化的系统提示词 |

## API 变更

新增或更新的接口能力：

- `POST /api/image-tasks/{task_id}/resume-poll`
  - 新增请求体字段 `conversation_id?: string`
  - 用于继续等待时兜底恢复上游图片轮询。

- `POST /api/image-tasks/{task_id}/stop`
  - 停止当前图片任务。
  - 已完成任务会直接返回当前任务状态。
  - 未完成任务会标记为错误，并写入“已手动停止生成”。

前端 `web/src/lib/api.ts` 已同步新增：

- `resumeImagePoll(taskId, extraTimeoutSecs, conversationId?)`
- `stopImageTask(taskId)`

账号类型 `Account` 也新增图片成功/失败统计字段。

## 部署注意事项

1. 镜像需要包含 Node。

   `Dockerfile` 已安装 `nodejs`。如果线上使用旧镜像或旧基础镜像，需要重新构建后部署，否则 Sentinel SDK runner 会报 `node_not_found_for_sentinel_sdk`。

2. 线上部署必须保留数据卷和配置文件挂载。

   后续更新容器时应继续挂载：

   ```bash
   -v /root/chatgpt2api/data:/app/data
   -v /root/chatgpt2api/config.json:/app/config.json
   ```

   如果漏挂载，会表现为账号额度变 0、历史数据消失或配置回到镜像内默认值。

3. 不要把服务器密码、账号 token、Sentinel token、OpenAI token 写入提交或日志。

   本次 Sentinel 日志只输出长度和 SDK 摘要，符合这个要求。

## 验证清单

建议发版前执行：

```bash
uv run python -m py_compile services/register/openai_register.py utils/sentinel.py services/image_task_service.py services/protocol/conversation.py services/account_service.py
uv run python -m unittest test/test_register_proxy_runtime.py
cd web
bunx tsc --noEmit
bun run build
```

功能验收建议：

- 注册流程：验证码校验通过后，确认 `create_account` 请求头同时包含 `OpenAI-Sentinel-Token` 与 `OpenAI-Sentinel-SO-Token`，日志只显示长度和 SDK 版本。
- 生图超时：生成超过 `image_poll_timeout_secs` 后应进入失败状态，并显示继续等待/重试/新建任务能力。
- 继续等待：本地存在 `upstreamConversationId` 时，不应再出现 `task has no conversation_id`。
- 手动停止：点击停止后任务应结束，且不影响继续生成其他图片。
- 冗余生成：开启后，请求 1 张图时后台可并发生成多份；成功几份显示几份，失败分支不展示。
- 右侧聊天：支持发送、停止、复制、上传图片、上传文本文件、切换模型和思考程度。
- 提示词优化：输入生图提示词后点击“优化”，应能用右侧聊天配置请求 GPT，并把优化结果回填输入框；修改优化提示词后刷新页面应保留。

## 风险与后续

- `registration_disallowed` 如果在 Sentinel 双 token 修复后仍部分出现，不一定是协议问题。需要继续按邮箱 provider/domain 统计成功率，并停用低成功率临时邮箱域名。
- 冗余生图会提高成功率，但也会消耗更多账号额度和并发资源。建议先使用 `2` 份并发、`3` 轮重试观察线上成功率。
- 右侧聊天面板当前只持久化配置，不持久化历史。如果需要跨刷新保留聊天，可后续接入本地存储或后端会话。
- 提示词优化会直接替换输入框内容。后续如果需要更稳妥，可增加“预览优化结果 / 应用 / 取消”确认流程。
- `package.json` 当前是未跟踪新增文件，内容偏默认模板。提交前应确认它是否确实需要纳入版本库。
