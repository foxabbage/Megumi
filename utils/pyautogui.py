# components/computer_tools.py
import asyncio
import pyautogui
import pyperclip
import time
from typing import Optional, Union, List, Tuple
from PIL import Image
from concurrent.futures import ThreadPoolExecutor


class ComputerTools:
    """
    异步兼容的电脑 GUI 操作工具类
    ⚠️ 所有方法设计为 async，但内部同步调用需注意事件循环阻塞
    """
    _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pyautogui_worker")

    @classmethod
    async def shutdown(cls):
        """程序退出时清理资源"""
        cls._executor.shutdown(wait=True)

    def __init__(self, duration: float = 0.1, pause: float = 0.05):
        """
        Args:
            duration: 鼠标移动默认耗时(秒)
            pause: 操作间默认暂停时间(秒)，防止操作过快被系统忽略
        """
        self.image_info: Optional[Tuple[int, int]] = None
        self.duration = duration
        self.pause = pause
        
        # 配置 pyautogui
        pyautogui.FAILSAFE = True  # 鼠标移到左上角可紧急停止
        pyautogui.PAUSE = pause

    # ==================== 截图相关 ====================

    async def _load_image_info_async(self, path: str):
        """异步加载图片尺寸"""
        loop = asyncio.get_running_loop()
        width, height = await loop.run_in_executor(
            self._executor,
            lambda: Image.open(path).size
        )
        self.image_info = (width, height)

    # ==================== 基础操作 ====================
    
    async def reset(self):
        """显示桌面 (Win+D)"""
        await self._run_pyautogui_action(
            lambda: pyautogui.hotkey('win', 'd')
        )

    async def press_key(self, keys: Union[str, List[str]]):
        # 1. 在外部统一处理类型，避免内部作用域污染
        keys_to_press = [keys] if isinstance(keys, str) else keys

        def _press():
            # 清理和转换键名
            cleaned = []
            for key in keys_to_press:
                if isinstance(key, str):
                    key = self._normalize_key_name(key)
                cleaned.append(key)
            
            if len(cleaned) > 1:
                pyautogui.hotkey(*cleaned)
            else:
                pyautogui.press(cleaned[0])

        await self._run_pyautogui_action(_press)

    def _normalize_key_name(self, key: str) -> str:
        """标准化键名格式"""
        key = key.strip()
        # 移除可能的列表包装
        if key.startswith("keys=["): key = key[6:]
        if key.endswith("]"): key = key[:-1]
        if key.startswith(("['", '["')): key = key[2:]
        if key.endswith(("']", '"]')): key = key[:-2]
        
        # 键名映射
        key_map = {
            "arrowleft": "left", "arrowright": "right",
            "arrowup": "up", "arrowdown": "down",
            "enter": "return", "space": " ",
        }
        return key_map.get(key.lower(), key)

    async def type(self, text: str, interval: float = 0.02):
        """
        输入文本（剪贴板方式支持中文）
        Args:
            text: 要输入的文本
            interval: 字符间隔(秒)，仅当剪贴板失败时回退使用
        """
        def _type_with_clipboard():
            pyperclip.copy(text)
            # 短暂等待确保剪贴板就绪
            time.sleep(0.05)
            pyautogui.hotkey('ctrl', 'v')
        
        def _type_fallback():
            # 回退方案：逐字符输入（不支持中文）
            pyautogui.write(text, interval=interval)
        
        try:
            await self._run_pyautogui_action(_type_with_clipboard)
        except Exception as e:
            print(f"Clipboard input failed, fallback to direct: {e}")
            await self._run_pyautogui_action(_type_fallback)

    # ==================== 鼠标操作 ====================
    
    async def mouse_move(self, x: float, y: float, duration: Optional[float] = None):
        """移动鼠标"""
        dur = duration if duration is not None else self.duration
        await self._run_pyautogui_action(
            lambda: pyautogui.moveTo(x, y, duration=dur)
        )
    
    async def move_relative(self, x: float, y: float, duration: Optional[float] = None):
        """相对移动鼠标"""
        dur = duration if duration is not None else self.duration
        await self._run_pyautogui_action(
            lambda: pyautogui.moveRel(x, y, duration=dur)
        )

    async def left_click(self, x: Optional[float] = None, y: Optional[float] = None, clicks: int = 1):
        """左键点击（可选坐标）"""
        if x is not None and y is not None:
            await self.mouse_move(x, y)
        await self._run_pyautogui_action(
            lambda: pyautogui.click(clicks=clicks, button='left')
        )

    async def left_click_drag(self, x: float, y: float, duration: Optional[float] = None):
        """拖拽到指定坐标"""
        dur = duration if duration is not None else self.duration
        await self._run_pyautogui_action(
            lambda: pyautogui.dragTo(x, y, duration=dur)
        )

    async def right_click(self, x: Optional[float] = None, y: Optional[float] = None):
        """右键点击"""
        if x is not None and y is not None:
            await self.mouse_move(x, y)
        await self._run_pyautogui_action(
            lambda: pyautogui.click(button='right')
        )

    async def middle_click(self, x: Optional[float] = None, y: Optional[float] = None):
        """中键点击"""
        if x is not None and y is not None:
            await self.mouse_move(x, y)
        await self._run_pyautogui_action(
            lambda: pyautogui.click(button='middle')
        )

    async def double_click(self, x: Optional[float] = None, y: Optional[float] = None):
        """双击"""
        await self.left_click(x, y, clicks=2)

    async def triple_click(self, x: Optional[float] = None, y: Optional[float] = None):
        """三击"""
        await self.left_click(x, y, clicks=3)

    async def scroll(self, pixels: int, x: Optional[float] = None, y: Optional[float] = None):
        """滚轮滚动"""
        if x is not None and y is not None:
            await self.mouse_move(x, y)
        await self._run_pyautogui_action(
            lambda: pyautogui.scroll(pixels)
        )

    # ==================== 内部工具 ====================
    
    async def _run_pyautogui_action(self, action_func):
        """
        在单线程 executor 中执行 pyautogui 操作
        确保所有鼠标/键盘操作串行执行，避免竞态
        """
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._executor, action_func)
        # 操作后短暂暂停，给系统响应时间
        await asyncio.sleep(self.pause)

    async def wait(self, seconds: float):
        """非阻塞等待"""
        await asyncio.sleep(seconds)

    def get_screen_size(self) -> Tuple[int, int]:
        """获取屏幕分辨率（同步方法，轻量无阻塞）"""
        return pyautogui.size()

    def position(self) -> Tuple[int, int]:
        """获取当前鼠标位置"""
        return pyautogui.position()