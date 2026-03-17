# astrbot_plugin_bilibili

基于 AstrBot 的 B 站评论区自动回复插件。

> 当前仓库为 **第一阶段基础版**：先完成方案文档、配置设计和最小插件骨架，方便后续按文档逐步开发成可上线版本。

## 当前已完成

- 完整的方案设计文档
- AstrBot 插件基础骨架
- 插件配置 Schema
- 基础命令：状态检查、Cookie/UID连通性验证、评论只读扫描、LLM 回复 Dry Run
- 插件数据目录初始化

## 目标能力（后续迭代）

- 定时轮询 UP 主自己视频的最新评论
- 仅在评论命中 `@我` / 关键词 / 指定规则时触发回复
- 调用 AstrBot 已配置的大模型生成回复
- 自动回发 B 站评论
- 去重、限流、日志、失败重试、人工审核开关

## 仓库结构

- `docs/design.md`：完整设计文档
- `docs/development-plan.md`：分阶段开发计划
- `main.py`：最小可运行插件骨架
- `_conf_schema.json`：AstrBot WebUI 配置定义
- `metadata.yaml`：插件元信息
- `requirements.txt`：插件依赖

## 当前基础命令

加载插件后，可先使用以下命令验证环境：

- `/bili_status`
  - 查看当前插件配置、运行状态、必要字段是否已填写
- `/bili_probe`
  - 使用当前 Cookie 调 B 站只读接口，检查登录状态和 UID 配置是否可用
- `/bili_scan`
  - 扫描最近几条评论，预览哪些评论命中了 `@你`
- `/bili_scan_mentions`
  - 仅显示命中 `@你` 的评论
- `/bili_scan_debug`
  - 输出视频扫描明细与评论样本，方便定位“到底有没有读到评论”
- `/bili_dry_run 你好，测试一下人设回复`
  - 直接调用 AstrBot 当前配置的大模型，验证人设 Prompt 和回复链路

## 需要提前准备

### 必需

1. B 站登录 Cookie
   - 至少包含 `SESSDATA`
   - 必须包含 `bili_jct`
2. B 站 UP 主 UID
3. AstrBot 中已经可用的聊天模型 Provider
4. 云服务器上的 AstrBot 运行环境

### 强烈建议

5. `refresh_token`
6. 一个稳定的人设 Prompt
7. 明确的触发规则（先建议只回复 `@你`）
8. 合理的轮询间隔和频率限制

## 开发建议

第一版建议按下面顺序落地：

1. 文档与配置结构
2. 只读探针能力（Cookie/UID/视频列表）
3. 评论只读扫描（@命中识别）
4. LLM Dry Run
5. 手动触发回复
6. 定时自动轮询
7. 限流 / 去重 / 日志 / 审核

## 参考文档

- AstrBot 插件开发总览：<https://docs.astrbot.app/dev/star/plugin-new.html>
- 最小实例：<https://docs.astrbot.app/dev/star/guides/simple.html>
- 插件配置：<https://docs.astrbot.app/dev/star/guides/plugin-config.html>
- 调用 AI：<https://docs.astrbot.app/dev/star/guides/ai.html>
- 插件存储：<https://docs.astrbot.app/dev/star/guides/storage.html>

## 说明

本项目当前版本还 **没有启用自动回复与定时轮询**，但已经支持评论只读扫描与 `@你` 命中预览，后续将按设计文档逐步补全。
