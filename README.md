# astrbot_plugin_bili_autoreply

基于 AstrBot 的 B 站评论区自动回复插件。

> 当前仓库已具备：登录探针、WBI 视频列表读取、评论扫描、`@你` 命中识别、自动生成回复、手动执行一轮自动回复、可选后台自动轮询回复。

## 当前已完成

- 完整的方案设计文档
- AstrBot 插件基础骨架
- 插件配置 Schema
- B站登录态探针
- WBI 视频列表读取
- 评论只读扫描与 `@你` 命中识别
- 基于 AstrBot LLM 的自动回复生成
- 单轮自动回复执行
- 消息中心触发源（@我 / 回复我）
- 已处理评论与消息去重持久化
- 可选后台轮询自动回复

## 当前基础命令

- `/bili_status`
  - 查看当前插件配置、运行状态、是否开启自动轮询、已处理评论数
- `/bili_probe`
  - 探测登录态、WBI keys、视频列表接口是否正常
- `/bili_cookie_status`
  - 查看 Cookie 当前是否需要刷新
- `/bili_refresh_cookie`
  - 使用 `bilibili_refresh_token` 手动刷新 Cookie，并写回插件配置
- `/bili_scan`
  - 扫描最近几条评论，预览评论与 `@你` 命中情况
- `/bili_scan_mentions`
  - 仅显示命中 `@你` 的评论
- `/bili_scan_debug`
  - 输出视频扫描明细与评论样本，方便排障
- `/bili_dry_run 你好，测试一下人设回复`
  - 直接调用 AstrBot 当前配置的大模型，验证人设 Prompt 和回复链路
- `/bili_msg_debug`
  - 查看消息中心里的未读计数、接口返回状态、可用触发项（@我 / 回复我）及其关键字段
- `/bili_run_once`
  - 基于消息中心触发源立即执行一轮自动回复流程；如果 `dry_run=true` 则只生成不发送

## 最关键配置

### 必填

1. `bilibili_cookie`
   - 至少包含 `SESSDATA`
   - 回复时必须包含 `bili_jct`
2. `bilibili_uid`
3. `provider_id`

### 自动回复前建议确认

4. `dry_run=true`
5. 先用 `/bili_msg_debug` 看消息中心触发范围
6. 再用 `/bili_run_once` 观察生成结果
7. 确认无误后再把 `dry_run=false`
8. 最后视情况开启 `auto_poll=true`

## 自动回复推荐上线顺序

### 第一步：验证命中范围
- `/bili_probe`
- `/bili_scan_debug`
- `/bili_scan_mentions`
- `/bili_msg_debug`

### 第二步：演练回复
- 保持 `dry_run=true`
- 执行 `/bili_run_once`
- 查看插件回复历史和控制台日志

### 第三步：真实发送
- 改为 `dry_run=false`
- 再执行 `/bili_run_once`

### 第四步：后台自动轮询
- 开启 `auto_poll=true`
- 配置合适的 `poll_interval_seconds`
- 建议一开始 `max_comments_per_cycle=1~3`

## 数据文件

插件会在：

- `data/plugin_data/astrbot_plugin_bili_autoreply/`

下生成：

- `processed_comments.json`：已处理评论 ID
- `reply_history.jsonl`：回复历史
- `state.json`：运行状态文件

## 风险提醒

- 本插件依赖 B站网页侧接口，存在风控、接口变动、Cookie 失效风险
- 建议默认只回复 `@你` 的评论
- 建议先 `dry_run`，确认命中和文案正确后再发真实评论
- 建议不要把轮询间隔设太短

## 当前已初步支持的发送场景

- 视频评论回复
- 动态评论回复（第一版映射）

## 当前仍未完成

- Cookie 自动刷新
- 更完善的敏感词过滤
- 审核模式
- 黑白名单

## 参考文档

- AstrBot 插件开发总览：<https://docs.astrbot.app/dev/star/plugin-new.html>
- 最小实例：<https://docs.astrbot.app/dev/star/guides/simple.html>
- 插件配置：<https://docs.astrbot.app/dev/star/guides/plugin-config.html>
- 调用 AI：<https://docs.astrbot.app/dev/star/guides/ai.html>
- 插件存储：<https://docs.astrbot.app/dev/star/guides/storage.html>

## 首次运行说明

为避免插件一上来把消息中心里的历史 @/回复 全部处理掉，当前版本加入了**首次消息基线**：

- 第一次执行 `/bili_run_once` 时，会先记录当前最新消息位置
- 不会立即回复旧消息
- 之后你再产生新的 @/回复，再执行 `/bili_run_once` 或开启 `auto_poll`，插件才会处理这些新消息

## 插件改名说明

为避免与 AstrBot 官方市场中已存在的 `astrbot_plugin_bilibili` 同名插件冲突，当前插件已改名为：

- `astrbot_plugin_bili_autoreply`

插件启动时会尽量从旧数据目录迁移已有状态文件：

- 旧目录：`data/plugin_data/astrbot_plugin_bilibili/`
- 新目录：`data/plugin_data/astrbot_plugin_bili_autoreply/`
