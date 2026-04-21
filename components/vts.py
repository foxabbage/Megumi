# components/vtube_studio.py
import asyncio
import re
import logging
from typing import Optional, Dict, List
import inspect
import json
from config import TOP_PATH, ACTION_KEYWORDS, VTS_CONFIG
import sys
sys.path.append(TOP_PATH)

from components.base import VTuberComponent
from core.protocol import ComponentID, MessageType, Message
import pyvts

logger = logging.getLogger("VTSComponent")

class VTubeStudioComponent(VTuberComponent):
    """
    VTube Studio 控制组件
    
    功能:
    - 接收 chat_llm 的文本消息
    - 解析文本中的动作关键词
    - 通过 VTS API 触发对应热键/表情 [[43]][[58]]
    """
    
    def __init__(
        self, 
        component_id: ComponentID = ComponentID.VTS,
        core_url: str = "ws://localhost:8025/ws/",
        vts_config: Optional[Dict] = None
    ):
        super().__init__(component_id, core_url)
        
        # 合并自定义配置
        self.vts_config = {**VTS_CONFIG, **(vts_config or {})}
        
        # pyvts 客户端实例
        self.vts_client: Optional[pyvts.vts] = None
        
        # 动作去重: 记录最近触发的动作，避免重复
        self._last_triggered: Dict[str, float] = {}
        self._trigger_cooldown: float = 2.0  # 相同动作冷却时间(秒)
        
        # 注册消息处理器
        self.register_handler(MessageType.TEXT_MESSAGE, self._handle_text_message)
        self.register_handler(MessageType.COMMAND, self._handle_command)
        
        logger.info(f"VTS Component initialized with config: {self.vts_config}")

    async def _connect_vts(self) -> bool:
        """连接 VTube Studio API"""
        try:
            plugin_info = {
                "plugin_name": self.vts_config["plugin_name"],
                "developer": self.vts_config["developer"],
                "authentication_token_path": self.vts_config["token_path"],
            }
            api_info = {
                "host": self.vts_config["host"],
                "port": self.vts_config["port"],
                "name": "VTubeStudioAPI",
                "version": "1.0",
            }
            
            self.vts_client = pyvts.vts(
                plugin_info=plugin_info,
                vts_api_info=api_info
            )
            
            # 建立WebSocket连接
            await self.vts_client.connect()
            logger.info(f"Connected to VTS WebSocket at {self.vts_config['host']}:{self.vts_config['port']}")
            
            # 认证流程: 获取并验证token [[44]]
            await self.vts_client.request_authenticate_token()
            auth_success = await self.vts_client.request_authenticate()
            
            if auth_success:
                logger.info("VTS authentication successful")
                return True
            else:
                logger.warning("VTS authentication failed, check if you allowed the plugin in VTS")
                return False
                
        except ConnectionRefusedError:
            logger.error(f"Cannot connect to VTS at {self.vts_config['host']}:{self.vts_config['port']}. Is VTube Studio running with API enabled?")
            return False
        except Exception as e:
            logger.error(f"VTS connection error: {e}", exc_info=True)
            return False

    async def _disconnect_vts(self):
        """断开 VTS 连接"""
        if self.vts_client and self.vts_client.get_connection_status() == 1:
            await self.vts_client.close()
            logger.info("Disconnected from VTS")

    def _extract_actions(self, text: str) -> List[Dict]:
        """
        从文本中提取动作关键词
        
        策略:
        1. 按优先级排序匹配
        2. 支持正则表达式扩展
        3. 返回按优先级降序排列的动作列表
        """
        matched_actions = []
        text_lower = text.lower()
        
        for keyword, action_info in ACTION_KEYWORDS.items():
            # 简单关键词匹配（可扩展为正则）
            if keyword.lower() in text_lower:
                matched_actions.append({
                    "keyword": keyword,
                    **action_info
                })
        
        # 按优先级降序排序，优先触发高优先级动作
        matched_actions.sort(key=lambda x: x.get("priority", 0), reverse=True)
        
        return matched_actions

    def _check_cooldown(self, hotkey_id: str) -> bool:
        """检查动作冷却时间"""
        import time
        now = time.time()
        last_time = self._last_triggered.get(hotkey_id, 0)
        
        if now - last_time < self._trigger_cooldown:
            return False  # 仍在冷却中
        self._last_triggered[hotkey_id] = now
        return True

    async def _trigger_hotkey(self, hotkey_id: str, item_instance_id: Optional[str] = None) -> bool:
        """通过 VTS API 触发热键 [[58]]"""
        if not self.vts_client:
            logger.error("VTS client not initialized")
            return False
        
        try:
            # 构建热键触发请求
            request_msg = self.vts_client.vts_request.requestTriggerHotKey(
                hotkeyID=hotkey_id,
                itemInstanceID=item_instance_id
            )
            response = await self.vts_client.request(request_msg)
            
            if response.get("data", {}).get("hotkeyTriggered", False):
                logger.info(f"✓ Triggered hotkey: {hotkey_id}")
                return True
            else:
                logger.warning(f"✗ Failed to trigger hotkey: {hotkey_id}, response: {response}")
                return False
                
        except Exception as e:
            logger.error(f"Error triggering hotkey '{hotkey_id}': {e}", exc_info=True)
            return False

    async def _handle_text_message(self, msg: Message):
        """处理来自 chat_llm 的文本消息"""
        if msg.source != ComponentID.CHAT_LLM:
            return
            
        text = msg.payload.get("text", "") if isinstance(msg.payload, dict) else str(msg.payload)
        trace_id = msg.trace_id
        
        if not text:
            return
            
        logger.debug(f"Received text from CHAT_LLM: '{text[:50]}...'")
        
        # 1. 提取动作关键词
        actions = self._extract_actions(text)
        
        if not actions:
            return  # 无匹配动作，静默返回
            
        # 2. 执行动作（优先执行最高优先级的1个，避免动作冲突）
        for action in actions:
            hotkey_id = action["hotkey_id"]
            desc = action["desc"]
            
            # 检查冷却
            if not self._check_cooldown(hotkey_id):
                logger.debug(f"Action '{desc}' on cooldown, skipping")
                continue
                
            # 触发动作
            success = await self._trigger_hotkey(hotkey_id)
            
            if success:
                logger.info(f"✓ Executed: {desc} (keyword: '{action['keyword']}')")
                break  # 执行一个动作后退出，避免连续触发
            else:
                logger.warning(f"✗ Failed to execute: {desc}")

    async def _handle_command(self, msg: Message):
        """处理控制命令（如动态配置、手动触发动作等）"""
        if msg.source != ComponentID.CORE:
            return
            
        command = msg.payload.get("command", "") if isinstance(msg.payload, dict) else ""
        
        if command == "list_hotkeys":
            # 获取当前模型的热键列表
            if self.vts_client:
                request_msg = self.vts_client.vts_request.requestHotKeyList()
                response = await self.vts_client.request(request_msg)
                await self.send_message(
                    target=ComponentID.CORE,
                    msg_type=MessageType.RESPONSE,
                    payload={"hotkeys": response.get("data", {}).get("availableHotkeys", [])},
                    trace_id=msg.trace_id
                )
                
        elif command == "trigger":
            # 手动触发指定热键: {"command": "trigger", "hotkey_id": "xxx"}
            hotkey_id = msg.payload.get("hotkey_id") if isinstance(msg.payload, dict) else None
            if hotkey_id:
                success = await self._trigger_hotkey(hotkey_id)
                await self.send_message(
                    target=ComponentID.CORE,
                    msg_type=MessageType.RESPONSE,
                    payload={"success": success, "hotkey_id": hotkey_id},
                    trace_id=msg.trace_id
                )

    async def connect(self):
        """重写连接方法，先连接 VTS 再连接 Core"""
        # 1. 尝试连接 VTube Studio
        vts_connected = await self._connect_vts()
        if not vts_connected:
            logger.warning("VTS not connected, will retry on reconnect...")
        
        # 2. 连接 Core WebSocket (调用父类)
        await super().connect()

    async def stop(self):
        """优雅停止"""
        self.is_running = False
        await self._disconnect_vts()
        logger.info("VTS Component stopped")

async def main():
    vts = VTubeStudioComponent(
        component_id=ComponentID.VTS,
        vts_config={"port": 8001}  # 自定义配置
    )
    try:
        await vts.start()
    except KeyboardInterrupt:
        await vts.stop()

if __name__ == "__main__":
    asyncio.run(main())