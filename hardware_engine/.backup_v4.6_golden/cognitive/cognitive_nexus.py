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
