"""
TRAE CDP 聊天记录爬虫 v8 — 自动保存 + CTRL+Q 高可靠终止
- data-role 精准区分 AI/用户
- 6 轮无新内容自动保存 checkpoint 并继续
- CDP 通信超时不会阻塞停止信号
- 连续 CDP 失败自动安全退出

使用方法：
  1. 关闭 TRAE
  2. 用调试端口启动：
     "D:\TRAE_SOLO\TRAE SOLO CN\TRAE SOLO CN.exe" --remote-debugging-port=9222
  3. 在 TRAE 中手动滚动到对话最上方，等历史加载完成
  4. 运行：python crawl_trae_current.py
  5. 按 CTRL+Q 停止爬取并输出（Ctrl+C 已被禁用）
  6. 输出：trae_chat_current.md（脚本所在目录）
     checkpoint: trae_chat_checkpoint.md（自动备份）
"""
import asyncio
import json
import os
import re
import signal
import sys
import threading
import urllib.request
from datetime import datetime

try:
    import websockets
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "websockets", "-q"])
    import websockets

try:
    import keyboard
except ImportError:
    import subprocess
    print("正在安装 keyboard 库...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "keyboard", "-q"])
    import keyboard

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_FILE = os.path.join(SCRIPT_DIR, "trae_chat_current.md")
CHECKPOINT_FILE = os.path.join(SCRIPT_DIR, "trae_chat_checkpoint.md")
CDP_HTTP = "http://localhost:9222/json"

# ── Ctrl+Q 停止机制 ──────────────────────────────────────────────
stop_flag = threading.Event()
_keyboard_registered = False


def on_ctrl_q():
    """Ctrl+Q 回调"""
    print("\n\n  [检测到 Ctrl+Q，正在停止并生成输出...]")
    stop_flag.set()


def ignore_ctrl_c(sig, frame):
    print("\n  [提示] 请使用 CTRL+Q 停止脚本，Ctrl+C 已被禁用。")


signal.signal(signal.SIGINT, ignore_ctrl_c)

try:
    keyboard.add_hotkey("ctrl+q", on_ctrl_q)
    _keyboard_registered = True
except Exception:
    print("  [警告] keyboard 热键注册失败，请以管理员运行或直接关闭终端结束")
    # 兜底：Ctrl+C 恢复原始行为
    signal.signal(signal.SIGINT, signal.SIG_DFL)


# ── CDP 通信层 ────────────────────────────────────────────────────
class CDP:
    def __init__(self, ws):
        self.ws = ws
        self.nid = 1

    async def eval(self, expr, timeout=10):
        """
        执行 JS 并返回结果。
        超时或连接断开时返回 None（不会抛异常阻塞调用方）。
        """
        mid = self.nid
        self.nid += 1
        try:
            await asyncio.wait_for(
                self.ws.send(
                    json.dumps({
                        "id": mid,
                        "method": "Runtime.evaluate",
                        "params": {"expression": expr, "returnByValue": True},
                    })
                ),
                timeout=timeout,
            )
            while True:
                r = json.loads(
                    await asyncio.wait_for(self.ws.recv(), timeout=timeout)
                )
                if "id" in r and r["id"] == mid:
                    res = r.get("result", {})
                    if "exceptionDetails" in res:
                        return None
                    return res.get("result", {}).get("value", "")
        except (asyncio.TimeoutError, Exception):
            return None


# ── 消息提取 ──────────────────────────────────────────────────────
async def extract_messages(cdp):
    """
    按 data-role + data-message-id 提取每条消息的原始全文。
    AI 回复正文嵌套在 plan/thought/todo 块内部，因此不做 DOM 级移除，
    只去掉纯 UI 装饰，正文清洗交给 Python 层的 clean_ai_text()。
    返回 [{text, role}, ...]
    """
    r = await cdp.eval(
        """
        (() => {
            const chat = document.querySelector('.ai-chat');
            if (!chat) return 'NO_CHAT';

            const results = [];
            const seen = new Set();

            /**
             * 区分三类元素：
             *   A) 纯 UI 装饰 — 直接移除
             *   B) 思考过程块 — plan-item:thought / task:todo-group /
             *      task:todo-progress-container / task:explore-group
             *      (与 plan-item:toolcall 是兄弟元素，移除不影响正文)
             *   C) 正文块 — plan-item:toolcall (AI 最终回复，保留)
             */
            const TO_REMOVE = [
                // ── 纯 UI 装饰 ──
                '.agent-message__header', '.agent-message__title',
                '.agent-avatar--solo-work',
                '[data-item-type="turn:assistant-avatar"]',
                '.latest-assistant-bar', '.assistant-action-bar',
                '.user-message__bottom-bar', '.user-message__time',
                '.user-message__icon-wrapper', '.user-message__attached-cards',
                '.user-message__attached-card',
                // ── 思考过程 / Todo / 操作日志 ──
                '[data-item-type="plan-item:thought"]',
                '[data-item-type="task:todo-group"]',
                '[data-item-type="task:todo-progress-container"]',
                '[data-item-type="task:explore-group"]',
                '[data-item-type="agent:reference-list"]',
                '[data-item-type="agent:before-plans"]',
                '[data-item-type="agent:after-plans"]',
                '[data-item-type="agent:notification"]',
                '[data-item-type="widget:action-bar"]',
            ];

            function extractRawText(container) {
                const clone = container.cloneNode(true);
                for (const sel of TO_REMOVE) {
                    try { clone.querySelectorAll(sel).forEach(e => e.remove()); }
                    catch(e) {}
                }
                return clone.textContent.trim();
            }

            // ── 按 data-role + data-message-id 提取 ──
            const roleEls = chat.querySelectorAll(
                '[data-role="user"], [data-role="assistant"]');
            const processed = new Set();

            for (const roleEl of roleEls) {
                const role = roleEl.getAttribute('data-role');
                if (role !== 'user' && role !== 'assistant') continue;

                let container = roleEl;
                while (container && !container.getAttribute('data-message-id'))
                    container = container.parentElement;
                if (!container) continue;

                const msgId = container.getAttribute('data-message-id');
                if (processed.has(msgId)) continue;
                processed.add(msgId);

                const text = extractRawText(container);
                if (text.length < 5) continue;

                const preview = text.substring(0, 150);
                if (seen.has(preview)) continue;
                seen.add(preview);

                results.push({
                    text: text.substring(0, 100000),
                    role: role === 'user' ? '用户' : 'AI'
                });
            }

            // ── 回退：data-item-type ──
            if (results.length === 0) {
                const itemEls = chat.querySelectorAll('[data-item-type]');
                const seen2 = new Set();
                for (const el of itemEls) {
                    const itemType = el.getAttribute('data-item-type') || '';
                    if (!itemType.startsWith('turn:')) continue;
                    let c = el;
                    while (c && !c.getAttribute('data-message-id')) c = c.parentElement;
                    const dedupKey = c ? c.getAttribute('data-message-id')
                        : itemType + '_' + Math.random();
                    if (seen2.has(dedupKey)) continue;
                    seen2.add(dedupKey);
                    const role = itemType.includes('user') ? '用户'
                        : (itemType.includes('assistant') || itemType.includes('agent'))
                            ? 'AI' : null;
                    if (!role) continue;
                    const text = extractRawText(c || el);
                    if (text.length < 5) continue;
                    const preview = text.substring(0, 150);
                    if (seen.has(preview)) continue;
                    seen.add(preview);
                    results.push({ text: text.substring(0, 100000), role });
                }
            }

            // ── 回退：class 名称 ──
            if (results.length === 0) {
                const turnEls = chat.querySelectorAll('[class*="turn__"]');
                const seen3 = new Set();
                for (const turnEl of turnEls) {
                    const cls = (turnEl.className || '').toLowerCase();
                    const role = cls.includes('user') ? '用户'
                        : (cls.includes('agent') || cls.includes('assistant'))
                            ? 'AI' : null;
                    if (!role) continue;
                    const mcList = turnEl.querySelectorAll('[data-message-id]');
                    if (mcList.length > 0) {
                        for (const mc of mcList) {
                            const msgId = mc.getAttribute('data-message-id');
                            if (seen3.has(msgId)) continue;
                            seen3.add(msgId);
                            const mcRole = mc.getAttribute('data-role');
                            const finalRole = mcRole === 'user' ? '用户'
                                : mcRole === 'assistant' ? 'AI' : role;
                            const text = extractRawText(mc);
                            if (text.length < 5) continue;
                            const preview = text.substring(0, 150);
                            if (seen.has(preview)) continue;
                            seen.add(preview);
                            results.push({ text: text.substring(0, 100000),
                                           role: finalRole });
                        }
                    }
                    if (mcList.length === 0) {
                        const dedupKey = turnEl.getAttribute('data-turn-id')
                            || turnEl.className;
                        if (seen3.has(dedupKey)) continue;
                        seen3.add(dedupKey);
                        const text = extractRawText(turnEl);
                        if (text.length < 5) continue;
                        const preview = text.substring(0, 150);
                        if (seen.has(preview)) continue;
                        seen.add(preview);
                        results.push({ text: text.substring(0, 100000), role });
                    }
                }
            }

            return JSON.stringify(results);
        })()
        """
    )
    if not r or r == "NO_CHAT":
        return []
    try:
        return json.loads(r)
    except Exception:
        return []


# ── 后处理：用交替模式修正孤立的"未知" ────────────────────────────
def fix_roles_by_alternating(messages):
    """
    聊天中消息通常 user→AI→user→AI 交替。
    如果某条被误标或漏标，利用前后文修正。
    """
    if not messages:
        return messages

    # 第一遍：如果有任意一条"未知"，尝试从前后文推断
    for i, msg in enumerate(messages):
        if msg.get("role") != "未知":
            continue
        # 看前一条
        if i > 0 and messages[i - 1].get("role") not in ("未知", None):
            prev = messages[i - 1]["role"]
            msg["role"] = "用户" if prev == "AI" else "AI"
        # 看后一条
        elif i < len(messages) - 1 and messages[i + 1].get("role") not in ("未知", None):
            nxt = messages[i + 1]["role"]
            msg["role"] = "用户" if nxt == "AI" else "AI"

    # 第二遍：如果还有未知，假设第一条是用户（对话由用户发起）
    for msg in messages:
        if msg.get("role") == "未知":
            msg["role"] = "用户"

    return messages


# ── 自动保存 checkpoint ──────────────────────────────────────────
def save_checkpoint(messages, reason=""):
    """将当前累计的消息写入 checkpoint 文件（不中断爬取）"""
    try:
        user_cnt = sum(1 for m in messages if m.get("role") == "用户")
        ai_cnt = sum(1 for m in messages if m.get("role") == "AI")

        content_lines = format_output(messages)
        output = [
            "# TRAE 聊天记录 (自动保存)\n",
            f"保存时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n",
            f"原因: {reason or '自动 checkpoint'}\n",
            f"当前条数: {len(messages)}（用户: {user_cnt}, AI: {ai_cnt}）\n",
            "---\n",
        ]
        output.extend(content_lines)

        with open(CHECKPOINT_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(output))
        print(
            f"    [checkpoint] 已保存 {len(messages)} 条到"
            f" trae_chat_checkpoint.md ({reason})"
        )
    except Exception as e:
        print(f"    [checkpoint] 保存失败: {e}")


# ── 爬取主循环 ────────────────────────────────────────────────────
async def crawl_current_chat(cdp):
    print("  （请确保已手动滚动到对话最上方，等历史加载完成）")
    print("  （随时按 CTRL+Q 停止爬取）")
    print("  （每 6 轮无新内容自动保存 checkpoint 并继续）\n")
    await asyncio.sleep(1)

    all_messages = []
    seen_hashes = set()
    consecutive_empty = 0
    consecutive_cdp_fail = 0     # 连续 CDP 调用失败计数
    SAVE_INTERVAL = 6            # 多少轮无新内容触发自动保存
    MAX_CDP_FAILS = 30           # 连续 CDP 失败多少次强制退出

    print("  开始向下滚动提取...")

    round_num = 0
    while not stop_flag.is_set():
        round_num += 1

        # ── 每条循环前先检查 stop_flag ──
        if stop_flag.is_set():
            break

        messages = await extract_messages(cdp)

        # extract_messages 返回空（可能是超时或连接问题）
        if messages is None:
            consecutive_cdp_fail += 1
            if consecutive_cdp_fail >= MAX_CDP_FAILS:
                print(
                    f"\n    [安全退出] 连续 {MAX_CDP_FAILS} 次 CDP 通信失败，"
                    f"TRAE 可能已关闭或连接断开"
                )
                save_checkpoint(all_messages, reason="CDP 断开自动保存")
                break
            await asyncio.sleep(0.5)
            continue
        else:
            consecutive_cdp_fail = 0

        new_count = 0
        for msg in messages:
            h = msg["text"][:150]
            if h not in seen_hashes:
                seen_hashes.add(h)
                all_messages.append(msg)
                new_count += 1

        if new_count == 0:
            consecutive_empty += 1
        else:
            consecutive_empty = 0

        # ── 每 SAVE_INTERVAL 轮无新内容 → 自动保存 checkpoint ──
        if consecutive_empty > 0 and consecutive_empty % SAVE_INTERVAL == 0:
            save_checkpoint(
                all_messages,
                reason=f"连续 {consecutive_empty} 轮无新内容",
            )

        # 滚动
        scroll_result = await cdp.eval(
            "(() => { const s = document.querySelector("
            "'.virtualized-message-list-view__scroller'); "
            "if (s) { s.scrollTop += 200; return s.scrollTop; } "
            "return null; })()"
        )
        if scroll_result is None:
            # 滚动超时不致命，但记一次失败
            consecutive_cdp_fail += 1

        await asyncio.sleep(1.0)

        if round_num % 50 == 0:
            user_cnt = sum(1 for m in all_messages if m.get("role") == "用户")
            ai_cnt = sum(1 for m in all_messages if m.get("role") == "AI")
            unknown_cnt = sum(1 for m in all_messages if m.get("role") == "未知")
            print(
                f"    [轮次 {round_num}] 累计 {len(all_messages)} 条 "
                f"(用户:{user_cnt}  AI:{ai_cnt}  未知:{unknown_cnt})"
                f"  空轮:{consecutive_empty}"
            )

    # ── 爬取结束后修正角色 ──
    all_messages = fix_roles_by_alternating(all_messages)

    print(f"\n  停止，共提取 {len(all_messages)} 条消息")
    return all_messages


# ── 文本清洗 ──────────────────────────────────────────────────────
def clean_text(text, role):
    """清洗消息文本：去除噪音标签、操作摘要、时间戳等"""
    if role == 'AI':
        # 去掉 "TRAE Work" 前缀
        text = re.sub(r'^TRAE\s*Work\s*', '', text)
        # 去掉 "任务耗时 Xs" / "任务耗时 Xm Xs"
        text = re.sub(r'任务耗时\s*\d+\s*[sm](?:\s*\d+\s*[sm])?\s*', '', text)
        # 去掉操作摘要行：含/不含"已"前缀
        #   "已读取 X 个文件" / "读取 X 个文件" / "已创建 X 个文件" / "已执行 X 条命令" / "已编辑 X 个文件" / "已搜索 X 次" / "已删除 X 个文件"
        text = re.sub(
            r'(?:已)?(?:读取|创建|执行|编辑|搜索|删除|更改)\s*\d+\s*(?:个|条|次)\s*(?:文件|命令)?[、，,\s]*',
            '', text)
        # 去掉孤立的"思考过程"/"思考中…"/"思考"标签
        text = re.sub(r'思考过程\s*', '', text)
        text = re.sub(r'思考中[.…]*\s*', '', text)
        text = re.sub(r'(?<!\w)思考(?=[A-Z])', '', text)  # "思考Let"→"Let"(仅英文前)
        # 去掉孤立的 Plan / Todo 标签行
        text = re.sub(r'^\s*Plan\s*$', '', text, flags=re.MULTILINE)
        text = re.sub(r'^\s*Todo\s*$', '', text, flags=re.MULTILINE)
    else:
        # 用户消息：去掉尾部时间戳 "22:47" 等 + 文件引用
        text = re.sub(r'\d{2}:\d{2}$', '', text)
        text = re.sub(r'^[\w.-]+\.py(?:PY)?\s*', '', text)

    # 通用清理
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = text.strip()
    text = re.sub(r'^[、，,\s]+', '', text)
    text = re.sub(r'[、，,\s]+$', '', text)

    return text


# ── 输出格式化 ────────────────────────────────────────────────────
def format_output(messages):
    lines = []
    for i, msg in enumerate(messages, 1):
        text = clean_text(msg["text"], msg.get("role", "未知"))
        if not text or len(text) < 5:
            continue
        role_tag = msg.get("role", "未知")
        lines.append(f"{i}. 【{role_tag}】\n{text}\n\n")
    return lines


# ── 入口 ───────────────────────────────────────────────────────────
async def main():
    print("=== TRAE CDP 聊天记录爬虫 v8（自动保存 · 高可靠终止）===\n")

    try:
        targets = json.loads(
            urllib.request.urlopen(CDP_HTTP, timeout=5).read()
        )
    except Exception as e:
        print(f"错误: 无法连接到 {CDP_HTTP}")
        print("请确认 TRAE 已用 --remote-debugging-port=9222 启动")
        print(f"详情: {e}")
        return

    ws_url = None
    for t in targets:
        if t.get("type") == "page" and "solo" in t.get("url", ""):
            ws_url = t["webSocketDebuggerUrl"]
            break
    if not ws_url:
        print("错误: 在 CDP targets 中找不到 TRAE solo 页面")
        print(f"可用 targets: {[t.get('title', '?') for t in targets]}")
        return

    async with websockets.connect(ws_url, max_size=50 * 1024 * 1024) as ws:
        cdp = CDP(ws)
        title = await cdp.eval("document.title")
        print(f"已连接: {title}\n")

        chat_len = await cdp.eval(
            "document.querySelector('.ai-chat')?.textContent.length || 0"
        )
        try:
            chat_len = int(chat_len) if chat_len else 0
        except Exception:
            chat_len = 0

        if chat_len < 30:
            print("当前窗口没有聊天内容。请在 TRAE 中打开一个对话后再运行。")
            return

        print(f"当前对话文本长度约: {chat_len} 字符")
        print("开始爬取...\n")

        messages = await crawl_current_chat(cdp)

        # 统计
        user_cnt = sum(1 for m in messages if m.get("role") == "用户")
        ai_cnt = sum(1 for m in messages if m.get("role") == "AI")
        unknown_cnt = sum(1 for m in messages if m.get("role") == "未知")

        # 写入
        content_lines = format_output(messages)

        output = [
            "# TRAE 聊天记录\n",
            f"导出时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n",
            f"总条数: {len(messages)}（用户: {user_cnt}, AI: {ai_cnt}, 未知: {unknown_cnt}）\n",
            "---\n",
        ]
        output.extend(content_lines)

        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(output))

        file_size = os.path.getsize(OUTPUT_FILE)
        print(f"\n完成!")
        print(f"输出: {OUTPUT_FILE}")
        print(f"大小: {file_size:,} bytes")
        print(
            f"统计: 总计 {len(messages)} 条 "
            f"（用户: {user_cnt}, AI: {ai_cnt}, 未知: {unknown_cnt}）"
        )


if __name__ == "__main__":
    asyncio.run(main())
