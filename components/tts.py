# components/tts.py
import asyncio
import threading
import base64
import logging
import time
import sys
from typing import Optional, Any
from concurrent.futures import ThreadPoolExecutor

import pyaudio
import dashscope
from dashscope.audio.qwen_tts_realtime import (
    QwenTtsRealtime, 
    QwenTtsRealtimeCallback, 
    AudioFormat
)

from config import TOP_PATH, DASHSCOPE_API_KEY
sys.path.append(TOP_PATH)

from components.base import VTuberComponent
from core.protocol import Message, ComponentID, MessageType

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("TTSComponent")


class TTSCallback(QwenTtsRealtimeCallback):
    """自定义 TTS 流式回调，负责音频播放和状态通知"""
    
    def __init__(self, component: 'TTSComponent'):
        super().__init__()
        self.component = component
        self.complete_event = threading.Event()
        self.interrupt_event = threading.Event()
        self._player: Optional[pyaudio.PyAudio] = None
        self._stream: Optional[pyaudio.Stream] = None
        self._v_stream: Optional[pyaudio.Stream] = None
        self._lock = threading.Lock()
        
    def _ensure_stream(self):
        """确保音频流已打开（线程安全）"""
        with self._lock:
            if self._stream is None and self._player is not None:
                self._stream = self._player.open(
                    format=pyaudio.paInt16,
                    channels=1,
                    rate=24000,
                    output=True,
                    frames_per_buffer=1024
                )
            if self._v_stream is None and self._player is not None:
                self._v_stream = self._player.open(
                    format=pyaudio.paInt16,
                    channels=1,
                    rate=24000,
                    output=True,
                    frames_per_buffer=1024,
                    output_device_index=7
                ) 
    
    def on_open(self) -> None:
        logger.info('[TTS] WebSocket 连接已建立')
        # 初始化 PyAudio
        self._player = pyaudio.PyAudio()
        
    def on_close(self, close_status_code: int, close_msg: str) -> None:
        self._cleanup_audio()
        logger.info(f'[TTS] 连接关闭 code={close_status_code}, msg={close_msg}')
        self.complete_event.set()
        
    def on_event(self, response: dict) -> None:
        """处理 TTS 事件"""
        try:
            event_type = response.get('type', '')
            
            if event_type == 'session.created':
                session_id = response.get("session", {}).get("id", "unknown")
                logger.info(f'[TTS] 会话开始: {session_id}')
                
            elif event_type == 'response.audio.delta':
                # 检查是否被中断
                if self.interrupt_event.is_set():
                    return
                audio_data = base64.b64decode(response['delta'])
                self._ensure_stream()
                if self._stream:
                    try:
                        self._stream.write(audio_data)
                    except Exception as e:
                        logger.warning(f"[TTS] 音频写入异常: {e}")
                if self._v_stream:
                    try:
                        self._v_stream.write(audio_data)
                    except Exception as e:
                        logger.warning(f"[TTS] 音频写入异常: {e}")
                        
            elif event_type == 'response.done':
                logger.info('[TTS] 响应生成完成')
                
            elif event_type == 'session.finished':
                logger.info('[TTS] 会话结束')
                self.complete_event.set()
                
        except Exception as e:
            logger.error(f'[TTS] 处理回调事件异常: {e}')
    
    def interrupt(self):
        """中断播放"""
        logger.debug('[TTS] 触发中断')
        self.interrupt_event.set()
        self._cleanup_audio()
        
    def reset(self):
        """重置状态，准备下一次播放"""
        self.complete_event.clear()
        self.interrupt_event.clear()
        with self._lock:
            self._stream = None
            self._v_stream = None
        
    def wait_for_finished(self, timeout: Optional[float] = None) -> bool:
        """
        等待播放完成
        Returns: True=正常完成, False=被中断或超时
        """
        result = self.complete_event.wait(timeout)
        return result and not self.interrupt_event.is_set()
    
    def _cleanup_audio(self):
        """清理音频资源（线程安全）"""
        with self._lock:
            if self._stream:
                try:
                    self._stream.stop_stream()
                    self._stream.close()
                except:
                    pass
                self._stream = None
            if self._v_stream:
                try:
                    self._v_stream.stop_stream()
                    self._v_stream.close()
                except:
                    pass
                self._stream = None
            if self._player:
                try:
                    self._player.terminate()
                except:
                    pass
                self._player = None
    
    def cleanup(self):
        """外部调用的资源清理"""
        self.interrupt()
        self._cleanup_audio()


class TTSComponent(VTuberComponent):
    """
    TTS 组件：接收文本消息进行语音合成并流式播放
    - 监听 CHAT_LLM 的 text_message 进行合成
    - 监听 STT 的 interrupt 命令停止播放
    - 播放完成/中断时向 CHAT_LLM 发送 response
    """
    
    # 配置常量
    TTS_MODEL = "qwen3-tts-vd-realtime-2026-01-15"
    TTS_VOICE = "qwen-tts-vd-vtb-voice-20260413162450096-cefb"
    TTS_URL = 'wss://dashscope.aliyuncs.com/api-ws/v1/realtime'  # 北京地域
    TTS_FORMAT = AudioFormat.PCM_24000HZ_MONO_16BIT
    TTS_MODE = 'server_commit'
    TEXT_CHUNK_SIZE = 50  # 分句发送的字符数
    
    def __init__(self, core_url: str = "ws://localhost:8025/ws/"):
        super().__init__(ComponentID.TTS, core_url)
        
        # 注册消息处理器
        self.register_handler(MessageType.TEXT_MESSAGE, self._handle_text_message)
        self.register_handler(MessageType.COMMAND, self._handle_command)
        
        # 线程池用于运行同步 TTS 代码
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tts_worker")
        
        # TTS 状态管理
        self._current_callback: Optional[TTSCallback] = None
        self._current_task: Optional[asyncio.Task] = None
        self._is_playing = False
        self._state_lock = asyncio.Lock()
    
    def _create_tts_session(self, callback: TTSCallback) -> QwenTtsRealtime:
        """创建并配置 TTS 会话"""
        tts = QwenTtsRealtime(
            model=self.TTS_MODEL,
            callback=callback,
            url=self.TTS_URL
        )
        tts.connect()
        tts.update_session(
            voice=self.TTS_VOICE,
            response_format=self.TTS_FORMAT,
            mode=self.TTS_MODE,
            language_type="Chinese"
        )
        return tts
    
    def _sync_tts_play(self, text: str) -> bool:
        """
        同步执行 TTS 合成和播放（运行在线程池中）
        Returns: True=正常完成, False=被中断或异常
        """
        callback = TTSCallback(self)
        self._current_callback = callback
        
        tts = None
        try:
            # 创建会话
            tts = self._create_tts_session(callback)
            
            # 分块发送文本（模拟流式输入体验）
            for i in range(0, len(text), self.TEXT_CHUNK_SIZE):
                if callback.interrupt_event.is_set():
                    logger.debug("[TTS] 发送过程中被中断")
                    return False
                chunk = text[i:i + self.TEXT_CHUNK_SIZE]
                tts.append_text(chunk)
                time.sleep(0.03)  # 微小延迟，避免阻塞
            
            # 标记文本发送完成
            tts.finish()
            
            # 等待播放完成（带超时保护）
            completed = callback.wait_for_finished(timeout=60)
            return completed
            
        except Exception as e:
            logger.error(f"[TTS] 合成播放异常: {type(e).__name__}: {e}")
            return False
        finally:
            # 确保资源清理
            callback.cleanup()
            if tts:
                try:
                    tts.close()
                except:
                    pass
            self._current_callback = None
    
    async def _handle_text_message(self, msg: Message):
        """处理来自 chat_llm 的文本合成请求"""
        if msg.source != ComponentID.CHAT_LLM:
            return
            
        # 解析文本内容
        payload = msg.payload
        if isinstance(payload, dict):
            text = payload.get("text", "")
        else:
            text = str(payload)
            
        if not text.strip():
            return
            
        logger.info(f"[TTS] 收到合成请求 ({len(text)}chars): {text[:40]}...")
        trace_id = msg.trace_id
        
        async with self._state_lock:
            # 如果正在播放，先中断（新输入打断旧语音）
            if self._is_playing:
                logger.debug("[TTS] 中断当前播放以处理新请求")
                await self._stop_playing()
            
            # 标记开始播放
            self._is_playing = True
        
        # 在线程池中执行同步 TTS 操作
        loop = asyncio.get_running_loop()
        try:
            completed_normally = await loop.run_in_executor(
                self._executor, 
                self._sync_tts_play, 
                text
            )
            status = "completed" if completed_normally else "interrupted"
            await self._send_response(trace_id, status)
            
        except asyncio.CancelledError:
            logger.debug("[TTS] 播放任务被取消")
            await self._send_response(trace_id, "cancelled")
        except Exception as e:
            logger.error(f"[TTS] 任务执行异常: {e}")
            await self._send_response(trace_id, "error", str(e))
        finally:
            async with self._state_lock:
                self._is_playing = False
    
    async def _handle_command(self, msg: Message):
        """处理来自 stt 的命令消息（如打断）"""
        if msg.source != ComponentID.STT:
            return
            
        payload = msg.payload
        if not isinstance(payload, dict):
            return
            
        command = payload.get("command")
        if command == "interrupt":
            reason = payload.get("reason", "unknown")
            logger.info(f"[TTS] 收到中断命令: reason={reason}")
            await self._stop_playing()
            # 发送中断确认响应
            await self._send_response(msg.trace_id, "interrupted", reason)
    
    async def _stop_playing(self):
        """停止当前播放（异步安全）"""
        async with self._state_lock:
            if not self._is_playing:
                return
            self._is_playing = False
        
        # 中断回调中的音频播放
        if self._current_callback:
            self._current_callback.interrupt()
        
        # 取消正在执行的任务
        if self._current_task and not self._current_task.done():
            self._current_task.cancel()
            try:
                await self._current_task
            except asyncio.CancelledError:
                pass
    
    async def _send_response(self, trace_id: Optional[str], status: str, 
                            extra_info: Optional[str] = None):
        """向 chat_llm 发送播放状态响应"""
        payload = {
            "status": status,  # completed | interrupted | cancelled | error
        }
        if extra_info:
            payload["info"] = extra_info
            
        await self.send_message(
            target=ComponentID.CHAT_LLM,
            msg_type=MessageType.RESPONSE,
            payload=payload,
            trace_id=trace_id
        )
        logger.debug(f"[TTS] 发送响应: status={status}, trace_id={trace_id}")
    
    async def start(self):
        """启动组件"""
        logger.info("[TTS] 组件启动，监听消息...")
        await super().start()
    
    def stop(self):
        """停止组件并清理资源"""
        logger.info("[TTS] 组件停止")
        super().stop()
        
        # 停止当前播放
        if self._current_callback:
            self._current_callback.cleanup()
        
        # 关闭线程池
        self._executor.shutdown(wait=False)
    
    async def __aenter__(self):
        await self.start()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.stop()


# ========== 入口 ==========
async def main():
    """开发测试入口"""
    dashscope.api_key = DASHSCOPE_API_KEY
    component = TTSComponent()
    try:
        await component.start()
    except KeyboardInterrupt:
        logger.info("[TTS] 收到退出信号")
    finally:
        component.stop()


if __name__ == "__main__":
    asyncio.run(main())