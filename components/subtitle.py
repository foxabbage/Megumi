# components/subtitle/component.py
import asyncio
import sys
from typing import Optional
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QObject, Signal, Qt, Slot
import signal
from qasync import QEventLoop

from config import TOP_PATH
sys.path.append(TOP_PATH)

from components.base import VTuberComponent
from core.protocol import ComponentID, MessageType, Message
from utils.subtitle_window import SubtitleWindow

import logging
logger = logging.getLogger("SubtitleComponent")


class SubtitleController(QObject):
    """Qt 控制器：所有 Qt 操作在主线程执行"""
    request_show_subtitle = Signal(str, int)
    request_clear_subtitle = Signal()
    request_close_window = Signal()
    
    window_ready = Signal()
    window_closed = Signal()
    
    def __init__(
        self,
        screen_idx: int,
        always_on_top: bool,
        click_through: bool,
        default_duration: int = 4000
    ):
        super().__init__()
        self.window: Optional[SubtitleWindow] = None
        self.default_duration = default_duration
        self._window_params = {
            "screen_idx": screen_idx,
            "always_on_top": always_on_top,
            "click_through": click_through
        }
        self._ready = False
        
        self.request_show_subtitle.connect(self._do_show_subtitle)
        self.request_clear_subtitle.connect(self._do_clear_subtitle)
        self.request_close_window.connect(self._do_close_window)
    
    def initialize(self):
        """初始化窗口（必须在 QApplication 创建后调用）"""
        if self.window is None:
            self.window = SubtitleWindow(**self._window_params)
            self.window.show()
            self._ready = True
            self.window_ready.emit()
            logger.info("SubtitleWindow initialized")
    
    @Slot(str, int)
    def _do_show_subtitle(self, text: str, duration: int):
        if self.window and self._ready:
            self.window.clear_subtitle()
            self.window.show_subtitle(text, duration)
    
    @Slot()
    def _do_clear_subtitle(self):
        if self.window:
            self.window.clear_subtitle()
    
    @Slot()
    def _do_close_window(self):
        if self.window:
            self.window.close()
            self.window = None
            self._ready = False
            self.window_closed.emit()
    
    @property
    def is_ready(self) -> bool:
        return self._ready


class SubtitleComponent(VTuberComponent):
    """字幕组件：基于 QEventLoop 的 asyncio + Qt 无缝集成"""
    
    def __init__(
        self, 
        core_url: str = "ws://localhost:8025/ws/",
        screen_idx: int = 0,
        always_on_top: bool = True,
        click_through: bool = False,
        default_duration: int = 10000
    ):
        super().__init__(ComponentID("subtitle"), core_url)
        
        self.default_duration = default_duration
        self.controller: Optional[SubtitleController] = None
        
        self._init_params = {
            "screen_idx": screen_idx,
            "always_on_top": always_on_top,
            "click_through": click_through,
            "default_duration": default_duration
        }
        
        self.register_handler(MessageType.TEXT_MESSAGE, self._handle_text_message)
        self.register_handler(MessageType.COMMAND, self._handle_command)
        
    def _handle_text_message(self, msg: Message):
        """处理文本消息 - 信号转发，零阻塞"""
        if msg.source != ComponentID.CHAT_LLM:
            return
        text = msg.payload.get("text", "")
        if not text or not self.controller or not self.controller.is_ready:
            return
        # ✅ 信号机制保证线程安全 + 非阻塞
        self.controller.request_show_subtitle.emit(text, self.default_duration)
    
    def _handle_command(self, msg: Message):
        """处理控制命令"""
        if msg.source != ComponentID.CORE:
            return
        cmd = msg.payload.get("command") if isinstance(msg.payload, dict) else None
        if cmd == "clear" and self.controller:
            self.controller.request_clear_subtitle.emit()
        elif cmd == "update_style" and isinstance(msg.payload, dict):
            # 可扩展样式更新逻辑
            pass
    
    async def start(self):
        """启动组件"""
        logger.info("Starting SubtitleComponent...")
        self.controller = SubtitleController(**self._init_params)
        self.controller.initialize()
        # ✅ WebSocket 连接现在运行在 QEventLoop 中，不会阻塞 Qt
        await super().start()
        logger.info("SubtitleComponent started")
    
    def stop(self):
        """请求停止"""
        logger.info("Stopping SubtitleComponent...")
        super().stop()
        if self.controller:
            self.controller.request_close_window.emit()
            self.controller = None
        logger.info("SubtitleComponent stop requested")
    
    # 🎯 不再需要 process_qt_events()！QEventLoop 自动处理


# ============ 主入口：QAsyncio 集成 ============

def main():
    """主入口：QEventLoop 驱动 asyncio + Qt"""

    # 1. 创建 QApplication（必须在主线程）
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)

    # 2. 🔑 创建 QEventLoop 并设置为当前事件循环
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)

    # 3. 创建组件
    component = SubtitleComponent(
        core_url="ws://localhost:8025/ws/",
        screen_idx=0,
        always_on_top=True,
        click_through=False,
        default_duration=20000
    )
    def sigint_handler(*args):
        logger.info("Received SIGINT, shutting down...")
        # 使用 call_soon_threadsafe 确保在 Qt 线程中执行
        loop.call_soon_threadsafe(loop.stop)
    
    signal.signal(signal.SIGINT, sigint_handler)

    try:
        with loop:
            # 6. 调度异步启动（不再用 await）
            loop.create_task(component.start())
            # 7. 🔑 run_forever() 直接在顶层调用，同时驱动 Qt + asyncio
            #    不再用 asyncio.run()，不再用 await
            loop.run_forever()
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")
    finally:
        # 8. 优雅清理
        component.stop()
        loop.close()


if __name__ == "__main__":
    main()