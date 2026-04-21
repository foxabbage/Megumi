# components/stt.py
import asyncio
import threading
import queue
import logging
import pyaudio
import dashscope
import os
import base64

# 使用新的 OmniRealtimeConversation
from dashscope.audio.qwen_omni import OmniRealtimeConversation, OmniRealtimeCallback, MultiModality
from dashscope.audio.qwen_omni.omni_realtime import TranscriptionParams

from config import TOP_PATH, DASHSCOPE_API_KEY
import sys
sys.path.append(TOP_PATH)

from core.protocol import Message, ComponentID, MessageType
from components.base import VTuberComponent

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("STTComponent")

# ========== 音频参数配置 ==========
SAMPLE_RATE = 16000           # 采样率 (Hz)
CHANNELS = 1                  # 单声道
BLOCK_SIZE = 3200             # 每块帧数 (~200ms @ 16kHz)
AUDIO_FORMAT = pyaudio.paInt16


class STTCallback(OmniRealtimeCallback):
    """
    Qwen-ASR-Realtime 回调处理器
    识别结果通过 queue 传递给主协程
    """
    
    def __init__(self, result_queue: queue.Queue):
        super().__init__()
        self.result_queue = result_queue
        self._current_stash = ""  # 缓存实时识别的 stash 部分
        
    def on_open(self) -> None:
        logger.info("ASR service connected.")
        
    def on_close(self, close_status_code, close_msg) -> None:
        logger.info(f"ASR service closed: {close_status_code} - {close_msg}")
        
    def on_event(self, message: dict) -> None:
        """处理服务端事件"""
        try:
            event_type = message.get('type')
            
            # 实时识别片段（高频推送，用于展示）
            if event_type == 'conversation.item.input_audio_transcription.text':
                text = message.get('text', '')
                stash = message.get('stash', '')
                self._current_stash = stash  # 更新缓存
                # 可选：处理实时预览
                # preview = text + stash
                
            # 句子识别完成（最终结果）
            elif event_type == 'conversation.item.input_audio_transcription.completed':
                transcript = message.get('transcript', '').strip()
                if transcript:
                    self.result_queue.put(transcript)
                    logger.info(f"Recognized: {transcript}")
                self._current_stash = ""  # 重置缓存
                
            # VAD 检测事件（可选日志）
            elif event_type == 'input_audio_buffer.speech_started':
                logger.debug("Speech started detected")
            elif event_type == 'input_audio_buffer.speech_stopped':
                logger.debug("Speech stopped detected")
                
            # 错误处理
            elif event_type == 'error':
                error_info = message.get('error', {})
                logger.error(f"ASR error: {error_info.get('message', 'Unknown error')}")
                
        except Exception as e:
            logger.error(f"Callback event handling error: {e}")


class STTComponent(VTuberComponent):
    """
    STT 组件：语音转文字 + 消息路由
    使用 Qwen-ASR-Realtime 新接口
    """
    
    def __init__(self, core_url: str = "ws://localhost:8025/ws/"):
        super().__init__(ComponentID.STT, core_url)
        
        # 音频相关资源
        self._audio: pyaudio.PyAudio = None
        self._stream: pyaudio.Stream = None
        self._conversation: OmniRealtimeConversation = None
        self._callback: STTCallback = None
        
        # 线程/协程通信
        self._result_queue = queue.Queue()
        self._is_recording = False
        self._audio_thread: threading.Thread = None
        
        # 注册命令处理器
        self.register_handler(MessageType.COMMAND, self._handle_command)
        
    def _handle_command(self, msg: Message):
        """处理外部控制命令：start/stop"""
        if not isinstance(msg.payload, dict):
            return
        cmd = msg.payload.get("command")
        if cmd == "start":
            asyncio.create_task(self.start_recognition())
        elif cmd == "stop":
            asyncio.create_task(self.stop_recognition())
            
    async def _process_recognition_results(self):
        """协程任务：持续轮询识别结果队列"""
        while self.is_running:
            try:
                text = self._result_queue.get_nowait()
                
                # 步骤1: 通知 TTS 立即停止
                await self.send_message(
                    target=ComponentID.TTS,
                    msg_type=MessageType.COMMAND,
                    payload={"command": "interrupt", "reason": "new_input"}
                )
                
                # 步骤2: 发送识别文本给 CHAT_LLM
                await self.send_message(
                    target=ComponentID.CHAT_LLM,
                    msg_type=MessageType.STREAM_DATA,
                    payload={"text": text}
                )
                logger.info(f"Forwarded to CHAT_LLM: {text}")
                
            except queue.Empty:
                pass
            await asyncio.sleep(0.1)
            
    def _audio_capture_thread(self):
        """独立线程：采集麦克风音频并发送给 ASR 服务"""
        logger.info("Audio capture thread started.")
        
        while self.is_running and self._is_recording and self._stream:
            try:
                data = self._stream.read(BLOCK_SIZE, exception_on_overflow=False)
                if self._conversation:
                    # 新版接口需要 base64 编码的音频
                    audio_b64 = base64.b64encode(data).decode('utf-8')
                    self._conversation.append_audio(audio_b64)
            except OSError as e:
                if "Input overflowed" in str(e):
                    logger.warning("Audio buffer overflow, skipping frame")
                    continue
                logger.error(f"Audio read error: {e}")
                break
            except Exception as e:
                logger.error(f"Unexpected audio error: {e}")
                break
                
        logger.info("Audio capture thread stopped.")
        
    async def start_recognition(self):
        """启动语音识别全流程"""
        if self._is_recording:
            logger.warning("Already recording, skipping start.")
            return
            
        try:
            # 初始化 PyAudio
            self._audio = pyaudio.PyAudio()
            self._stream = self._audio.open(
                format=AUDIO_FORMAT,
                channels=CHANNELS,
                rate=SAMPLE_RATE,
                input=True,
                frames_per_buffer=BLOCK_SIZE,
                start=True
            )
            
            # 初始化回调
            self._callback = STTCallback(self._result_queue)
            
            # 创建 OmniRealtimeConversation 实例 [[1]]
            self._conversation = OmniRealtimeConversation(
                model='qwen3-asr-flash-realtime',
                # 中国内地使用此 URL，国际版用 dashscope-intl
                url='wss://dashscope.aliyuncs.com/api-ws/v1/realtime',
                callback=self._callback
            )

            self._conversation.connect()
            
            # 配置会话参数 [[1]]
            transcription_params = TranscriptionParams(
                language='zh',  # 中文识别
                sample_rate=SAMPLE_RATE,
                input_audio_format="pcm"
            )
            
            self._conversation.update_session(
                output_modalities=[MultiModality.TEXT],
                enable_turn_detection=True,  # 开启服务端 VAD 自动断句
                turn_detection_type="server_vad",
                turn_detection_threshold=0.3,  # 灵敏度
                turn_detection_silence_duration_ms=900,  # 900ms 静音判定断句
                enable_input_audio_transcription=True,
                transcription_params=transcription_params
            )
            
            # 启动音频采集线程
            self._is_recording = True
            self._audio_thread = threading.Thread(
                target=self._audio_capture_thread,
                daemon=True,
                name="STT-AudioCapture"
            )
            self._audio_thread.start()
            
            # 启动结果处理协程
            asyncio.create_task(self._process_recognition_results())
            
            logger.info("STT component started successfully with Qwen-ASR-Realtime.")
            
        except Exception as e:
            logger.error(f"Failed to start recognition: {e}")
            await self.stop_recognition()
            raise
            
    async def stop_recognition(self):
        """优雅停止语音识别，释放所有资源"""
        if not self._is_recording:
            return
            
        logger.info("Stopping STT recognition...")
        self._is_recording = False
        
        # 等待音频采集线程退出
        if self._audio_thread and self._audio_thread.is_alive():
            self._audio_thread.join(timeout=2.0)
            
        # 结束会话并关闭连接
        if self._conversation:
            try:
                # 通知服务端结束会话（等待最后识别完成）
                self._conversation.end_session(timeout=5)
                self._conversation.close()
            except Exception as e:
                logger.error(f"Error stopping ASR conversation: {e}")
            self._conversation = None
            
        # 关闭音频流
        if self._stream:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception as e:
                logger.error(f"Error closing audio stream: {e}")
            self._stream = None
            
        # 终止 PyAudio
        if self._audio:
            try:
                self._audio.terminate()
            except Exception as e:
                logger.error(f"Error terminating PyAudio: {e}")
            self._audio = None
            
        logger.info("STT component stopped.")
        
    async def start(self):
        """组件主入口"""
        if self.is_running:
            logger.warning(f"{self.component_id} already running")
            return
            
        self.is_running = True
        await self.start_recognition()
        self._cleanup_done = False
        
        try:
            await self.connect()
        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.info(f"{self.component_id} interrupted")
        finally:
            # 【关键】确保 cleanup 只执行一次
            await self._safe_cleanup()
        
    def stop(self):
        """外部调用停止组件"""
        super().stop()
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.stop_recognition())
        except RuntimeError:
            asyncio.run(self.stop_recognition())

async def main():
    dashscope.api_key = DASHSCOPE_API_KEY
    # 注意：OmniRealtimeConversation 使用 url 参数指定端点，无需设置 base_websocket_api_url
    stt = STTComponent()
    try:
        await stt.start()
    except KeyboardInterrupt:
        stt.stop()

if __name__ == "__main__":
    asyncio.run(main())