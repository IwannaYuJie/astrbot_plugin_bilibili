# B 站评论自动回复 AstrBot 插件设计文档

## 1. 目标

本插件的目标是：

- 运行在 AstrBot 插件体系内
- 复用 AstrBot 已接入的大模型能力
- 在云服务器上长期运行
- 监听 B 站评论区的新增评论
- 当评论命中规则（优先是 `@我`）时，自动生成拟人化回复
- 将回复发送回 B 站评论区

本阶段不追求复杂长期记忆，只追求：

- 被 `@` 后能回复
- 回复有稳定人设
- 能结合当前楼层少量上下文
- 尽量控制风控和稳定性风险

---

## 2. 非目标

第一阶段不做：

- 跨视频、跨天的长期用户记忆
- 多账号托管平台
- 面向外部用户的 SaaS 化能力
- 自动学习用户画像
- 高复杂度 Agent 编排
- 官方平台级接入（即不把 B 站作为 AstrBot 消息平台）

---

## 3. 插件定位

### 3.1 正确定位

这个项目应定位为：

**AstrBot 内部的后台服务型插件**。

而不是：

- 普通聊天指令插件
- B 站消息平台适配器
- 完整独立微服务

### 3.2 为什么这么定位

AstrBot 提供了以下能力：

- 插件生命周期
- 插件配置管理
- 统一 LLM 调用接口
- 插件存储能力
- 定时任务 / 后台任务承载能力

因此最合理的做法是：

- **B 站接入逻辑由插件自行维护**
- **大模型调用复用 AstrBot 的 provider**
- **插件通过后台轮询方式监听 B 站评论**

---

## 4. 总体架构

```text
AstrBot Core
 ├─ 插件配置（WebUI / _conf_schema.json）
 ├─ LLM Provider（已有 OpenAI / DeepSeek / 其他）
 ├─ 插件存储（KV / plugin_data）
 └─ 本插件 astrbot_plugin_bili_autoreply
      ├─ B站 API Client
      ├─ 评论轮询器 Poller
      ├─ 触发规则引擎 Rule Engine
      ├─ Prompt 构造器 Prompt Builder
      ├─ 回复生成器 Reply Generator
      ├─ B站回复执行器 Sender
      ├─ 状态存储 State Store
      └─ 管理指令 / 状态查看命令
```

---

## 5. 核心流程

### 5.1 初始化阶段

1. 插件启动
2. 读取配置
3. 初始化数据目录
4. 校验 Cookie / UID / Provider 等关键配置
5. 载入历史状态
6. 如启用自动轮询，则启动后台任务

### 5.2 单轮扫描流程

1. 获取视频列表（可缓存）
2. 获取最新评论列表
3. 对评论逐条过滤：
   - 是否已处理
   - 是否来自自己
   - 是否命中 `@我`
   - 是否命中关键词 / 黑名单 / 白名单
4. 为命中评论构造 prompt
5. 调用 AstrBot 的 LLM provider 生成回复
6. 进行回复前校验：
   - 长度限制
   - 敏感词过滤
   - 风险短路
7. 发送 B 站评论回复
8. 写入历史和日志

### 5.3 后台循环流程

```text
定时触发 -> 拉评论 -> 规则过滤 -> 生成回复 -> 发送回复 -> 保存状态 -> 等待下一轮
```

---

## 6. 模块设计

## 6.1 配置模块

负责读取并管理以下配置：

- `enabled`：插件是否启用
- `auto_poll`：是否自动轮询
- `poll_interval_seconds`
- `provider_id`
- `bilibili_cookie`
- `bilibili_refresh_token`
- `bilibili_uid`
- `reply_only_when_mentioned`
- `reply_prefix`
- `persona_prompt`
- `max_reply_chars`
- `max_comments_per_cycle`
- `video_cache_ttl_seconds`
- `dry_run`

## 6.2 B站 API Client

负责：

- 注入 Cookie 与请求头
- 调只读接口验证登录状态
- 获取 UP 主视频列表
- 获取指定视频评论列表
- 发送回复评论
- 后续支持 Cookie 刷新

约束：

- 使用 `httpx.AsyncClient`
- 避免使用 `requests`
- 统一异常包装
- 提供限速与超时配置

## 6.3 Poller

负责：

- 在固定间隔运行单轮扫描
- 控制同一时间仅有一个扫描任务在跑
- 统计每轮处理数量
- 发现异常时不中断 AstrBot 主进程

## 6.4 Rule Engine

职责：

- 是否只回复 `@我`
- 是否回复指定关键词
- 是否跳过自己发的评论
- 是否跳过已处理评论
- 是否跳过风控词/黑名单用户

第一版建议仅启用：

- 只回复 `@你`
- 跳过自己评论
- 跳过已处理评论

## 6.5 Prompt Builder

输入：

- 当前评论
- 视频标题
- 视频简介（可选）
- 当前楼层前几条评论
- 固定 persona prompt

输出：

- 一个适合给 AstrBot provider 的 prompt / contexts

原则：

- 不追求长上下文
- 不做复杂记忆
- 限制长度
- 强约束口吻、人设、输出格式

## 6.6 Reply Generator

职责：

- 调用 `self.context.llm_generate(...)`
- 指定 `provider_id`
- 生成回复文本
- 清洗多余换行、引用、过长内容

建议：

- 先固定使用插件配置里的 provider
- 不依赖“当前聊天会话 provider”
- 这样后台轮询任务更稳定

## 6.7 Sender

职责：

- 调 B 站回复接口
- 失败重试
- 频率限制
- 记录失败原因

### 幂等要求

- 同一 `comment_id` 只能成功回复一次
- 即使重启后也不能重复回复

## 6.8 State Store

建议持久化以下内容：

- 已处理评论 ID 集合
- 回复历史
- 上次扫描时间
- 视频缓存
- 最近错误日志
- Cookie 状态缓存

存储建议：

- 小量配置：AstrBot 插件配置 / KV
- 结构化运行数据：`data/plugin_data/astrbot_plugin_bili_autoreply/`

---

## 7. 数据目录建议

```text
data/plugin_data/astrbot_plugin_bili_autoreply/
 ├─ state.json                # 基础运行状态
 ├─ processed_comments.json   # 已处理评论ID
 ├─ video_cache.json          # 视频列表缓存
 ├─ reply_history.jsonl       # 回复历史
 ├─ errors.log                # 插件运行错误
 └─ debug/                    # 调试阶段输出
```

---

## 8. 配置设计建议

建议在 `_conf_schema.json` 中暴露以下配置：

### 基础配置

- 插件开关
- 自动轮询开关
- 轮询间隔
- 每轮最大处理数
- 调试模式

### B站配置

- Cookie
- refresh_token
- UP 主 UID
- 请求超时
- 视频缓存 TTL

### 回复配置

- 是否仅回复 `@我`
- 关键词列表
- 回复前缀
- 最大回复长度
- 是否启用 dry run
- 是否启用人工审核模式（后续）

### 模型配置

- provider_id（通过 AstrBot provider 选择器）
- persona_prompt
- temperature / max_tokens（后续可选）

---

## 9. 文案 / 人设策略

为了做出“类似评论区电子宠物”的感觉，不需要长期记忆，重点反而在：

1. 固定人格
2. 回复短小
3. 语气一致
4. 只在被 cue 到时出现

建议第一版 persona prompt 约束：

- 回复自然、像真人，不像客服
- 长度 20~60 字优先
- 尽量贴合评论上下文
- 不要自称模型或机器人
- 不要编造长期记忆
- 遇到敏感内容就柔和回避

---

## 10. 风险与边界

## 10.1 B站侧风险

本方案依赖网页侧登录态与接口，不是官方机器人能力，因此存在：

- 接口变动风险
- 风控 / 412 / 限流风险
- Cookie 失效风险
- 账号异常风险

## 10.2 模型侧风险

LLM 自动回复存在：

- 输出跑偏
- 生成不当内容
- 与用户争执升级
- 被诱导说敏感内容

## 10.3 运行侧风险

后台任务如果设计不当，可能：

- 拖慢 AstrBot
- 占用过多连接
- 出错后持续重试
- 插件重载后出现多实例轮询

---

## 11. 稳定性原则

1. **默认低频轮询**
2. **默认只回复 `@我`**
3. **默认每轮最多处理少量评论**
4. **默认启用去重**
5. **默认保留 dry run 模式**
6. **默认所有异常只记日志，不让插件崩溃主程序**

---

## 12. 阶段划分

### Phase 1：基础版

- 文档
- 配置 Schema
- 插件骨架
- 状态检查命令
- B 站只读探针
- LLM Dry Run

### Phase 2：读评论

- 拉视频列表
- 拉评论列表
- 读取上下文
- 单轮扫描

### Phase 3：手动回帖

- 管理命令触发一次回复
- 保证回复接口可用
- 先不自动轮询

### Phase 4：自动轮询

- 加入 cron / 后台循环
- 去重、限速、错误处理

### Phase 5：上线前增强

- 审核模式
- 黑白名单
- 敏感词过滤
- 更完善日志

---

## 13. 当前实现范围（本仓库首版）

当前仓库只实现：

- 文档与架构设计
- AstrBot 基础插件可加载骨架
- 配置 Schema
- 状态查看命令
- Cookie / UID / 登录态探针
- 评论只读扫描与 `@你` 识别
- LLM 回复 dry run

尚未实现：

- 评论轮询
- 自动回复
- 评论发送
- 定时任务
- Cookie 刷新

---

## 14. 上线前必备项清单

### 账号与平台

- [ ] B 站账号 Cookie
- [ ] `bili_jct`
- [ ] `SESSDATA`
- [ ] `refresh_token`（推荐）
- [ ] 目标 UP 主 UID

### AstrBot 侧

- [ ] AstrBot 正常运行
- [ ] 至少一个聊天 provider 可用
- [ ] 插件已能读取配置
- [ ] 插件数据目录可写

### 运行环境

- [ ] 云服务器网络可访问 B 站
- [ ] 云服务器时区、日志、磁盘空间正常
- [ ] 异常重启策略明确

### 安全与控制

- [ ] 先启用 dry run
- [ ] 先只回复 `@我`
- [ ] 先设置较长轮询间隔
- [ ] 先限制每轮处理数

