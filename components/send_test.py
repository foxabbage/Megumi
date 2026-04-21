# components/subtitle_test_sender.py
import asyncio
import logging
from typing import List
import sys
from config import TOP_PATH
sys.path.append(TOP_PATH)
from components.base import VTuberComponent
from core.protocol import ComponentID, MessageType, Message

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("SubtitleTestSender")

class SubtitleTestSender(VTuberComponent):
    """测试组件：持续向 subtitle 组件发送文本消息"""
    
    def __init__(
        self, 
        messages: List[str] = None, 
        interval: float = 2.0,
        core_url: str = "ws://localhost:8025/ws/"
    ):
        super().__init__(ComponentID.CHAT_LLM, core_url)
        self.messages = messages or [
            "测试字幕 1：你好，世界！",
            "测试字幕 2：这是一个自动发送的测试消息~",
            "测试字幕 3：The quick brown fox jumps over the lazy dog.",
            "测试字幕 4：🎉 表情符号测试 ✨",
        ]
        self.interval = interval
        self._send_task: asyncio.Task | None = None
        self._msg_index = 0

    async def _send_loop(self):
        """持续发送消息的协程"""
        while self.is_running:
            try:
                text = self.messages[self._msg_index % len(self.messages)]
                await self.send_message(
                    target=ComponentID.SUBTITLE,
                    msg_type=MessageType.TEXT_MESSAGE,  # 根据实际协议调整
                    payload={"text": text, "source": "test_sender"}
                )
                logger.info(f"Sent: {text}")
                
                self._msg_index += 1
                await asyncio.sleep(self.interval)
                
            except asyncio.CancelledError:
                logger.debug("Send loop cancelled")
                break
            except Exception as e:
                if self.is_running:
                    logger.error(f"Send error: {e}")
                break

    async def start(self):
        """重写 start：启动发送任务"""
        
        try:
            # 先启动发送任务
            self._send_task = asyncio.create_task(self._send_loop())
            # 再启动连接（connect 内部会运行事件循环）
            await super().start()
        finally:
            # 确保发送任务被取消
            if self._send_task and not self._send_task.done():
                self._send_task.cancel()
                try:
                    await self._send_task
                except asyncio.CancelledError:
                    pass

    async def _safe_cleanup(self):
        """确保清理发送任务"""
        if self._cleanup_done:
            return
        # 先取消发送任务
        if self._send_task and not self._send_task.done():
            self._send_task.cancel()
            try:
                await self._send_task
            except asyncio.CancelledError:
                pass
        # 再执行父类清理
        await super()._safe_cleanup()


# ============ 快速启动入口 ============
if __name__ == "__main__":
    import sys
    from config import TOP_PATH
    sys.path.append(TOP_PATH)
    
    # 自定义测试消息（可选）
    test_msgs = [
        f"🧪 压力测试 #{i}: " + "Lorem ipsum " * (i+1)
        for i in range(5)
    ]
    
    sender = SubtitleTestSender(
        messages=test_msgs,
        interval=4,  # 每 1.5 秒发送一条
        core_url="ws://localhost:8025/ws/"
    )
    
    try:
        asyncio.run(sender.start())
    except KeyboardInterrupt:
        logger.info("Manual shutdown")
    finally:
        sender.stop()