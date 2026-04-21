# components/pc_control_llm.py
import asyncio
import time
import logging
import os
from typing import Optional, Dict, List, Any
from collections import deque
import json
import re
from pathlib import Path

from dashscope import MultiModalConversation
import dashscope

from config import TOP_PATH, GUI_PROMPT, INFER_PROMPT, INFER_MODEL, GUI_MODEL, DASHSCOPE_API_KEY, BROWSER_PROMPT
import sys
sys.path.append(TOP_PATH)
from components.base import VTuberComponent
from core.protocol import Message, ComponentID, MessageType
from utils.pyautogui import ComputerTools
from utils.playwrightgui import PlaywrightComputer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("PCControlLLM")

class PCControlLLM(VTuberComponent):
    MAX_INFER_SCREENSHOTS = 5
    MAX_GUI_SCREENSHOTS = 3
    MAX_INFER_HISTORY = 6
    MAX_GUI_HISTORY = 10
    
    def __init__(self, core_url: str = "ws://localhost:8025/ws/"):
        super().__init__(ComponentID.PC_LLM, core_url)
        
        # 截图缓存：deque 保证 FIFO，自动丢弃旧截图
        self.screenshots_auto: deque = deque(maxlen=self.MAX_INFER_SCREENSHOTS)   # 流式截图
        self.screenshots_request = None
        self.screenshots_request_deque: deque = deque(maxlen=self.MAX_GUI_SCREENSHOTS)
        self.env_state = None

        self.infer_history: deque = deque(maxlen=self.MAX_INFER_HISTORY)
        self.gui_history: deque = deque(maxlen=self.MAX_GUI_HISTORY)
        
        # 注册消息处理器
        self.register_handler(MessageType.TEXT_MESSAGE, self._handle_chat_command)
        self.register_handler(MessageType.STREAM_DATA, self._handle_screenshot_stream)
        self.register_handler(MessageType.RESPONSE, self._handle_screenshot_response)
        
        # 状态标记
        self._pending_trace_id: Optional[str] = None
        self._last_command_text: Optional[str] = ""
        self.computer_tools = ComputerTools()
        self.web_tools = PlaywrightComputer(task_dir=f"{TOP_PATH}/cache")
        
        # 新增：命令缓存和执行状态
        self._pending_text_cache: Optional[str] = None  # 缓存待执行的文本命令
        self._is_executing: bool = False  # 是否正在执行任务

    # ==================== 消息处理 ====================
    
    async def _handle_chat_command(self, msg: Message):
        """处理来自 chat_llm 的文本指令"""
        try:
            text = msg.payload.get("text", "")
            
            # 如果正在执行，缓存 text（简单合并）
            if self._is_executing:
                if self._pending_text_cache:
                    self._pending_text_cache += "\n" + text
                else:
                    self._pending_text_cache = text
                logger.info(f"Command cached (currently executing): {text[:100]}...")
                logger.info(f"Current cache: {self._pending_text_cache[:200]}...")
                return
            
            # 开始执行
            task = asyncio.create_task(self._execute_command_with_cache(text, msg.trace_id))
                
        except Exception as e:
            logger.error(f"Error handling chat command: {e}")
            await self._send_execution_result(f"执行异常: {str(e)}", success=False)
            self._is_executing = False
    
    async def _execute_command_with_cache(self, initial_text: str, trace_id: str):
        """执行命令，并在执行完后检查是否有缓存的命令"""
        self._is_executing = True
        self._last_command_text = initial_text
        self._pending_trace_id = trace_id
        
        try:
            while True:
                # 执行当前命令
                operation_desc = await self._infer_operation_with_screenshots(self._last_command_text)
                if operation_desc.strip() and operation_desc.find("无需操作")==-1:
                    self._last_command_text = ""
                    first_line, *rest = operation_desc.split('\n', 1)
                    operation_desc = rest[0] if rest else ""
                    logger.info(operation_desc)
                    
                    if operation_desc and first_line.find("gui")>=0:
                        logger.info(f"gui:{operation_desc}")
                        flag = await self._execute_gui_operation(operation_desc)
                        await self._send_execution_result(operation_desc, success=flag)
                    elif operation_desc and first_line.find("browser")>=0:
                        logger.info(f"browser:{operation_desc}")
                        flag = await self._execute_browser_operation(operation_desc)
                        await self._send_execution_result(operation_desc, success=flag)
                    else:
                        await self._send_execution_result("无法解析操作意图", success=False)
                else:
                    await self._send_execution_result(operation_desc, success=True)
                
                # 检查是否有缓存的命令
                if self._pending_text_cache:
                    # 取出缓存的命令
                    self._last_command_text = self._pending_text_cache
                    self._pending_text_cache = None
                    logger.info(f"Processing cached command: {self._last_command_text[:100]}...")
                    # 继续循环执行缓存的命令
                else:
                    # 没有缓存命令，退出循环
                    break
        except Exception as e:
            logger.warning(f"execute error: {e}")
        finally:
            self._is_executing = False
            self._last_command_text = ""
            logger.info("Command execution completed, no more cached commands")

    async def _handle_screenshot_stream(self, msg: Message):
        """处理截图流数据（来自 screenshot 组件）"""
        try:
            payload = msg.payload
            image_path = payload.get("image_path")
            
            if not image_path or not os.path.exists(image_path):
                logger.warning(f"Screenshot file not found: {image_path}")
                return
            
            screenshot_entry = {
                "image_path": image_path,
                "timestamp": payload.get("timestamp", time.time()),
                "width": payload.get("width", 0),
                "height": payload.get("height", 0)
            }

            self.screenshots_auto.append(screenshot_entry)
            logger.debug(f"Cached auto screenshot: {Path(image_path).name}")
                
        except Exception as e:
            logger.error(f"Error handling screenshot stream: {e}")
    
    async def _handle_screenshot_response(self, msg: Message):
        """处理截图流数据（来自 screenshot 组件）"""
        try:
            payload = msg.payload
            image_path = payload.get("image_path")
            
            if not image_path or not os.path.exists(image_path):
                logger.warning(f"Screenshot file not found: {image_path}")
                return
            
            screenshot_entry = {
                "image_path": image_path,
                "timestamp": payload.get("timestamp", time.time()),
                "width": payload.get("width", 0),
                "height": payload.get("height", 0),
                "trace_id": msg.trace_id
            }
            
            self.screenshots_request = screenshot_entry
            logger.debug(f"Cached request screenshot: {Path(image_path).name}")
                
        except Exception as e:
            logger.error(f"Error handling screenshot stream: {e}")

    # ==================== 核心推理逻辑 ====================
    
    async def _infer_operation_with_screenshots(self, command_text: str) -> Optional[str]:
        """
        结合截图和文本指令，使用 Qwen3.5-Flash 推理自然语言操作描述
        返回: 自然语言描述的操作步骤（如："点击左上角的设置按钮，然后选择'导出'菜单项"）
        """
        # 构建消息内容：文本 + 截图（优先使用主动请求截图，不足时补充流截图）
        messages = [{"role": "system", "content": [{"text": INFER_PROMPT}]}]
        if len(self.infer_history) > 0:
            messages.extend(self.infer_history)
        contents = [{"text": command_text}]
        self.infer_history.append({"role": "user", "content": contents})
        
        for shot in reversed(self.screenshots_auto):  # 最近的在前
            contents.append({
                "image": f"file://{shot["image_path"]}"  # DashScope 支持直接传入
            })
        messages.append({"role": "user", "content": contents})
        try: 
            response = await asyncio.to_thread(
                MultiModalConversation.call,
                model=INFER_MODEL,
                messages=messages,
                temperature=0.1,  # 降低随机性，保证操作描述稳定
                stream=False,
                enable_thinking=False
            )
            
            if response.status_code == 200:
                result = response.output.choices[0].message.content[0]["text"]
                # 提取纯文本（可能包含 markdown，按需清理）
                operation_desc = result.strip().replace("```", "").strip()
                self.infer_history.append({"role": "assistant", "content": [{"text":operation_desc}]})
                return operation_desc
            else:
                logger.error(f"DashScope API error: {response.code} - {response.message}")
                return None
                
        except Exception as e:
            logger.error(f"Inference failed: {e}")
            return None

    # ==================== 工具方法 ====================

    async def _send_execution_result(self, operation_desc: str, success: bool):
        """向 chat_llm 回传执行结果"""
        payload = {
            "operation_description": operation_desc,
            "success": success,
            "timestamp": time.time()
        }
        
        await self.send_message(
            target=ComponentID.CHAT_LLM,
            msg_type=MessageType.RESPONSE,
            payload=payload,
            trace_id=self._pending_trace_id
        )

    # ==================== 预留接口 ====================
    
    async def _execute_gui_operation(self, operation_desc: str, max_iter: int=20):
        """
        使用 Qwen GUI-Plus 模型执行具体操作
        """
        stop_flag = False
        self.gui_history.clear()
        self.screenshots_request_deque.clear()
        for _ in range(max_iter):
            if stop_flag:
                break

            # 1. 截图
            screen = await self._request_screenshot()
            if not screen:
                return False

            # 2. 构造消息
            messages = self._build_gui_messages(operation_desc)

            # 3. 调用模型
            response = await asyncio.to_thread(
                MultiModalConversation.call,
                model=GUI_MODEL,
                messages=messages,
                stream=False,
                enable_thinking=False
            )
            if response.status_code == 200:
                output_text = response.output.choices[0].message.content[0]['text']
                self.gui_history.append({"role": "assistant", "content": [{"text": output_text}]})
            else:
                logger.error(f"DashScope API error: {response.code} - {response.message}")
                continue

            # 4. 解析操作
            action_list = self._extract_tool_calls(output_text)
            logger.info(f"pc action:{action_list}")
            if not action_list:
                return True

            # 5. 执行操作
            for action in action_list:
                DESKTOP_H, DESKTOP_W = 3072, 1920
                action_parameter = action['arguments']
                action_type = action_parameter['action']

                # 映射坐标（从归一化坐标 1000x1000 映射到实际尺寸）
                for key in ['coordinate', 'coordinate1', 'coordinate2']:
                    if key in action_parameter:
                        x_norm, y_norm = action_parameter[key][0], action_parameter[key][1]
        
                        # 1. 归一化坐标 (0~1000) -> 桌面绝对坐标
                        target_x = x_norm*3
                        target_y = y_norm*3
                        
                        # 2. 边界保护（防止大模型幻觉导致坐标越界）
                        target_x = max(0, min(target_x, DESKTOP_W - 1))
                        target_y = max(0, min(target_y, DESKTOP_H - 1))
                        
                        # 3. 写回原字典
                        action_parameter[key][0] = int(target_x)
                        action_parameter[key][1] = int(target_y)

                # 执行对应操作
                if action_type in ['click', 'left_click']:
                    await self.computer_tools.left_click(
                        action_parameter['coordinate'][0],
                        action_parameter['coordinate'][1]
                    )

                elif action_type == 'mouse_move':
                    await self.computer_tools.mouse_move(
                        action_parameter['coordinate'][0],
                        action_parameter['coordinate'][1]
                    )
                
                elif action_type == 'move_relative':
                    await self.computer_tools.move_relative(
                        action_parameter['coordinate'][0],
                        action_parameter['coordinate'][1]
                    )

                elif action_type == 'middle_click':
                    await self.computer_tools.middle_click(
                        action_parameter['coordinate'][0],
                        action_parameter['coordinate'][1]
                    )

                elif action_type in ['right click', 'right_click']:
                    await self.computer_tools.right_click(
                        action_parameter['coordinate'][0],
                        action_parameter['coordinate'][1]
                    )

                elif action_type in ['key', 'hotkey']:
                    await self.computer_tools.press_key(action_parameter['keys'])

                elif action_type == 'type':
                    text = action_parameter['text']
                    await self.computer_tools.type(text)

                elif action_type == 'drag':
                    await self.computer_tools.left_click_drag(
                        action_parameter['coordinate'][0],
                        action_parameter['coordinate'][1]
                    )

                elif action_type == 'scroll':
                    if 'coordinate' in action_parameter:
                        await self.computer_tools.mouse_move(
                            action_parameter['coordinate'][0],
                            action_parameter['coordinate'][1]
                        )
                    await self.computer_tools.scroll(action_parameter.get("pixels", 1))

                elif action_type in ['computer_double_click', 'double_click']:
                    await self.computer_tools.double_click(
                        action_parameter['coordinate'][0],
                        action_parameter['coordinate'][1]
                    )

                elif action_type == 'wait':
                    await asyncio.sleep(action_parameter.get('time', 2))

                elif action_type == 'answer':
                    stop_flag = True
                    break

                elif action_type in ['stop', 'terminate', 'done']:
                    stop_flag = True
                    break

                else:
                    logger.warning(f"未知操作类型: {action_type}")

        return True
    
    def _build_gui_history(self):
        n = len(self.gui_history)
        if n==0:
            return []
        m = len(self.screenshots_request_deque)
        gui = list(self.gui_history)
        prefix_gui = gui[:n - m]
        
        suffix_gui = list(gui[n - m:])
        suffix_scr = list(self.screenshots_request_deque)
        interleaved = []
        for s, g in zip(suffix_scr, suffix_gui):
            interleaved.append(s)
            interleaved.append(g)
        return prefix_gui + interleaved
    
    def _build_gui_messages(self, operation_desc):
        messages = [{"role": "system", "content": [{"text": GUI_PROMPT}]}]
        messages.append({"role": "user", "content": [{"text": operation_desc}]})
        gui_history = self._build_gui_history()
        messages.extend(gui_history)
        messages.append({"role": "user", "content": [{"text":"Please generate the next move according to the UI screenshot, instruction and previous actions. Current screenshot:"}, {"image": f"file://{self.screenshots_request["image_path"]}"}]})
        self.screenshots_request_deque.append({"role": "user", "content": [{"image": f"file://{self.screenshots_request["image_path"]}"}]})
        return messages
    
    async def _execute_browser_operation(self, operation_desc: str, max_iter: int=20, dpi=2):
        import uuid
        self.gui_history.clear()
        self.screenshots_request_deque.clear()
        if self.web_tools._is_initialized:
            await self.web_tools.focus()
        else:
            await self.web_tools.reset()
        stop_flag = False
        session_id = str(uuid.uuid4())

        for step_id in range(max_iter):
            if stop_flag:
                break
            self.env_state = await self.web_tools.current_state(it=step_id)
            messages = self._build_browser_messages(operation_desc)

            response = await asyncio.to_thread(
                MultiModalConversation.call,
                model=GUI_MODEL,
                messages=messages,
                stream=False,
                enable_thinking=False,
                headers={"x-dashscope-gui-session-id": session_id}
            )
            if response.status_code == 200:
                output_text = response.output.choices[0].message.content[0]['text']
                self.gui_history.append({"role": "assistant", "content": [{"text": output_text}]})
            else:
                logger.error(f"DashScope API error: {response.code} - {response.message}")
                continue

            action_list = self._extract_tool_calls(output_text)
            logger.info(f"pc action:{action_list}")

            for action in action_list:
                action_parameter = action['arguments']
                action_type = action_parameter['action']
                label = action_parameter.get('label', None)
                wiki_url = "https://baike.baidu.com/"
                coordicate = []

                if label is not None:
                    if label == "WINDOW":
                        coordicate = [500, 500]
                    else:
                        ele = self.env_state["SoM"]["SoM_list"][label]
                        box = ele["bbox"]
                        x, y, w, h = box.get("x"), box.get("y"), box.get("width"), box.get("height")
                        nx = int((x + w/2)/dpi + 0.5) # 注意视口大小和som大小不匹配，需要通过dpi调整
                        ny = int((y + h/2)/dpi + 0.5)
                        coordicate = [nx, ny]
                        action_parameter['coordinate'] = coordicate
                        logger.info(coordicate)

                if action_type == 'wait':
                    await asyncio.sleep(action_parameter.get('time', 2))
                elif action_type == 'scroll':
                    direction = action_parameter['direction']
                    await self.web_tools.scroll_at(coordicate[0], coordicate[1], direction=direction, magnitude=300)
                elif action_type == 'select':
                    text = action_parameter['option']
                    await self.web_tools._select(coordicate[0], coordicate[1], text=text)
                elif action_type == 'goback':
                    await self.web_tools.go_back()
                elif action_type == 'goto':
                    url = action_parameter.get('url', '')
                    await self.web_tools.navigate(url, normalize=True)
                elif action_type == 'click':
                    await self.web_tools.click_at(coordicate[0], coordicate[1])
                elif action_type == 'type':
                    text = action_parameter['text']
                    await self.web_tools.type_text_at(coordicate[0], coordicate[1], text)
                elif action_type == 'wikipedia':
                    await self.web_tools.navigate(wiki_url, normalize=True)
                elif action_type == 'answer':
                    text = action_parameter['text']
                    stop_flag = True
                    break
            await asyncio.sleep(2)
        return True
    
    def _build_browser_history(self):
        n = len(self.gui_history)
        if n==0:
            return []
        m = len(self.screenshots_request_deque)
        gui = list(self.gui_history)
        prefix_gui = gui[:n - m]
        
        suffix_gui = list(gui[n - m:])
        suffix_scr = list(self.screenshots_request_deque)
        interleaved = []
        for s, g in zip(suffix_scr, suffix_gui):
            interleaved.append(s)
            interleaved.append(g)
        return prefix_gui + interleaved

    
    def _build_browser_messages(self, operation_desc):
        messages = [{"role": "system", "content": [{"text": BROWSER_PROMPT}]}]
        messages.append({"role": "user", "content": [{"text": operation_desc}]})
        gui_history = self._build_browser_history()
        messages.extend(gui_history)
        messages.append({"role": "user", "content": [{"text":"Please generate the next move according to the UI screenshot, instruction and previous actions. Current screenshot:"}, 
                                                     {"image": f"file://{self.env_state["img_path"]}"},
                                                     {"text": self.env_state["SoM"]["format_ele_text"]}]})
        self.screenshots_request_deque.append({"role": "user", 
                                               "content": [{"image": f"file://{self.env_state["img_path"]}"},
                                                {"text": self.env_state["SoM"]["format_ele_text"]}]})
        return messages
    
    # components/pc_control_llm.py 中添加以下方法

    async def _request_screenshot(self, timeout: float = 3.0) -> bool:
        """
        主动向 SCREENSHOT 组件请求截图
        
        Args:
            timeout: 等待截图响应的超时时间（秒）
            
        Returns:
            bool
        """
        import uuid
        
        # 生成唯一 trace_id 用于匹配响应
        trace_id = f"pc_llm_req_{uuid.uuid4().hex[:8]}"
        
        try:
            # 构造查询消息
            payload={
                "query": "screenshot",
                "timestamp": time.time()
            }
            logger.debug(f"Sending screenshot request with trace_id: {trace_id}")
            
            await self.send_message(ComponentID.SCREENSHOT, MessageType.QUERY, payload, trace_id)
            
            # 等待响应：轮询检查 screenshots_request 缓存
            start_time = time.time()
            while time.time() - start_time < timeout:
                # 检查是否有新截图进入缓存（通过比对时间戳或数量变化）
                if self.screenshots_request:
                    if self.screenshots_request["trace_id"] == trace_id:
                        return True
                await asyncio.sleep(0.1)
            
            logger.warning(f"Screenshot request timeout after {timeout}s")
            return False
            
        except Exception as e:
            logger.error(f"Failed to request screenshot: {e}")
            return False

    def _extract_tool_calls(self, text: str) -> List:
        """
        从模型输出中提取所有 <tool_call> 块

        参数:
            text: 模型返回的文本

        返回:
            actions: 解析后的操作列表
        """
        pattern = re.compile(r'<tool_call>(.*?)</tool_call>', re.DOTALL | re.IGNORECASE)
        blocks = pattern.findall(text)

        actions = []
        for blk in blocks:
            blk = blk.strip()
            try:
                actions.append(json.loads(blk))
            except json.JSONDecodeError as e:
                logger.error(f"Computer control tool calling failed with json error: {e}")

        return actions

    # ==================== 生命周期 ====================
    
    async def start(self):
        logger.info(f"PC Control LLM component starting with ID: {self.component_id}")
        await super().start()
    
    def stop(self):
        """清理资源"""
        self.screenshots_auto.clear()
        self.screenshots_request.clear()
        self.computer_tools.shutdown()
        super().stop()
        logger.info("PC Control LLM component stopped")

async def main():
    pc_control = PCControlLLM()
    dashscope.api_key = DASHSCOPE_API_KEY
    dashscope.base_http_api_url = 'https://dashscope.aliyuncs.com/api/v1'
    try:
        await pc_control.start()
    except KeyboardInterrupt:
        pc_control.stop()

if __name__ == "__main__":
    asyncio.run(main())