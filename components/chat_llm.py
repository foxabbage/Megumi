# components/chat_llm.py
import asyncio
import logging
import sys
from collections import deque
from typing import Optional, List, Dict, Any, Deque
import json5

# DashScope SDK
from dashscope import MultiModalConversation

from config import TOP_PATH, DASHSCOPE_API_KEY, CHAT_MODEL, USER_NAME, AI_NAME, BASE_PROMPT
sys.path.append(TOP_PATH)

from core.protocol import Message, ComponentID, MessageType
from components.base import VTuberComponent

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger("ChatLLM")

class ChatLLMComponent(VTuberComponent):
    """
    AI VTuber ChatLLM 组件
    支持三种模式:
      - chat_live: 直播互动模式(语音+弹幕+截图)
      - danmaku_chat: 纯弹幕聊天模式(带主题)
      - game_video: 游戏/视频自动解说模式
    """
    MAX_CONTEXT_TURNS = 10          # 保留20轮对话历史
    MAX_DANMAKU_BUFFER = 5         # 弹幕缓冲区大小
    MAX_SCREEN = 4
    AUTO_REPLY_INTERVAL = 6.0      # 模式三自动回复间隔(秒)
    TTS_PAUSE_AFTER_REPLY = 2.0     # TTS播放后等待时间(秒)
    
    def __init__(
        self, 
        mode: str = "chat_live", 
        theme: str = "", 
        play_name: str = "",
        enable_search: bool = False, 
        core_url: str = "ws://localhost:8025/ws/",
        specific_mode: str = ""
    ):
        super().__init__(ComponentID.CHAT_LLM, core_url)
        
        # 模式配置
        self.mode = mode
        self.specific_mode = specific_mode # video/game, used to distinguish game_video
        self.theme = theme
        self.play_name = play_name
        self.enable_search = enable_search
        
        # 上下文管理: deque自动维护最大长度
        self.context: Deque[Dict[str, str]] = deque(maxlen=self.MAX_CONTEXT_TURNS * 2)
        
        # 运行时状态
        self._pending_trace_id: Optional[str] = None
        self._current_input_text: str = ""
        self._current_image_path: Deque[str] = deque(maxlen=self.MAX_SCREEN)
        self._danmaku_buffer: Deque[str] = deque(maxlen=self.MAX_DANMAKU_BUFFER)
        self._tts_busy: bool = False
        self._last_reply_time: float = 0
        self._auto_reply_interval: float = self.AUTO_REPLY_INTERVAL
        self._auto_reply_task: Optional[asyncio.Task] = None
        self.is_speaking = False
        self.operate = False
        
        # 注册消息处理器
        self._register_handlers()
        
        # 初始化 DashScope
        self._init_dashscope()
    
    def _init_dashscope(self):
        """初始化 DashScope API"""
        import dashscope
        dashscope.api_key = DASHSCOPE_API_KEY
        logger.info("DashScope initialized")
    
    def _register_handlers(self):
        """注册消息处理器"""
        # 通用处理器
        self.register_handler(MessageType.COMMAND, self._handle_command)
        self.register_handler(MessageType.RESPONSE, self._handle_response)
        self.register_handler(MessageType.ERROR, self._handle_error)
        
        # 模式特定处理器
        mode_handlers = {
            "chat_live": self._handle_live_input,
            "danmaku_chat": self._handle_danmaku_input,
            "game_video": self._handle_game_input
        }
        if self.mode in mode_handlers:
            self.register_handler(MessageType.STREAM_DATA, mode_handlers[self.mode])
        
        # 模式三：启动自动回复任务
        if self.mode == "game_video":
            self._auto_reply_task = asyncio.create_task(self._auto_reply_loop())
    
    # ==================== 消息处理器 ====================
    
    async def _handle_command(self, msg: Message):
        """处理Core下发的控制命令"""
        cmd = msg.payload.get("command", "")
        params = msg.payload.get("params", {})
        
        logger.info(f"Received command: {cmd}, params: {params}")
        
        if cmd == "set_search_enabled":
            self.enable_search = params.get("enabled", True)
            logger.info(f"✓ Search enabled: {self.enable_search}")
        elif cmd == "update_theme":
            self.theme = params.get("theme", self.theme)
            logger.info(f"✓ Theme updated: {self.theme}")
        elif cmd == "update_game":
            self.play_name = params.get("game", self.play_name)
            logger.info(f"✓ Game updated: {self.play_name}")
        elif cmd == "clear_context":
            self.context.clear()
            logger.info("✓ Context cleared")
        elif cmd == "set_auto_interval":
            self._auto_reply_interval = params.get("interval", self.AUTO_REPLY_INTERVAL)
            logger.info(f"✓ Auto reply interval: {self._auto_reply_interval}s")
    
    async def _handle_response(self, msg: Message):
        """处理Memory组件返回的检索结果"""
        if msg.source == ComponentID.MEMORY:
            texts = msg.payload.get("texts", [])
            memory_hint = "\n".join(texts) if texts else None
            await self._generate_reply(memory_hint=memory_hint)
        elif msg.source == ComponentID.TTS:
            self.is_speaking = False
        elif msg.source == ComponentID.PC_LLM:
            self.operate = False
            logger.info(f"pc_control:{msg.payload.get("operation_description", "")}")
    
    async def _handle_error(self, msg: Message):
        """处理错误消息"""
        logger.error(f"Error from {msg.source}: {msg.payload}")
    
    # ==================== 模式特定输入处理 ====================
    
    async def _handle_live_input(self, msg: Message):
        """模式一：直播互动输入处理（STT语音 + 弹幕 + 截图）"""
        payload = msg.payload
        
        # 处理语音转文字输入
        if "text" in payload and payload["text"].strip():
            user_text = payload["text"].strip()
            self._current_input_text = user_text
            self._pending_trace_id = msg.trace_id
            await self._query_memory(user_text)
        
        # 处理弹幕输入
        if "text_summary" in payload:
            danmaku = payload.get("text_summary", "").strip()
            if danmaku:
                self._danmaku_buffer.append(danmaku)
        
        # 处理截图输入
        if "image_path" in payload:
            img_path = payload["image_path"]
            self._current_image_path.append(img_path)
    
    async def _handle_danmaku_input(self, msg: Message):
        """模式二：弹幕聊天输入处理"""
        payload = msg.payload
        text = payload.get("text_summary", "").strip()
        
        if text:
            self._current_input_text = text
            self._pending_trace_id = msg.trace_id
            await self._query_memory(self._current_input_text)
    
    async def _handle_game_input(self, msg: Message):
        """模式三：游戏/视频模式输入处理（主要接收截图）"""
        payload = msg.payload
        
        if "image_path" in payload:
            img_path = payload["image_path"]
            self._current_image_path.append(img_path)
    
    # ==================== 核心业务逻辑 ====================
    
    async def _query_memory(self, query_text: str):
        """向Memory组件发起语义检索请求"""
        await self.send_message(
            target=ComponentID.MEMORY,
            msg_type=MessageType.QUERY,
            payload={"query": query_text},
            trace_id=self._pending_trace_id
        )
    
    async def _generate_reply(self, memory_hint: Optional[str] = None):
        """调用LLM生成回复并分发给各组件"""
        if self.is_speaking or self.operate:
            logger.info("other components busy")
            return
        try:
            messages = self._build_llm_messages(memory_hint)
            reply_text = await self._call_llm(messages)
            logger.info(reply_text)
            
            if not reply_text:
                logger.info("llm with no reply")
                return
            
            reply_text = json5.loads(reply_text)
            say_text = reply_text.get("say", "")
            if say_text:
                # 发送给TTS组件
                await self._send_to_tts(say_text)
                self.is_speaking = True
                await self._send_to_subtitle(say_text)
            
            annotation_text = reply_text.get("annotation", "")
            reply_text = ""
            if annotation_text:
                reply_text += f"[annotation]{annotation_text}\n"
            if say_text:
                reply_text += f"[say]{say_text}\n"
                
            # 模式三：同时发送给PC_CONTROL_LLM
            if self.mode == "game_video":
                await self._send_to_pc_control(reply_text)
                self.operate = True
            
            # 更新对话上下文
            if self._current_input_text.strip():
                self._update_context("user", self._current_input_text)
            self._update_context("assistant", reply_text)
            
            # 保存对话到Memory（仅文本）
            await self._save_to_memory(self._current_input_text, reply_text)
            
            # 可选：回传响应给请求方
            if self._pending_trace_id:
                await self.send_message(
                    target=ComponentID.CORE,
                    msg_type=MessageType.RESPONSE,
                    payload={"text": reply_text},
                    trace_id=self._pending_trace_id
                )
            
            # 清理临时状态
            self._current_input_text = ""
            self._current_image_path.clear()
            self._pending_trace_id = None
            self._danmaku_buffer.clear()
            
        except Exception as e:
            logger.error(f"Error in _generate_reply: {e}", exc_info=True)
    
    def _build_llm_messages(self, memory_hint: Optional[str] = None) -> List[Dict[str, Any]]:
        """构建发送给LLM的消息列表"""
        messages = []
        
        # System Prompt
        system_content = self._build_system_prompt()
        
        messages.append({"role": "system", "content": system_content})
        
        # 添加历史上下文
        for msg in self.context:
            messages.append({"role": msg["role"], "content": msg["content"]})
        
        # 构建当前用户输入
        user_content = f"[{USER_NAME}]: " + self._current_input_text.strip()
        
        # + 弹幕内容（如有）
        if self._danmaku_buffer:
            danmaku_str = "\n".join(self._danmaku_buffer)
            user_content += f"\n\n[实时弹幕]:\n{danmaku_str}"
        if memory_hint:
            user_content += f"\n\n相关记忆参考:\n{memory_hint}"
        
        # + 图片内容（模式一&三支持）
        if self._current_image_path and self.mode in ["chat_live", "game_video"]:
            image_content = self._construct_image_content()
            if image_content:
                # Qwen多模态格式
                image_content.append({"text": user_content})
                user_content = image_content
        
        messages.append({"role": "user", "content": user_content})
        return messages
    
    def _build_system_prompt(self) -> str:
        """构建 System Prompt"""
        # 1. 核心人设定义 (Persona)
        base = BASE_PROMPT

        # 2. 场景模式适配 (Mode Adaptation)
        if self.mode == "chat_live":
            base += f"""
# 当前场景：直播互动中
- 请配合{USER_NAME}节奏，把观众当成“家人”或“小伙伴”。
- 如果冷场，主动抛出一个关于游戏或动漫的轻松话题。
- 偶尔调皮一下，比如假装没听到某个问题，或者故意曲解观众的意思开玩笑。
"""
        elif self.mode == "danmaku_chat":
            theme = self.theme or "轻松闲聊"
            base += f"""
# 当前场景：主题闲聊
- 当前主题：「{theme}」
- 请围绕主题展开有趣内容，但可以随时联想到宅文化（游戏/漫画/动画）。
- 如果话题太严肃，尝试用呆萌的方式化解气氛。
- 多提问，引导观众继续聊天（如：你们觉得呢？）。
- 额外事项：如果{USER_NAME}在场（直播互动时），你应该优先和{USER_NAME}互动。你应该把{USER_NAME}当作特殊意义的好朋友互动，而不应该提及开发者这件事。在看番/游戏/闲聊时，你应当关注自身想法。
"""
        elif self.mode == "game_video" and self.specific_mode == "game":
            game = self.play_name or "游戏"
            base += f"""
需要操作时可以具体说出现在想要干什么，比如：先打开浏览器/今天我们玩{game}吧，具体操作会由另一个智能体完成。
# 当前场景：正在玩/观看「{game}」
游戏时：
- 请根据画面内容做出自然反应（惊讶、兴奋、懊恼）。
- 操作失误时要可爱地找借口（如：刚刚是键盘先动的手！）。
- 高光时刻要自信吐槽（如：这波操作帅不帅！快夸我！）。
- 保持解说节奏，避免过于频繁发言，给观众留出反应时间。
"""
        elif self.mode == "game_video" and self.specific_mode=="video":
            game = self.play_name or "视频"
            base += f"""
今天自己在浏览器上使用bilibili看番剧{game}
需要操作时可以具体说出现在想要干什么，比如：先打开浏览器/今天我们看番{game}吧，具体操作会由另一个智能体完成。
# 当前场景：正在玩/观看「{game}」
看番/视频时：
- 你必须使用json结构化输出，包含两个键'annotation'和'say'，其值均为str类型
- annotation记录你看到的画面描述，事实等，say则是你作为虚拟主播需要说的话，需要符合之前的提示词。
- 如果发现操作没有成功，可以通过发言再次尝试。
- 你应该自己独立的评论，完成操作，不应该提及{USER_NAME}的事情或名字。
- 操作都是由你自己完成的。
- {game}是一部番剧
重要提示：
- 当在番剧或视频播放时，你应该认真观看内容，减少直接发言，输出时'say':''，即say部分输出空字符串，并且吐槽频率必须小于每三轮对话吐槽一次。
- 当不在播放番剧，你应该作为虚拟主播的身份，活跃气氛以及具体说出你需要做的事情，'say'部分可以增长一些。
- 输出严格遵循json结构体输出


例子1(正在播放番剧)：
{{'annotation':'智乃在做咖啡拉花，心爱在旁边帮忙', 'say':''}}

针对例子1的说明：由于正在播放番剧，你只能使用'annotation'描述画面，不能进行'say'，除非你有特别需要吐槽或者评论的事情。

例子2(正在播放番剧)：
{{'annotation':'心爱提出要帮智乃做咖啡，说“全部交给姐姐我吧！”，智乃对心爱感到有些无奈', 'say':'能不能有个心爱这样的姐姐？羡慕'}}

针对例子2的说明：你可以在观看番剧时，在值得评价和吐槽的地方使用'say'。

例子3：
{{'annotation':'', 'say':'最近有新百合番{game}了\n那今天我们一起来看怎么样？\n让我先打开浏览器和B站'}}

针对例子3的说明：不在观看番剧，请通过发言操作和活跃气氛。此处通过"让我先打开浏览器和B站"描述了需要做的操作，同时也活跃了气氛。

重要事项：今天的所有操作需要通过你自己发言完成，同时你也要作为虚拟主播进行评论等活动。
现在根据屏幕和今天的任务进行发言或思考，并且只输出单纯的json结构体：
"""
        
        if self.enable_search:
            base += "\n已启用联网搜索：遇到时事、新闻、数据类问题请主动搜索最新信息。"
        
        return base
    
    async def _call_llm(self, messages: List[Dict[str, Any]]) -> str|None:
        """调用 Qwen3.5-Flash 模型（DashScope）"""
        try:
            response = await asyncio.to_thread(
                MultiModalConversation.call,
                model=CHAT_MODEL,
                messages=messages,
                temperature=0.7,
                stream=False,
                enable_thinking=False
            )
            
            if response.status_code == 200 and response.output.choices:
                return response.output.choices[0].message.content[0]["text"].strip()
            else:
                logger.error(f"❌ DashScope error: {response.code} - {response.message}")
                return None
                
        except Exception as e:
            logger.error(f"❌ Call Qwen error: {e}", exc_info=True)
            return None
    
    def _construct_image_content(self):
        if self._current_image_path:
            return [{"image": f"file://{image_path}"} for image_path in self._current_image_path]
    
    def _update_context(self, role: str, content: str):
        """更新对话上下文（自动维护最大轮数）"""
        if content and content.strip():
            self.context.append({"role": role, "content": content.strip()})
    
    async def _save_to_memory(self, user_text: str, assistant_text: str):
        """将对话文本保存到Memory组件（仅文本）"""
        memory_entry = [{"role":"user", "content":user_text}, {"role":"assistant", "content":assistant_text}]
        await self.send_message(
            target=ComponentID.MEMORY,
            msg_type=MessageType.STREAM_DATA,
            payload={"text": memory_entry, "type": "dialogue"}
        )
    
    async def _send_to_tts(self, text: str):
        """发送文本到TTS组件"""
        await self.send_message(
            target=ComponentID.TTS,
            msg_type=MessageType.TEXT_MESSAGE,
            payload={"text": text}
        )
        
        # 模式二：等待TTS播放完成再继续
        if self.mode == "danmaku_chat":
            await asyncio.sleep(self.TTS_PAUSE_AFTER_REPLY)
    
    async def _send_to_subtitle(self, text: str):
        """发送文本到Subtitle组件"""
        await self.send_message(
            target=ComponentID.SUBTITLE,
            msg_type=MessageType.TEXT_MESSAGE,
            payload={"text": text}
        )
        
        # 模式二：等待TTS播放完成再继续
        if self.mode == "danmaku_chat":
            await asyncio.sleep(self.TTS_PAUSE_AFTER_REPLY)
    
    async def _send_to_pc_control(self, text: str):
        """模式三：发送回复到PC_CONTROL_LLM"""
        
        payload = {
            "text": text,
            "game_context": self.play_name
        }
        
        await self.send_message(
            target=ComponentID.PC_LLM,
            msg_type=MessageType.TEXT_MESSAGE,
            payload=payload
        )
    
    # ==================== 模式三：自动回复循环 ====================
    
    async def _auto_reply_loop(self):
        """模式三：定时触发自动回复"""
        while self.is_running and self.mode == "game_video":
            try:
                await asyncio.sleep(self.AUTO_REPLY_INTERVAL)
                
                # 无待处理输入时，主动生成游戏解说
                prompt = f"[🎮 {self.play_name} 进展] 请对当前画面/游戏状态做出自然解说"
                self._current_input_text = prompt
                
                # 有截图则带图，无截图则纯文本
                if self._current_image_path:
                    await self._generate_reply()
                else:
                    # 无图时直接生成（不查记忆避免空查）
                    await self._generate_reply(memory_hint=None)
                        
            except asyncio.CancelledError:
                logger.info("🔄 Auto reply loop cancelled")
                break
            except Exception as e:
                logger.error(f"❌ Auto reply loop error: {e}")
                await asyncio.sleep(6)
    
    # ==================== 生命周期管理 ====================
    
    async def start(self):
        """启动组件"""
        logger.info(f"🚀 ChatLLM Component starting | Mode: [{self.mode}] | Search: {self.enable_search}")
        if self.theme:
            logger.info(f"   Theme: {self.theme}")
        if self.play_name:
            logger.info(f"   Game: {self.play_name}")
        await super().start()
    
    def stop(self):
        """停止组件"""
        self.is_running = False
        if self._auto_reply_task and not self._auto_reply_task.done():
            self._auto_reply_task.cancel()
        logger.info("🛑 ChatLLM Component stopped")


# ==================== 启动函数（供外部调用） ====================

async def run_chat_live(theme: str = "", enable_search: bool = False, core_url: str = None):
    """模式一：直播聊天启动入口"""
    comp = ChatLLMComponent(
        mode="chat_live",
        theme=theme,
        enable_search=enable_search,
        core_url=core_url or "ws://localhost:8025/ws/"
    )
    try:
        await comp.start()
    except KeyboardInterrupt:
        comp.stop()

async def run_danmaku_chat(theme: str = "轻松闲聊", enable_search: bool = True, core_url: str = None):
    """模式二：弹幕聊天启动入口"""
    comp = ChatLLMComponent(
        mode="danmaku_chat",
        theme=theme,
        enable_search=enable_search,
        core_url=core_url or "ws://localhost:8025/ws/"
    )
    try:
        await comp.start()
    except KeyboardInterrupt:
        comp.stop()

async def run_game_video(
    play_name: str = "一叠间漫画咖啡屋生活", 
    enable_search: bool = False, 
    core_url: str = None,
    specific: str = "video"
):
    """模式三：游戏/视频模式启动入口"""
    comp = ChatLLMComponent(
        mode="game_video",
        play_name=play_name,
        enable_search=enable_search,
        core_url=core_url or "ws://localhost:8025/ws/",
        specific_mode=specific
    )
    try:
        await comp.start()
    except KeyboardInterrupt:
        comp.stop()

if __name__ == "__main__":
    asyncio.run(run_game_video())