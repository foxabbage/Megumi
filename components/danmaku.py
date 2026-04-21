# components/danmaku.py
import asyncio
import time
import logging
from typing import Optional, List, Dict, Any
from datetime import datetime
import sys
import websockets
from config import TOP_PATH
sys.path.append(TOP_PATH)

import blivedm
from blivedm.models import DanmakuMessage
from core.protocol import Message, ComponentID, MessageType
from components.base import VTuberComponent

logger = logging.getLogger("DanmakuComponent")


class DanmakuHandler(blivedm.BaseHandler):
    """blivedm 弹幕消息处理器，将弹幕转发给组件主类"""
    
    def __init__(self, callback: callable):
        self.callback = callback
    
    async def _on_danmaku(self, client: blivedm.BLiveClient, message: DanmakuMessage):
        """收到弹幕时的回调"""
        if self.callback:
            await self.callback(message)


class DanmakuComponent(VTuberComponent):
    """弹幕收集组件：监听直播间弹幕，定时打包发送给 chat_llm"""
    
    def __init__(
        self, 
        room_id: int, 
        uid: int = 0, 
        session: Optional[any] = None,
        core_url: str = "ws://localhost:8025/ws/",
        batch_interval: float = 3.0,  # 弹幕打包发送间隔（秒）
        max_batch_size: int = 20      # 单次最大弹幕数量
    ):
        super().__init__(ComponentID.DANMAKU, core_url)
        
        self.room_id = room_id
        self.uid = uid
        self.session = session
        
        # 弹幕收集配置
        self.batch_interval = batch_interval
        self.max_batch_size = max_batch_size
        
        # 弹幕缓冲区
        self.danmaku_buffer: List[Dict[str, Any]] = []
        self.buffer_lock = asyncio.Lock()
        
        # blivedm 客户端
        self.client: Optional[blivedm.BLiveClient] = None
        self.client_task: Optional[asyncio.Task] = None
        
        # 定时打包任务
        self.batch_task: Optional[asyncio.Task] = None
        
        # 注册消息处理器（可选：响应来自 core 的控制指令）
        self.register_handler(MessageType.COMMAND, self._handle_command)
        self.register_handler(MessageType.QUERY, self._handle_query)

    # ==================== 生命周期管理 ====================
    
    async def start(self):
        """启动组件：连接 WebSocket + 启动弹幕监听 + 启动定时打包"""
        logger.info(f"Starting DanmakuComponent for room {self.room_id}")
        
        # 1. 连接 Core WebSocket
        connect_task = asyncio.create_task(super().connect())
        
        # 2. 初始化 blivedm 客户端
        self.client = blivedm.BLiveClient(
            room_id=self.room_id,
            uid=self.uid,
            session=self.session,
            handler=DanmakuHandler(self._on_danmaku_received)
        )
        
        # 3. 启动弹幕监听（非阻塞）
        self.client_task = asyncio.create_task(self.client.start())
        
        # 4. 等待连接建立后启动定时打包任务
        await asyncio.sleep(1)  # 等待 WebSocket 连接
        if self.websocket:
            self.batch_task = asyncio.create_task(self._batch_send_loop())
        
        logger.info("DanmakuComponent started")
        return connect_task

    def stop(self):
        """停止组件：关闭所有任务"""
        logger.info("Stopping DanmakuComponent...")
        super().stop()
        
        if self.client:
            self.client.stop()
        if self.client_task and not self.client_task.done():
            self.client_task.cancel()
        if self.batch_task and not self.batch_task.done():
            self.batch_task.cancel()

    # ==================== 弹幕处理逻辑 ====================
    
    async def _on_danmaku_received(self, message: DanmakuMessage):
        """收到单条弹幕时的处理：加入缓冲区"""
        danmaku_item = {
            "uname": message.uname,
            "msg": message.msg,
            "uid": message.uid,
            "timestamp": time.time(),
            "medal_level": message.medal_level
        }
        
        async with self.buffer_lock:
            self.danmaku_buffer.append(danmaku_item)
            # 缓冲区溢出保护
            if len(self.danmaku_buffer) > self.max_batch_size * 2:
                self.danmaku_buffer = self.danmaku_buffer[-self.max_batch_size:]
        
        logger.debug(f"Danmaku received: [{message.uname}] {message.msg}")

    async def _batch_send_loop(self):
        """定时打包并发送弹幕批次"""
        while self.is_running:
            try:
                await asyncio.sleep(self.batch_interval)
                await self._flush_buffer()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Batch send error: {e}")
                await asyncio.sleep(1)

    async def _flush_buffer(self):
        """将缓冲区弹幕打包发送"""
        async with self.buffer_lock:
            if not self.danmaku_buffer:
                return
            
            # 取出并清空缓冲区
            batch = self.danmaku_buffer[:self.max_batch_size]
            self.danmaku_buffer = self.danmaku_buffer[self.max_batch_size:]
        
        # 构建 payload：弹幕打包为结构化数据
        payload = self._build_danmaku_payload(batch)
        
        # 发送给 chat_llm
        await self.send_message(
            target=ComponentID.CHAT_LLM,
            msg_type=MessageType.STREAM_DATA,
            payload=payload,
            trace_id=None  # 可根据需求生成 trace_id
        )
        
        logger.info(f"Sent {len(batch)} danmakus to CHAT_LLM")

    def _build_danmaku_payload(self, danmaku_list: List[Dict]) -> Dict[str, Any]:
        """构建弹幕批次消息的 payload"""
        # 方案1：简单拼接为字符串（按需求）
        text_batch = "\n".join([f"[{d['uname']}]: {d['msg']}" for d in danmaku_list])
        
        # 方案2：保留结构化数据（推荐，便于后续扩展）
        return {
            "count": len(danmaku_list),
            "text_summary": text_batch,           # 简易字符串形式
            "time_range": {
                "start": danmaku_list[0]["timestamp"],
                "end": danmaku_list[-1]["timestamp"]
            }
        }

    # ==================== 消息处理器 ====================
    
    async def _handle_command(self, msg: Message):
        """处理来自 Core 的控制指令"""
        cmd = msg.payload.get("command") if isinstance(msg.payload, dict) else None
        logger.info(f"Received command: {cmd}")
        
        if cmd == "pause_collect":
            self.batch_interval = float('inf')  # 暂停打包
        elif cmd == "resume_collect":
            self.batch_interval = 3.0  # 恢复默认
        elif cmd == "flush_now":
            await self._flush_buffer()
        elif cmd == "change_room":
            new_room = msg.payload.get("room_id")
            if new_room and new_room != self.room_id:
                self.stop()
                self.room_id = new_room
                await self.start()

    async def _handle_query(self, msg: Message):
        """处理状态查询请求"""
        if msg.payload.get("query") == "buffer_status":
            async with self.buffer_lock:
                buf_len = len(self.danmaku_buffer)
            await self.send_message(
                target=msg.source,
                msg_type=MessageType.RESPONSE,
                payload={"buffer_size": buf_len, "room_id": self.room_id},
                trace_id=msg.trace_id
            )

    # ==================== 重连增强 ====================
    
    async def connect(self):
        """重写 connect，增加 blivedm 重连逻辑"""
        while self.is_running:
            try:
                # 先建立与 Core 的连接
                async with websockets.connect(self.core_url) as websocket:
                    self.websocket = websocket
                    logger.info(f"Connected to Core as {self.component_id}")
                    
                    # 启动心跳
                    heartbeat_task = asyncio.create_task(self.heartbeat_loop())
                    
                    # 监听 Core 消息
                    await self.listen()
                    
                    heartbeat_task.cancel()
            except Exception as e:
                logger.error(f"Core connection lost: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)

async def main():
    component = DanmakuComponent(
        room_id=12345678,          # B站直播间ID
        batch_interval=3.0,        # 每3秒打包一次
        max_batch_size=20          # 每次最多20条
    )
    
    try:
        await component.start()
        # 保持运行
        while component.is_running:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        component.stop()

if __name__ == "__main__":
    asyncio.run(main())