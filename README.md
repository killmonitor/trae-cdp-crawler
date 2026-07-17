# TRAE CDP Chat Crawler

通过 Chrome DevTools Protocol (CDP) 提取 TRAE 聊天记录的 Python 脚本。支持自动区分 AI 与用户对话，去除思考过程，仅保留正文。

## 功能

- **角色识别** — 基于 `data-role` + `data-message-id` 精准区分 AI / 用户消息
- **思考过程过滤** — 通过 `data-item-type` 选择器移除 thought、todo、explore 等非正文块
- **两层清洗** — JS 层最小化 DOM 移除 + Python 层正则清洗（去除 "TRAE Work"、任务耗时等 UI 噪音）
- **自动保存** — 每 6 轮无新内容自动写入 checkpoint 文件，防止长时间运行丢失数据
- **安全终止** — Ctrl+Q 全局热键停止，CDP 超时不阻塞停止信号，连续失败自动退出
- **虚拟列表适配** — 200px 步长滚动 + 1.0s 等待，确保虚拟列表充分渲染

## 环境要求

- Python 3.8+
- TRAE SOLO CN（桌面版）
- 依赖库：`websockets`、`keyboard`（脚本会自动安装）

## 使用方法

1. 关闭 TRAE，用调试端口重新启动：

```bash
"D:\TRAE_SOLO\TRAE SOLO CN\TRAE SOLO CN.exe" --remote-debugging-port=9222
```

> 路径请根据你的实际安装位置修改。

2. 在 TRAE 中打开目标对话，手动滚动到最上方，等历史消息加载完成。

3. 运行脚本：

```bash
python crawl_trae_current.py
```

4. 脚本会自动向下滚动并提取消息。随时按 **Ctrl+Q** 停止。

5. 输出文件（脚本所在目录）：
   - `trae_chat_current.md` — 最终聊天记录
   - `trae_chat_checkpoint.md` — 自动备份

## 输出格式

```markdown
# TRAE 聊天记录

总条数: 35（用户: 17, AI: 18, 未知: 0）

---

1. 【用户】
你的问题内容

2. 【AI】
AI 的回复正文
```

## 注意事项

- Ctrl+C 已被禁用，请使用 **Ctrl+Q** 终止
- 如果长时间挂起无新内容，脚本会自动保存 checkpoint 后继续
- CDP 连接端口默认 `localhost:9222`，如需修改请编辑脚本中的 `CDP_HTTP` 变量
- `keyboard` 库需要管理员权限才能注册全局热键

## License

MIT
