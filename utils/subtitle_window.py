# utils/subtitle_window.py
from PySide6.QtWidgets import (
    QWidget, QLabel, QVBoxLayout, QApplication, QGraphicsOpacityEffect, QSizePolicy
)
from PySide6.QtCore import Qt, QTimer, Slot, QRect
import random

import logging
logger = logging.getLogger("SubtitleWindow")


class SubtitleLabel(QLabel):
    """支持逐字显示的字幕标签（修复：半透明背景 + 大小策略）"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        
        # 基础设置
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setWordWrap(True)
        
        # 🔑 关键修复：设置大小策略，避免透明背景下控件大小为0
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setMinimumHeight(60)
        self.setMinimumWidth(200)
        
        # 🎨 样式表：使用纯色半透明背景（兼容性更好）
        self.setStyleSheet("""
            QLabel {
                color: pink;
                border-radius: 12px;
                padding: 12px 24px;
                font-size: 16px;
                font-weight: 500;
            }
        """)
        
        # 逐字显示定时器
        self._type_timer = QTimer(self)
        self._type_timer.setSingleShot(False)
        self._type_timer.timeout.connect(self._on_type_tick)
        
        # 停留定时器
        self._hold_timer = QTimer(self)
        self._hold_timer.setSingleShot(True)
        self._hold_timer.timeout.connect(self._do_clear)
        
        # 逐字显示状态
        self._full_text = ""
        self._current_index = 0
        self._type_interval = 50  # 每50ms显示一个字符
        
    @Slot(str, int)
    def show_text(self, text: str, duration):
        """显示字幕：逐字显示 -> 停留 -> 清除"""
        logger.info(f"SubtitleLabel showing: {text[:50]}... for {duration}ms")
        
        # 停止所有定时器，避免竞争
        self._type_timer.stop()
        self._hold_timer.stop()
        
        # 重置状态
        self._full_text = text
        self._current_index = 0
        self.setText("")
        
        # 🔑 关键：确保控件可见且刷新
        self.show()
        self.raise_()
        
        # 空文本处理
        if not text:
            self.setText("")
            self._hold_timer.start(duration)
            return
        
        # 开始逐字显示
        self._type_timer.start(self._type_interval)
        
        # 计算总时间：逐字耗时 + 停留时间
        total_type_time = len(text) * self._type_interval
        self._hold_timer.start(total_type_time + duration)
        
    def _on_type_tick(self):
        """逐字显示回调"""
        if self._current_index <= len(self._full_text):
            self.setText(self._full_text[:self._current_index])
            self._current_index += 1
        else:
            self._type_timer.stop()
    
    def _do_clear(self):
        """执行清除操作"""
        self._type_timer.stop()
        self._hold_timer.stop()
        self.setText("")
        
    @Slot()
    def clear_subtitle(self):
        """立即清除字幕"""
        self._type_timer.stop()
        self._hold_timer.stop()
        self.setText("")


class SubtitleWindow(QWidget):
    """独立字幕窗口 - 无边框、置顶、半透明背景"""
    
    WINDOW_WIDTH = 800
    WINDOW_HEIGHT = 300
    
    def __init__(self, screen_idx: int = 0, always_on_top: bool = True, click_through: bool = False):
        super().__init__()
        self._positioned = False
        
        self._setup_ui()
        self._setup_window(screen_idx, always_on_top, click_through)
        
    def _setup_ui(self):
        """初始化UI组件"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(0)
        
        self.subtitle = SubtitleLabel()
        layout.addWidget(self.subtitle)
        
        self.setWindowTitle("VTuber Subtitle")
        self.setFixedSize(self.WINDOW_WIDTH, self.WINDOW_HEIGHT)
        
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        
        # 窗口自身也设为透明（不影响子控件绘制）
        self.setStyleSheet("background: transparent;")
        
    def _setup_window(self, screen_idx: int, always_on_top: bool, click_through: bool):
        """配置窗口属性和位置"""
        # 窗口标志：无边框 + 置顶 + 工具窗口
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        if always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
            
        # 鼠标穿透设置
        if click_through:
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            
        # 🔑 再次确保半透明支持（窗口级别）
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        
        self._position_to_screen(screen_idx)
        
    def _position_to_screen(self, screen_idx: int):
        """将窗口定位到指定屏幕的底部中央"""
        screens = QApplication.screens()
        if 0 <= screen_idx < len(screens):
            screen = screens[screen_idx]
            geo = screen.geometry()
            
            x = geo.x() + (geo.width() - self.WINDOW_WIDTH) // 2
            y = geo.y() + geo.height() - self.WINDOW_HEIGHT - 50
            self.move(x, y)
            logger.info(f"Subtitle window positioned at ({x}, {y}) on screen {screen_idx}")
        else:
            logger.warning(f"Invalid screen_idx: {screen_idx}, using default position")
            self.move(100, 100)  # 默认位置
    
    # ============ 对外接口 ============
    
    @Slot(str, int)
    def show_subtitle(self, text: str, duration: int = 3000):
        """对外接口：显示字幕"""
        # 确保窗口可见
        if not self.isVisible():
            self.show()
        self.subtitle.show_text(text, duration)
    
    @Slot()
    def clear_subtitle(self):
        """立即清除字幕"""
        self.subtitle.clear_subtitle()
    
    @Slot(bool)
    def set_always_on_top(self, enable: bool):
        """动态设置窗口置顶"""
        flags = self.windowFlags()
        if enable:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.show()
        
    def set_click_through(self, enable: bool):
        """动态设置鼠标穿透"""
        if enable:
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        else:
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
