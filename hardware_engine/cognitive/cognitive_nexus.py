import os
import sys
import logging
import json

# 为了能在 streamer_app.py 或其他地方导入，添加环境变量或相对路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from persistence.db import FitnessDB

logging.basicConfig(level=logging.INFO, format='%(asctime)s - [NEXUS] - %(message)s')

class CognitiveNexus:
    """
    轻量级的历史数据认知中枢。
    负责从 SQLite 中拉取长线历史，并与当前瞬时状态糅合，供远端大脑(DeepSeek)提取。
    """
    def __init__(self):
        self.db = FitnessDB()
    
    def _fetch_history_context(self) -> str:
        """获取最近 7 天的记录摘要和今天详细的 session，组装成文本上下文"""
        try:
            self.db.connect()
            stats = self.db.get_range_stats(days=7)
            recent_sessions = self.db.get_recent_sessions(limit=5)
            self.db.close()

            if not stats and not recent_sessions:
                return "用户暂无包含历史运动数据的记录（如果是第一次使用，请多多鼓励）。"

            context = "【系统供给：用户最近7日历史运动摘要】\n"
            for st in stats:
                context += f"- 日期: {st.get('d')}, 训练: {st.get('session_count')}次, 达标: {st.get('total_good')}次, 违规: {st.get('total_failed')}次, 疲劳峰值: {st.get('total_fatigue'):.1f}\n"
            
            context += "\n【今日/最新近的训练短序列记录】\n"
            for sess in recent_sessions:
                ex = sess.get("exercise", "未知")
                good = sess.get("good_count", 0)
                failed = sess.get("failed_count", 0)
                context += f"- 动作: {ex}, 达标 {good}次, 违规 {failed}次\n"
            return context
        except Exception as e:
            logging.error(f"历史提取失败: {e}")
            return "（历史数据提取遇到技术问题，按当前数据分析即可）"

    def build_prompt_for_type(self, push_type: str, fsm_data: dict, custom_user_prompt: str = "") -> dict:
        """
        根据推送类型构建带历史记忆的完整 Prompt payloads。
        push_type: "plan" | "summary" | "reminder"
        返回: {"system": "...", "user": "..."}
        """
        good = fsm_data.get("good", 0)
        failed = fsm_data.get("failed", 0)
        fatigue = fsm_data.get("fatigue", 0)
        exercise = fsm_data.get("exercise", "squat")

        history_ctx = self._fetch_history_context()

        system_prompt = (
            "你是 IronBuddy 高级智能健身副驾，能够掌握长期记忆并提供具有深度的数据点评。\n"
            "不要讲套话，像一个冷峻且专业的硬派数据分析教练那样输出。\n"
            "输出语言干练，不需要 <think> 标签，且请使用 Markdown 排版！"
        )

        user_prompt_base = f"【当前瞬时数据】\n- 动作项: {exercise}\n- 单次好球: {good}\n- 单次犯规: {failed}\n- 当前实时疲劳池: {fatigue}/1500\n\n{history_ctx}\n\n"

        if push_type == "plan":
            user_prompt = user_prompt_base + "指令：综合以上所有的当前状态与它最近7天的表现流，为他制定一份极精简的【今明两天建议计划】。"
        elif push_type == "summary":
            user_prompt = user_prompt_base + "指令：请基于它的历史数据并对比今天本次的表现，发出一份【训练总结与定性点评报告表】。要有一针见血的评价。"
        elif push_type == "reminder":
            user_prompt = user_prompt_base + "指令：系统刚收到了重置或卡死超载的警告消息，请发送一次包含警钟色彩的【重置关怀提醒或安全警示】。"
        else:
            # 兼容任意/默认情况
            user_prompt = user_prompt_base + f"指令：请针对用户的以下指示，综合数据进行回答：{custom_user_prompt}"

        # 任何额外的自定义输入都可以再附加
        if custom_user_prompt and push_type in ["plan", "summary", "reminder"]:
            user_prompt += f"\n另外，用户特别叮嘱: {custom_user_prompt}"

        return {
            "system": system_prompt,
            "user": user_prompt
        }

    # ========== V4.7 后端常驻 Prompt 构造 ==========
    def _fetch_preference_context(self) -> str:
        """抽取 user_config 偏好，拼成教练可读的短文。"""
        try:
            self.db.connect()
            prefs = self.db.get_user_preferences()
            self.db.close()
        except Exception as e:
            logging.warning(f"偏好读取失败: {e}")
            prefs = {}
        if not prefs:
            return "（暂无已学习到的用户偏好，请按通用策略给方案）"
        # 剥掉前缀，变成教练更好解读的小字典
        short = {}
        for k, v in prefs.items():
            short_k = k.replace("user_preference.", "")
            short[short_k] = v
        lines = ["【用户长期偏好（数据库学习结果）】"]
        for k, v in short.items():
            lines.append(f"- {k}: {v}")
        return "\n".join(lines)

    def _fetch_yesterday_summary(self) -> str:
        """取昨日训练快照，若无则返回提示。"""
        try:
            from datetime import date, timedelta as _td
            y = (date.today() - _td(days=1)).strftime("%Y-%m-%d")
            self.db.connect()
            row = self.db.get_daily_summary(y)
            self.db.close()
            if not row:
                return f"（昨日 {y} 无训练记录）"
            return (
                f"【昨日 {y} 完成度】\n"
                f"- 训练场次: {row.get('session_count', 0)}\n"
                f"- 达标: {row.get('total_good', 0)} 次\n"
                f"- 违规: {row.get('total_failed', 0)} 次\n"
                f"- 疲劳峰值累计: {row.get('total_fatigue', 0):.1f}\n"
                f"- 最佳连击: {row.get('best_streak', 0)}"
            )
        except Exception as e:
            logging.warning(f"昨日摘要读取失败: {e}")
            return "（昨日数据读取失败）"

    def build_daily_plan_prompt(self) -> dict:
        """早 9 点：生成"早安 + 今日计划"。
        system_prompt 强调"专业、鼓励/严格根据偏好切换、给出具体动作次数"；
        user_prompt 拼昨日完成度 + 7 日趋势 + 长期偏好。
        返回 {"system": ..., "user": ...}。
        """
        yesterday = self._fetch_yesterday_summary()
        history = self._fetch_history_context()
        prefs = self._fetch_preference_context()

        system_prompt = (
            "你是 IronBuddy 后端常驻教练。现在是清晨 9 点，你需要给用户发送一条问候与今日训练计划。\n"
            "硬性要求：\n"
            "1) 开场用『早上好』或同义亲切招呼；\n"
            "2) 点名『该健身了』并明确告知是否基于昨日完成度做加码 / 减量；\n"
            "3) 必须给出【具体动作 + 组数 × 次数】，不要讲抽象概念；\n"
            "4) 教练口吻跟随用户偏好 coach_style 切换（鼓励/严格），没有偏好时默认『专业温和』；\n"
            "5) 语言干练，Markdown 排版，总长度 200 字以内，不要 <think> 标签。"
        )

        user_prompt = (
            f"{yesterday}\n\n{history}\n\n{prefs}\n\n"
            "指令：请综合【昨日完成度】【7 日趋势】【用户偏好】，"
            "生成一份今天的训练计划。"
            "若昨日完成量偏低，今日应温和鼓励并降低目标；"
            "若昨日超额完成，今日应给出进阶方案。"
            "输出需包含：① 早安问候一句；② 今日计划（动作名 + 组数 × 次数，至少 2 个动作）；"
            "③ 一句贴合偏好的激励语。"
        )

        return {"system": system_prompt, "user": user_prompt}

    def build_weekly_report_prompt(self) -> dict:
        """周日 20:00：生成周报 prompt，含 7 日 rep_events 聚合 + llm_log + 偏好。"""
        history = self._fetch_history_context()
        prefs = self._fetch_preference_context()
        # 7 日对话摘要，精简到每条 80 字
        try:
            self.db.connect()
            chats = self.db.get_recent_chats(days=7)
            self.db.close()
        except Exception:
            chats = []
        chat_lines = []
        for c in chats[:30]:
            p = (c.get("prompt") or "")[:60]
            r = (c.get("response") or "")[:80]
            chat_lines.append(f"- [{c.get('trigger')}] Q: {p} / A: {r}")
        chat_block = "\n".join(chat_lines) if chat_lines else "（近 7 日无对话日志）"

        system_prompt = (
            "你是 IronBuddy 后端常驻教练。现在是周日晚上 20 点，需要向用户发送一份【本周训练周报】。\n"
            "硬性要求：\n"
            "1) 标题必须包含『本周训练周报』；\n"
            "2) 必须列出【具体数据】：达标总数、违规总数、疲劳峰值、最佳连击、场次；\n"
            "3) 必须指出至少 1 处进步 + 1 处待改进（基于数据对比）；\n"
            "4) 根据偏好 coach_style 调整口吻；\n"
            "5) 末尾给出【下周目标】建议（具体数字）；\n"
            "6) 不要空话，Markdown 排版，500 字以内，不要 <think>。"
        )

        user_prompt = (
            f"{history}\n\n"
            f"【近 7 日对话摘要】\n{chat_block}\n\n"
            f"{prefs}\n\n"
            "指令：请基于以上数据与对话，生成本周训练周报。"
            "要求数据化、对比化、给出可执行的下周目标。"
        )

        return {"system": system_prompt, "user": user_prompt}

    def build_preference_learning_prompt(self) -> dict:
        """23:00 偏好学习：读当日 llm_log，要 LLM 以 JSON 提炼偏好。

        解析端约定的 JSON Schema：
          {
            "favorite_exercise": "...",
            "coach_style": "鼓励|严格|专业温和",
            "training_time": "morning|afternoon|evening",
            "insights": ["...短句..."]
          }
        """
        try:
            self.db.connect()
            chats = self.db.get_recent_chats(days=1)
            prefs = self.db.get_user_preferences()
            self.db.close()
        except Exception:
            chats, prefs = [], {}

        if not chats:
            chat_block = "（今日无对话日志）"
        else:
            lines = []
            for c in chats:
                p = (c.get("prompt") or "")[:80]
                r = (c.get("response") or "")[:120]
                lines.append(f"- [{c.get('ts')}|{c.get('trigger')}] Q: {p} / A: {r}")
            chat_block = "\n".join(lines)

        pref_block = "（此前无已学偏好）"
        if prefs:
            pref_block = "\n".join(f"- {k}: {v}" for k, v in prefs.items())

        system_prompt = (
            "你是 IronBuddy 偏好学习子系统。需要从今日对话中提炼用户偏好，"
            "**只能输出一个 JSON 对象**，不要任何解释文字、不要 Markdown、不要 <think>。\n"
            "JSON Schema:\n"
            "{\n"
            '  "favorite_exercise": "squat|bicep_curl|...（从对话里看他最常提的）",\n'
            '  "coach_style": "鼓励|严格|专业温和",\n'
            '  "training_time": "morning|afternoon|evening",\n'
            '  "insights": ["一句话描述1", "一句话描述2"]\n'
            "}\n"
            "字段若无线索可输出空字符串或空数组，但不得省略字段。"
        )

        user_prompt = (
            "【今日对话日志】\n" + chat_block + "\n\n"
            "【已有偏好（可能为空）】\n" + pref_block + "\n\n"
            "指令：严格以 JSON 输出，不要任何多余字符。"
        )

        return {"system": system_prompt, "user": user_prompt}
