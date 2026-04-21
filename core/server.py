# core/server.py
import asyncio
import json
import logging
from typing import Dict, Set, Optional, Any
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from protocol import Message, ComponentID, MessageType
import os

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("VTuberCore")

app = FastAPI(title="AI VTuber Core", version="1.0.0")

# ==================== 静态文件配置 ====================
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# ==================== 连接管理器 ====================
class ConnectionManager:
    def __init__(self):
        # 组件连接：{ component_id: websocket }
        self.active_components: Dict[ComponentID, WebSocket] = {}
        # 监控面板连接
        self.dashboard_clients: Set[WebSocket] = set()
        # 组件状态信息
        self.component_status: Dict[ComponentID, dict] = {}
        # 消息日志（最近 100 条）
        self.message_log: list = []
        self.max_log_size = 100
        # 启动时间
        self.start_time = datetime.now()

    def add_log(self, source: str, target: str, msg_type: str, payload: Any = None):
        """添加消息日志"""
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "source": source,
            "target": target,
            "type": msg_type,
            "payload": str(payload)[:100] if payload else None  # 截断过长 payload
        }
        self.message_log.insert(0, log_entry)
        if len(self.message_log) > self.max_log_size:
            self.message_log.pop()

    async def connect_component(self, websocket: WebSocket, component_id: ComponentID):
        """组件连接"""
        await websocket.accept()
        self.active_components[component_id] = websocket
        self.update_status(component_id, "online")
        await self.broadcast_dashboard()
        
        # 通知新组件当前系统状态
        await self.send_personal_message(
            websocket, 
            Message(
                source=ComponentID.CORE,
                target=component_id,
                type=MessageType.STATUS_REPORT,
                payload={
                    "event": "welcome",
                    "active_components": list(self.active_components.keys()),
                    "system_start_time": self.start_time.isoformat()
                }
            )
        )
        
        logger.info(f"✅ Component Connected: {component_id}")

    def disconnect_component(self, component_id: ComponentID):
        """组件断开"""
        if component_id in self.active_components:
            del self.active_components[component_id]
        self.update_status(component_id, "offline")
        asyncio.create_task(self.broadcast_dashboard())
        logger.warning(f"❌ Component Disconnected: {component_id}")

    async def connect_dashboard(self, websocket: WebSocket):
        """监控面板连接"""
        await websocket.accept()
        self.dashboard_clients.add(websocket)
        await self.send_dashboard_snapshot(websocket)
        logger.info(f"📊 Dashboard Connected: {websocket.client}")

    def disconnect_dashboard(self, websocket: WebSocket):
        """监控面板断开"""
        self.dashboard_clients.discard(websocket)
        logger.info(f"📊 Dashboard Disconnected")

    def update_status(self, component_id: ComponentID, status: str, details: dict = None):
        """更新组件状态"""
        if component_id not in self.component_status:
            self.component_status[component_id] = {
                "component_id": component_id,
                "status": "offline",
                "connected_at": None,
                "last_seen": None,
                "last_msg_type": None,
                "message_count": 0
            }
        
        now = datetime.now()
        self.component_status[component_id].update({
            "status": status,
            "last_seen": now.isoformat(),
            **(details or {})
        })
        
        if status == "online" and self.component_status[component_id]["connected_at"] is None:
            self.component_status[component_id]["connected_at"] = now.isoformat()

    async def send_personal_message(self, websocket: WebSocket, message: Message):
        """发送消息给特定连接"""
        try:
            await websocket.send_json(message.model_dump(mode="json"))
        except Exception as e:
            logger.error(f"Failed to send message: {e}")

    async def send_dashboard_snapshot(self, websocket: WebSocket):
        """发送当前所有组件状态的快照"""
        payload = {
            "type": "snapshot",
            "data": {
                "components": {
                    cid: info for cid, info in self.component_status.items()
                },
                "system": {
                    "start_time": self.start_time.isoformat(),
                    "uptime_seconds": (datetime.now() - self.start_time).total_seconds(),
                    "active_components_count": len(self.active_components),
                    "dashboard_clients_count": len(self.dashboard_clients)
                },
                "logs": self.message_log[:20]  # 发送最近 20 条日志
            }
        }
        await websocket.send_json(payload)

    async def broadcast_dashboard(self):
        """向所有监控面板推送最新状态"""
        if not self.dashboard_clients:
            return
        
        payload = {
            "type": "update",
            "data": {
                "components": {
                    cid: info for cid, info in self.component_status.items()
                },
                "system": {
                    "active_components_count": len(self.active_components),
                    "timestamp": datetime.now().isoformat()
                }
            }
        }
        
        disconnected = []
        for client in self.dashboard_clients:
            try:
                await client.send_json(payload)
            except Exception:
                disconnected.append(client)
        
        # 清理断开的 dashboard 连接
        for client in disconnected:
            self.disconnect_dashboard(client)

    async def route_message(self, message: Message):
        """核心消息路由"""
        # 记录日志
        if message.type != MessageType.HEARTBEAT:
            self.add_log(
                source=message.source,
                target=message.target,
                msg_type=message.type,
                payload=message.payload
            )
        
        # 更新发送者状态（心跳）
        if message.source in self.active_components:
            self.update_status(
                message.source, 
                "online", 
                {
                    "last_msg_type": message.type,
                    "message_count": self.component_status[message.source].get("message_count", 0) + 1
                }
            )
            # 定期广播状态更新（避免过于频繁）
            await self.broadcast_dashboard()

        # 处理发给 Core 的消息
        if message.target == ComponentID.CORE:
            await self.handle_core_request(message)
            return

        # 转发给目标组件
        if message.target in self.active_components:
            ws = self.active_components[message.target]
            try:
                await self.send_personal_message(ws, message)
            except Exception as e:
                logger.error(f"Failed to forward message to {message.target}: {e}")
                self.disconnect_component(message.target)
        else:
            logger.warning(f"⚠️ Target {message.target} not found, message dropped")
            # 可选：返回错误给发送者
            await self.send_error_message(message.source, f"Target {message.target} is offline")

    async def handle_core_request(self, message: Message):
        """处理组件向 Core 发起的请求"""
        action = message.payload.get("action") if isinstance(message.payload, dict) else None

        try:
            if action == "get_status":
                # 获取所有组件状态
                response_payload = {
                    "action": "get_status",
                    "components": {
                        cid: info for cid, info in self.component_status.items()
                    },
                    "system": {
                        "uptime_seconds": (datetime.now() - self.start_time).total_seconds(),
                        "active_count": len(self.active_components)
                    }
                }
                
            elif action == "get_component_info":
                # 获取特定组件信息
                target_id = message.payload.get("component_id")
                if target_id in self.component_status:
                    response_payload = {
                        "action": "get_component_info",
                        "component_id": target_id,
                        "info": self.component_status[target_id]
                    }
                else:
                    response_payload = {
                        "action": "get_component_info",
                        "error": f"Component {target_id} not found"
                    }
                    
            elif action == "list_components":
                # 列出所有在线组件
                response_payload = {
                    "action": "list_components",
                    "online": list(self.active_components.keys()),
                    "all": list(self.component_status.keys())
                }
                
            elif action == "get_logs":
                # 获取消息日志
                limit = message.payload.get("limit", 20)
                response_payload = {
                    "action": "get_logs",
                    "logs": self.message_log[:limit]
                }
                
            elif action == "broadcast":
                # 广播消息给所有组件
                broadcast_msg = message.payload.get("message")
                if broadcast_msg:
                    for cid, ws in self.active_components.items():
                        if cid != message.source:  # 不发给发送者
                            await self.send_personal_message(
                                ws,
                                Message(
                                    source=ComponentID.CORE,
                                    target=cid,
                                    type=MessageType.COMMAND,
                                    payload=broadcast_msg
                                )
                            )
                    response_payload = {
                        "action": "broadcast",
                        "success": True,
                        "sent_to": len(self.active_components) - 1
                    }
                else:
                    response_payload = {"action": "broadcast", "error": "No message provided"}
                    
            elif action == "restart_component":
                # 请求重启某个组件（需要 launcher 配合）
                target_id = message.payload.get("component_id")
                response_payload = {
                    "action": "restart_component",
                    "component_id": target_id,
                    "note": "Restart request sent to launcher (if implemented)"
                }
                logger.warning(f"🔄 Restart requested for {target_id}")
                
            else:
                response_payload = {
                    "action": action,
                    "error": f"Unknown action: {action}",
                    "available_actions": [
                        "get_status", "get_component_info", "list_components",
                        "get_logs", "broadcast", "restart_component"
                    ]
                }
            
            # 发送响应
            response = Message(
                source=ComponentID.CORE,
                target=message.source,
                type=MessageType.STATUS_REPORT,
                payload=response_payload,
                trace_id=message.trace_id
            )
            if message.source in self.active_components:
                await self.send_personal_message(
                    self.active_components[message.source], 
                    response
                )
                
        except Exception as e:
            logger.error(f"Error handling core request: {e}")
            await self.send_error_message(message.source, f"Core request failed: {str(e)}")

    async def send_error_message(self, target: ComponentID, error_msg: str):
        """发送错误消息"""
        if target in self.active_components:
            error = Message(
                source=ComponentID.CORE,
                target=target,
                type=MessageType.ERROR,
                payload={"error": error_msg}
            )
            await self.send_personal_message(self.active_components[target], error)

manager = ConnectionManager()

@app.websocket("/ws/dashboard")
async def dashboard_websocket(websocket: WebSocket):
    """监控面板连接端点"""
    await manager.connect_dashboard(websocket)
    try:
        while True:
            # 保持连接，前端通常只接收不发送
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect_dashboard(websocket)
    except Exception as e:
        logger.error(f"Dashboard connection error: {e}")
        manager.disconnect_dashboard(websocket)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8025","http://localhost:8025"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.websocket("/ws/{component_id}")
async def component_websocket(websocket: WebSocket, component_id: str):
    """组件连接端点"""
    try:
        c_id = ComponentID(component_id)
    except ValueError:
        logger.warning(f"Invalid component_id: {component_id}")
        await websocket.close(code=1008, reason="Invalid component ID")
        return
    
    await manager.connect_component(websocket, c_id)
    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg_dict = json.loads(data)
                message = Message(**msg_dict)
                await manager.route_message(message)
            except json.JSONDecodeError as e:
                logger.error(f"Invalid JSON: {e}")
            except Exception as e:
                logger.error(f"Message processing error: {e}")
                await manager.send_error_message(c_id, f"Message processing failed: {str(e)}")
    except (WebSocketDisconnect, ConnectionResetError) as e:
        logger.info(f"Component disconnected normally: {c_id} ({type(e).__name__})")
        manager.disconnect_component(c_id)
    except Exception as e:
        logger.error(f"Component connection error: {e}")
        manager.disconnect_component(c_id)

# ==================== HTTP 端点 ====================
@app.get("/", response_class=HTMLResponse)
async def read_root():
    """首页重定向到 dashboard"""
    try:
        with open("static/dashboard.html", "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return HTMLResponse(
            "<h1>AI VTuber Core</h1><p>Dashboard not found. Please create static/dashboard.html</p>",
            status_code=404
        )

@app.get("/health")
async def health_check():
    """健康检查接口"""
    return {
        "status": "ok",
        "uptime_seconds": (datetime.now() - manager.start_time).total_seconds(),
        "active_components": list(manager.active_components.keys()),
        "dashboard_clients": len(manager.dashboard_clients)
    }

@app.get("/api/status")
async def get_status():
    """获取所有组件状态（REST API）"""
    return {
        "components": manager.component_status,
        "system": {
            "start_time": manager.start_time.isoformat(),
            "uptime_seconds": (datetime.now() - manager.start_time).total_seconds()
        }
    }

@app.get("/api/logs")
async def get_logs(limit: int = 20):
    """获取消息日志（REST API）"""
    return {"logs": manager.message_log[:limit]}

# ==================== 启动入口 ====================
if __name__ == "__main__":
    import uvicorn
    print(f"WebSocket: ws://localhost:8025/ws/{{component_id}}")
    print("Dashboard: http://localhost:8025")
    print("Health: http://localhost:8025/health")
    uvicorn.run(
        app, 
        host="0.0.0.0", 
        port=8025,
        log_level="info"
    )