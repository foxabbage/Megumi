# components/base.py
import asyncio
import websockets
from websockets.protocol import State
import json
import logging
import signal
from typing import Callable, Optional, Any, Dict
import sys
from config import TOP_PATH
sys.path.append(TOP_PATH)
from core.protocol import Message, ComponentID, MessageType
import inspect

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ComponentBase")

class VTuberComponent:
    def __init__(self, component_id: ComponentID, core_url: str = "ws://localhost:8025/ws/"):
        self.component_id = component_id
        self.core_url = f"{core_url}{component_id}"
        self.websocket: Optional[websockets.ClientConnection] = None
        self.message_handlers: Dict[MessageType, Callable] = {}
        self.is_running = False  # 【关键】初始为 False，start() 时再设为 True
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._cleanup_done = False  # 【关键】防止重复清理

    def register_handler(self, msg_type: MessageType, handler: Callable):
        self.message_handlers[msg_type] = handler

    async def heartbeat_loop(self):
        while self.is_running and self.websocket:
            try:
                await self.send_message(
                    target=ComponentID.CORE,
                    msg_type=MessageType.HEARTBEAT,
                    payload={}
                )
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                logger.debug("Heartbeat cancelled")
                break
            except Exception as e:
                if self.is_running:
                    logger.warning(f"Heartbeat error: {e}")
                break

    async def connect(self):
        """主连接循环 - 关键修复：任何中断都立即设置 is_running=False"""
        while self.is_running:
            try:
                async with websockets.connect(self.core_url) as websocket:
                    self.websocket = websocket
                    logger.info(f"Connected to Core as {self.component_id}")
                    
                    self._heartbeat_task = asyncio.create_task(self.heartbeat_loop())
                    
                    # 【关键】listen 返回即表示需要退出，不再重连
                    await self.listen()
                    
            except (KeyboardInterrupt, asyncio.CancelledError):
                # 【关键】任何中断都立即标记停止，避免重连
                logger.info("Interrupt received, stopping...")
                self.is_running = False
                break
            except (websockets.exceptions.ConnectionClosed,
                    websockets.exceptions.InvalidURI,
                    OSError, ConnectionRefusedError) as e:
                # 仅在网络错误且仍需运行时重试
                if self.is_running:
                    logger.warning(f"Connection issue: {e}. Reconnecting in 5s...")
                    await asyncio.sleep(5)
                else:
                    break
            except Exception as e:
                if self.is_running:
                    logger.error(f"Unexpected error: {e}. Reconnecting in 5s...")
                    await asyncio.sleep(5)
                else:
                    break
            # 【关键】正常退出 listen 后，检查是否应停止
            if not self.is_running:
                break

    async def listen(self):
        """消息监听 - 非阻塞 + 安全 + 顺序保证"""
        message_queue = asyncio.Queue(maxsize=100)  # 防止内存爆炸
        
        # 生产者：快速接收
        async def producer():
            try:
                async for message in self.websocket:
                    if not self.is_running:
                        break
                    if message_queue.full():
                        logger.warning("Message queue full, dropping oldest")
                        message_queue.get_nowait()  # 丢弃最旧的
                    await message_queue.put(message)
            except (KeyboardInterrupt, asyncio.CancelledError):
                raise
            except Exception as e:
                if self.is_running:
                    logger.error(f"Receive error: {e}")
                raise
        
        # 消费者：串行处理
        async def consumer():
            while self.is_running:
                try:
                    message = await asyncio.wait_for(
                        message_queue.get(), 
                        timeout=1.0  # 定期检查 is_running
                    )
                    # 解析 + 分发
                    data = json.loads(message)
                    msg = Message(**data)
                    if msg.source == self.component_id:
                        continue
                    handler = self.message_handlers.get(msg.type)
                    if handler:
                        # ✅ 创建独立任务，不阻塞消费者
                        asyncio.create_task(self._execute_handler(handler, msg))
                    message_queue.task_done()
                except asyncio.TimeoutError:
                    continue  # 定期检查 is_running
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"Consumer error: {e}")
        
        # 启动生产-消费协程
        producer_task = asyncio.create_task(producer())
        consumer_task = asyncio.create_task(consumer())
        
        # 等待任一结束（通常是生产者因断开结束）
        done, pending = await asyncio.wait(
            [producer_task, consumer_task],
            return_when=asyncio.FIRST_COMPLETED
        )
        
        # 清理
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _execute_handler(self, handler: Callable, msg: Message):
        """
        安全执行消息处理器
        """
        try:
            # 设置处理超时（可选，避免单个消息卡死）
            if inspect.iscoroutinefunction(handler):
                # 异步处理器：直接 await + 超时保护
                await asyncio.wait_for(handler(msg), timeout=10.0)
            else:
                # 同步处理器：放到线程池执行，避免阻塞事件循环
                # 注意：如果 handler 操作 Qt 控件，必须确保在主线程执行！
                await asyncio.to_thread(handler, msg)
                
        except asyncio.TimeoutError:
            logger.warning(f"Handler {handler.__name__} timed out for message: {msg}")
        except Exception as e:
            # ✅ 关键：捕获所有异常，防止监听循环意外退出
            logger.error(f"Handler {handler.__name__} failed: {e}", exc_info=True)
        # ✅ 正常返回，不传播异常

    async def send_message(self, target: ComponentID, msg_type: MessageType, payload: Any, trace_id: Optional[str] = None):
        if not self.is_running or not self.websocket or self.websocket.state is State.CLOSED:
            return
        try:
            msg = Message(source=self.component_id, target=target, type=msg_type, payload=payload, trace_id=trace_id)
            await self.websocket.send(json.dumps(msg.model_dump(mode="json"), ensure_ascii=False))
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except Exception as e:
            logger.debug(f"Send failed (may be shutting down): {e}")

    async def start(self):
        """启动入口 - 统一信号处理和生命周期"""
        if self.is_running:
            logger.warning(f"{self.component_id} already running")
            return
            
        self.is_running = True
        self._cleanup_done = False
        
        try:
            await self.connect()
        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.info(f"{self.component_id} interrupted")
        finally:
            # 【关键】确保 cleanup 只执行一次
            await self._safe_cleanup()

    async def _safe_cleanup(self):
        """幂等清理 - 避免重复关闭资源"""
        if self._cleanup_done:
            return
        self._cleanup_done = True
        
        logger.info(f"Cleaning up {self.component_id}...")
        self.is_running = False
        
        # 取消心跳任务
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except (asyncio.CancelledError, Exception):
                pass
        
        # 关闭 websocket
        if self.websocket and self.websocket.state is not State.CLOSED:
            try:
                await self.websocket.close(code=1000, reason="Component stopping")
            except (asyncio.CancelledError, KeyboardInterrupt):
                pass
            except Exception as e:
                logger.debug(f"Close websocket warning: {e}")
        
        self.websocket = None
        logger.info(f"{self.component_id} cleaned up")

    def stop(self):
        """外部停止接口 - 幂等设计"""
        logger.info(f"Stop signal received for {self.component_id}")
        self.is_running = False
        # 注意：实际资源释放由 _safe_cleanup 异步完成