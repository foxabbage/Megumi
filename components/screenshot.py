# components/screenshot.py
import asyncio
import os
import time
from pathlib import Path
from typing import Optional, Dict, Any
from PIL import Image
import mss

from config import TOP_PATH, SCREENSHOT_CACHE_PATH
import sys
sys.path.append(TOP_PATH)
from components.base import VTuberComponent, logger
from core.protocol import ComponentID, MessageType, Message


class ScreenshotComponent(VTuberComponent):
    """
    截图组件：负责定时截图和响应截图请求
    - 每秒自动截图并压缩，发送给 chat_llm 和 pc_control_llm
    - 响应 pc_control_llm 的截图请求
    - payload 中通过 capture_type 区分 "auto" / "request"
    """
    
    def __init__(self, 
                 core_url: str = "ws://localhost:8025/ws/",
                 mode = "chat_live",
                 cache_dir: Optional[str] = None,
                 compression_ratio: float = 1/3,    # 压缩比例: 0.1~1.0
                 capture_interval: float = 1.5,      # 自动截图间隔(秒)
                 quality: int = 80):                 # JPEG 压缩质量
        super().__init__(component_id=ComponentID.SCREENSHOT, core_url=core_url)
        
        # 缓存配置
        self.cache_dir = Path(cache_dir or SCREENSHOT_CACHE_PATH)
        self.compression_ratio = compression_ratio
        self.capture_interval = capture_interval
        self.quality = quality
        
        # 状态管理
        self.mode = mode
        self._current_image_path: Optional[str] = None
        self._sequence_num = 0
        self._auto_capture_task: Optional[asyncio.Task] = None
        
        # 注册消息处理器
        self.register_handler(MessageType.QUERY, self._handle_query)
    
    def _clear_cache(self):
        """启动时清空缓存目录"""
        if self.cache_dir.exists():
            for f in self.cache_dir.glob("*.jpg"):
                try:
                    f.unlink()
                except Exception as e:
                    logger.warning(f"Failed to delete {f}: {e}")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Cache directory cleared: {self.cache_dir}")
    
    def _generate_cache_path(self) -> str:
        """生成带时间戳和序列号的缓存路径"""
        self._sequence_num = (self._sequence_num + 1) % 10000
        timestamp = int(time.time() * 1000)
        filename = f"scr_{timestamp}_{self._sequence_num:04d}.jpg"
        return f"{SCREENSHOT_CACHE_PATH}/{filename}"
    
    def _capture_and_compress(self, compress=True) -> str:
        """
        执行截图并按比例压缩
        返回: 压缩后的图片缓存路径
        """
        with mss.mss() as sct:
            # 截取主显示器 (monitor[1])
            monitor = sct.monitors[1]
            screenshot = sct.grab(monitor)
            
            # BGRX -> RGB 转换
            img = Image.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")
            
            # 按比例缩放压缩
            if 0 < self.compression_ratio < 1.0 and compress:
                new_size = (
                    max(1, int(img.width * self.compression_ratio)),
                    max(1, int(img.height * self.compression_ratio))
                )
                img = img.resize(new_size, Image.Resampling.LANCZOS)
            else:
                new_size = (1024,640)
                img = img.resize(new_size, Image.Resampling.LANCZOS)
            
            # 保存为优化后的 JPEG
            cache_path = self._generate_cache_path()
            img.save(cache_path, format="JPEG", quality=self.quality, optimize=True)
            
            return cache_path, img.width, img.height
    
    def _build_screenshot_payload(self, image_path: str, capture_type: str, width: int, height: int) -> Dict[str, Any]:
        """构建截图消息的 payload"""
        # 尝试获取图片尺寸信息
        return {
            "image_path": image_path,
            "capture_type": capture_type,      # "auto" | "request"
            "timestamp": time.time(),
            "width": width,
            "height": height,
            "compression_ratio": self.compression_ratio
        }
    
    async def _send_screenshot(self, image_path: str, capture_type: str, width: int, height: int, mode: str, trace_id: Optional[str] = None):
        """向 chat_llm 和 pc_control_llm 广播截图消息"""
        payload = self._build_screenshot_payload(image_path, capture_type, width, height)
        
        if mode == "chat_live":
            t = [ComponentID.CHAT_LLM]
        else:
            t = [ComponentID.CHAT_LLM, ComponentID.PC_LLM]
        for target in t:
            await self.send_message(
                target=target,
                msg_type=MessageType.STREAM_DATA,
                payload=payload,
                trace_id=trace_id
            )
        logger.debug(f"Screenshot sent ({capture_type}): {image_path}")
    
    async def _respond_screenshot(self, image_path: str, capture_type: str, width: int, height: int, trace_id: Optional[str] = None):
        """向pc_control_llm 回传截图消息"""
        payload = self._build_screenshot_payload(image_path, capture_type, width, height)
        
        await self.send_message(
            target=ComponentID.PC_LLM,
            msg_type=MessageType.RESPONSE,
            payload=payload,
            trace_id=trace_id
        )
        logger.debug(f"Screenshot sent ({capture_type}): {image_path}")
    
    async def _auto_capture_loop(self):
        """自动截图任务：按设定间隔持续截图"""
        while self.is_running:
            try:
                # 执行截图
                image_path, width, height = self._capture_and_compress()
                self._current_image_path = image_path
                
                # 发送截图消息
                await self._send_screenshot(image_path, "auto", width, height, self.mode)

                await asyncio.sleep(self.capture_interval)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Auto capture failed: {e}")
                await asyncio.sleep(1)  # 失败后短暂等待
    
    async def _handle_query(self, msg: Message):
        """
        处理 COMMAND 类型消息
        响应 pc_control_llm 的截图请求
        """
        # 只处理来自 pc_control_llm 的请求
        if msg.source != ComponentID.PC_LLM:
            return
        
        payload = msg.payload or {}
        query = payload.get("query")
        
        if query != "screenshot":
            return
        
        logger.info(f"Received screenshot request from PC_LLM, trace_id: {msg.trace_id}")
        
        try:
            # 执行截图
            image_path, width, height = self._capture_and_compress(False)
            self._current_image_path = image_path
            
            # 发送截图数据（区分 request 类型）
            await self._respond_screenshot(image_path, "request", width, height, trace_id=msg.trace_id)
            
        except Exception as e:
            logger.error(f"Request screenshot failed: {e}")
            await self.send_message(
                target=ComponentID.PC_LLM,
                msg_type=MessageType.ERROR,
                payload={
                    "query": "screenshot",
                    "error": str(e),
                    "error_type": type(e).__name__
                },
                trace_id=msg.trace_id
            )
    
    async def start(self):
        """组件启动入口"""
        # 1. 清空缓存目录
        self._clear_cache()
        
        # 2. 启动自动截图任务
        self._auto_capture_task = asyncio.create_task(self._auto_capture_loop())
        logger.info("Screenshot component auto-capture started")
        
        # 3. 连接 Core 并进入消息监听循环
        await super().start()

    def stop(self):
        """停止组件"""
        super().stop()
        if self._auto_capture_task and not self._auto_capture_task.done():
            self._auto_capture_task.cancel()
        logger.info("Screenshot component stopping...")
    
    def get_current_image(self) -> Optional[str]:
        """获取最新截图路径（同步方法，供外部查询）"""
        return self._current_image_path

async def main():
    screenshot = ScreenshotComponent(mode="game_video")
    try:
        await screenshot.start()
    except KeyboardInterrupt:
        screenshot.stop()
        
if __name__ == "__main__":
    asyncio.run(main())