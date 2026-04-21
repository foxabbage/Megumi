# components/memory.py
import asyncio
import logging
import sys
import uuid
from datetime import datetime
import signal
from typing import List, Dict, Any, Optional
from collections import deque
import asyncio
import re

# 添加项目路径
from config import TOP_PATH, MEMORY_PATH, MEMORY_PROMPT, MEMORY_MODEL, DASHSCOPE_API_KEY, HF_API_KEY
sys.path.append(TOP_PATH)

from components.base import VTuberComponent
from core.protocol import Message, ComponentID, MessageType

# 第三方依赖
import lancedb
import pyarrow as pa
from sentence_transformers import SentenceTransformer
from openai import AsyncOpenAI

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("MemoryComponent")


class MemoryComponent(VTuberComponent):
    """
    VTuber 记忆组件
    - 接收 chat_llm 的文本，经 Qwen3.5-flash 处理后存入 LanceDB 向量库
    - 支持语义检索，返回最多5条相关记忆
    - 嵌入模型：shibing624/text2vec-base-chinese（中小型中文模型）
    """
    
    def __init__(
        self,
        component_id: ComponentID = ComponentID.MEMORY,
        core_url: str = "ws://localhost:8025/ws/",
        # LanceDB 配置
        db_path: str = MEMORY_PATH,
        table_name: str = "vtuber",
        # 嵌入模型配置（中小型中文模型）
        embedding_model: str = "shibing624/text2vec-base-chinese",
        # Qwen3.5-flash 配置 (OpenAI 兼容格式)
        qwen_api_key: str = "",
        # 检索配置
        max_results: int = 5,
        similarity_threshold: float = 0.3,
    ):
        super().__init__(component_id, core_url)
        
        # ============ 向量数据库配置 ============
        self.db_path = db_path
        self.table_name = table_name
        self.db = lancedb.connect(db_path)
        self.table = None
        self.history_queue: deque[str] = deque(maxlen=8)
        self._queue_lock = asyncio.Lock()
        self.history_join_sep = "\n---\n"  # 历史拼接分隔符
        self.enable_history = True
        
        # ============ 嵌入模型配置 ============
        self.embedder = SentenceTransformer(embedding_model, token=HF_API_KEY)
        self.embedding_dim = self.embedder.get_sentence_embedding_dimension()
        self._ensure_table()
        
        # ============ Qwen 客户端配置 ============
        self.qwen_client = AsyncOpenAI(
            api_key=qwen_api_key,
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1"
        )
        
        # ============ 检索参数 ============
        self.max_results = max_results
        self.similarity_threshold = similarity_threshold
        
        # ============ 注册消息处理器 ============
        self.register_handler(MessageType.QUERY, self._handle_query)      # 查询记忆
        self.register_handler(MessageType.STREAM_DATA, self._handle_store)  # 存储记忆
        self.register_handler(MessageType.COMMAND, self._handle_command)  # 控制命令
        
        logger.info(f"MemoryComponent [{component_id}] initialized")

    def _ensure_table(self):
        """确保 LanceDB 表存在，不存在则创建"""
        try:
            self.table = self.db.open_table(self.table_name)
            logger.info(f"Opened table: {self.table_name}")
        except:
            schema = pa.schema([
                pa.field("id", pa.string()),
                pa.field("text", pa.string()),
                pa.field("vector", pa.list_(pa.float32(), self.embedding_dim)),
                pa.field("timestamp", pa.timestamp("us")),
                pa.field("metadata", pa.string()),  # JSON 字符串
            ])
            self.table = self.db.create_table(self.table_name, schema=schema)
            logger.info(f"Created table: {self.table_name}")

    def _embed(self, text: str) -> List[float]:
        """文本 -> 向量"""
        embedding = self.embedder.encode(text, convert_to_numpy=True, normalize_embeddings=True)
        return embedding.tolist()

    async def _process_memory(self, text: list) -> list:
        """
        使用 Qwen3.5-flash 处理文本，支持5条历史上下文拼接
        - 队列 < 5 条：直接处理当前文本
        - 队列 = 5 条：前4条拼接为历史 + 第5条(当前) 作为输入
        - 无额外提示词，纯文本拼接
        """
        async with self._queue_lock:
            self.history_queue.extend(text)
            
            history_list = list(self.history_queue)
            
            return await self._process_multi(history_list)
        
    async def _process_multi(self, text_list:list) -> list:
        """
        单文本处理：调用 Qwen3.5-flash（OpenAI 兼容格式）
        无系统提示词，纯文本输入 -> 摘要输出
        """
        try:
            response = await self.qwen_client.chat.completions.create(
                model=MEMORY_MODEL,
                messages=[{"role": "system", "content": MEMORY_PROMPT}]+text_list,
                temperature=0.1,      # 降低随机性，保证摘要稳定性
                max_tokens=256,       # 控制输出长度
                stream=False,
                extra_body={"enable_thinking": False}
            )
            llm_output = response.choices[0].message.content.strip()
            if not llm_output:
                return []
            memories = re.findall(r'\{(.*?)\}', llm_output)
            if not memories and llm_output.strip():
                lines = [line.strip() for line in llm_output.split('\n') if line.strip() and not line.startswith('[')]
                if lines:
                    memories = lines
            return memories
        except Exception as e:
            logger.warning(f"Qwen processing failed: {e}, using original text")
            return [text_list[-1]["content"]]

    async def _handle_store(self, msg: Message):
        """
        处理存储请求：接收 chat_llm 发送的文本 -> Qwen处理 -> 向量化 -> 存入 LanceDB
        """
        try:
            # 解析输入文本
            payload = msg.payload if isinstance(msg.payload, dict) else {"text": str(msg.payload)}
            raw_text = payload.get("text", [])
            if not raw_text:
                return
            
            # Step 1: Qwen 预处理（摘要/关键信息提取）
            processed_texts = await self._process_memory(raw_text)
            
            # Step 2: 向量化
            for processed_text in processed_texts:
                vector = self._embed(processed_text)
                
                # Step 3: 构建记录
                record = {
                    "id": str(uuid.uuid4()),
                    "text": processed_text,
                    "vector": vector,
                    "timestamp": datetime.now()
                }
                
                # Step 4: 写入 LanceDB
                self.table.add([record])
                
                logger.info(f"Stored: {processed_text[:40]}...")
            
        except Exception as e:
            logger.error(f"Store error: {e}")

    async def _handle_query(self, msg: Message):
        """
        处理查询请求：语义检索最近的最多5个相关文本
        返回格式：{"texts": [...], "count": int}
        """
        try:
            payload = msg.payload
            query_text = payload.get("query", "").strip()
            
            # 执行检索
            if query_text:
                # 向量相似度搜索
                query_vec = self._embed(query_text)
                results = (self.table
                          .search(query_vec)
                          .limit(self.max_results)
                          .to_list())
            else:
                # 无查询词时返回最新记录
                results = (self.table
                          .search()
                          .limit(self.max_results)
                          .to_list())
            
            # 格式化输出
            texts = [r["text"] for r in results]
            timestamps = [r["timestamp"] for r in results]
            
            response = {
                "texts": texts, # List[str]
                "count": len(texts),  # 按要求返回文本数量
                "timestamps": timestamps
            }
            
            await self.send_message(
                ComponentID.CHAT_LLM,
                MessageType.RESPONSE,
                response,
                msg.trace_id
            )
            
        except Exception as e:
            logger.error(f"Query error: {e}")

    async def _handle_command(self, msg: Message):
        """处理控制命令：clear / count / status"""
        try:
            payload = msg.payload if isinstance(msg.payload, dict) else {}
            cmd = payload.get("command", "").lower()
            
            if cmd == "clear":
                if self.table_name in self.db.list_tables():
                    self.db.drop_table(self.table_name)
                    self.table = None
                    resp = {"status": "success", "message": "memory cleared"}
                else:
                    resp = {"status": "success", "message": "no data to clear"}
                    
            elif cmd == "count":
                cnt = self.table.count_rows()
                resp = {"status": "success", "count": cnt}
                
            elif cmd == "status":
                cnt = self.table.count_rows() if self.table else 0
                resp = {
                    "status": "online",
                    "component": self.component_id.value,
                    "embedding_model": "shibing624/text2vec-base-chinese",
                    "vector_dim": self.embedding_dim,
                    "stored_count": cnt
                }
            else:
                resp = {"status": "unknown_command", "command": cmd}
            
            await self.send_message(
                target=msg.source,
                msg_type=MessageType.RESPONSE,
                payload=resp,
                trace_id=msg.trace_id
            )
            
        except Exception as e:
            logger.error(f"Command error: {e}")
            await self.send_message(
                target=msg.source,
                msg_type=MessageType.ERROR,
                payload={"error": str(e)},
                trace_id=msg.trace_id
            )

    async def start(self):
        """启动组件"""
        logger.info("MemoryComponent starting...")
        await super().start()

    def stop(self):
        """优雅停止"""
        logger.info("MemoryComponent stopping...")
        if self.db:
            del self.db
        if self.embedder:
            del self.embedder
        self.is_running = False


# ============ 独立运行入口（调试用） ============
def main():
    component = MemoryComponent(
        qwen_api_key=DASHSCOPE_API_KEY,
        db_path=MEMORY_PATH
    )
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    def shutdown_handler(sig, frame):
        loop.call_soon_threadsafe(loop.stop)
        # 不要直接 sys.exit()，让 asyncio 循环自然结束
    
    signal.signal(signal.SIGBREAK, shutdown_handler)  # Ctrl+Break
    signal.signal(signal.SIGINT, shutdown_handler)
    try:
        loop.create_task(component.start())
        loop.run_forever()
    except KeyboardInterrupt:
        component.stop()
        loop.close()


if __name__ == "__main__":
    main()