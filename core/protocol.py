# core/protocol.py
from pydantic import BaseModel, Field, ConfigDict
from typing import Any, Optional, Literal
from datetime import datetime
from enum import Enum
import uuid

class ComponentID(str, Enum):
    CORE = "core"
    CHAT_LLM = "chat_llm"
    STT = "stream_stt"
    TTS = "tts"
    VTS = "vtube_studio"
    PC_LLM = "pc_control_llm"
    MEMORY = "memory"
    SCREENSHOT = "screenshot"
    DANMAKU = "danmaku"
    SUBTITLE = "subtitle"
    
    def __str__(self) -> str:
        return self.value


class MessageType(str, Enum):
    HEARTBEAT = "heartbeat"       # 心跳
    TEXT_MESSAGE = "text"         # 普通文本
    COMMAND = "command"           # 控制指令
    STREAM_DATA = "stream"        # 流式数据
    STATUS_REPORT = "status"      # 状态上报
    ERROR = "error"               # 错误报告
    RESPONSE = "response"         # 回传请求
    QUERY = "query"               # 请求数据
    
    def __str__(self) -> str:
        return self.value


class Message(BaseModel):
    model_config = ConfigDict(
        arbitrary_types_allowed=False,
        use_enum_values=True,
    )
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source: ComponentID
    target: ComponentID
    type: MessageType
    payload: Any
    timestamp: datetime = Field(default_factory=datetime.now)
    
    # 链路追踪
    trace_id: Optional[str] = None 


class Heartbeat(BaseModel):
    model_config = ConfigDict(use_enum_values=True)
    
    component_id: ComponentID
    status: Literal["online", "busy", "error"]
    cpu_usage: float = 0.0
    memory_usage: float = 0.0
