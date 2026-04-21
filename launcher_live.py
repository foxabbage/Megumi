# launcher.py
import subprocess
import time
import sys
import os
import signal

# 定义所有需要启动的组件脚本
COMPONENTS = [
    {"name": "Core", "script": "core/server.py", "essential": True},
    {"name": "ChatLLM", "script": "components/chat_llm.py", "essential": True},
    {"name": "TTS", "script": "components/tts.py", "essential": True},
    {"name": "STT", "script": "components/stt.py", "essential": True},
    {"name": "Memory", "script": "components/memory.py", "essential": True},
    {"name": "Subtitle", "script": "components/subtitle.py", "essential": True},
    {"name": "VTS", "script": "components/vts.py", "essential": True},
    {"name": "screenshot", "script": "components/screenshot.py", "essential": True}
]

processes = []
shutdown_requested = False

def start_subprocess(script_path):
    """
    跨平台启动子进程，确保进程组隔离
    """
    kwargs = {
        "args": [sys.executable, script_path],
        # 继承环境变量，但可以根据需要修改
        "env": os.environ.copy(), 
    }

    kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        
    return subprocess.Popen(**kwargs)

def terminate_subprocess(p:subprocess.Popen):
    """
    跨平台终止子进程及其子进程
    """
    if p.poll() is not None:
        return # 进程已经结束了

    try:
        p.send_signal(signal.CTRL_BREAK_EVENT)
    except (ProcessLookupError, OSError):
        print("ctrl c failed")
        pass # 进程可能已经消失

def cleanup_processes():
    """
    等待并清理所有进程
    """
    print("\nCleaning up processes...")
    for i in range(len(processes)-1,-1,-1):
        p = processes[i]
        if p.poll() is None: # 如果进程还在运行
            comp_name = COMPONENTS[i]["name"]
            print(f"Stopping {comp_name} (PID: {p.pid})...")
            terminate_subprocess(p)
    
    # 等待所有进程结束，设置超时防止无限等待
    for i, p in enumerate(processes):
        try:
            p.wait(timeout=3) 
        except subprocess.TimeoutExpired:
            comp_name = COMPONENTS[i]["name"]
            print(f"⚠️  {comp_name} did not stop gracefully. Killing...")
            p.kill() # 强制杀死
            p.wait()

def signal_handler(sig, frame):
    global shutdown_requested
    print("\n⚠️  Shutdown signal received (Ctrl+C).")
    shutdown_requested = True

def main():
    global shutdown_requested
    signal.signal(signal.SIGINT, signal_handler)
    # 处理可能的终止信号
    if hasattr(signal, 'SIGTERM'):
        signal.signal(signal.SIGTERM, signal_handler)
    
    # 确保在正确的目录下运行
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    try:
        # 1. 先启动 Core
        print("Starting Core Server...")
        core_proc = start_subprocess(COMPONENTS[0]["script"])
        processes.append(core_proc)
        time.sleep(2) 

        # 2. 启动其他组件
        for comp in COMPONENTS[1:]:
            if shutdown_requested: break
            print(f"Starting {comp['name']}...")
            p = start_subprocess(comp["script"])
            processes.append(p)
            time.sleep(0.5)

        # 3. 监控进程
        while not shutdown_requested:
            restart_needed = False
            
            for i, p in enumerate(processes):
                if p.poll() is not None: # 进程已结束
                    comp_name = COMPONENTS[i]["name"]
                    
                    # 如果是正常关闭期间，不要重启
                    if shutdown_requested:
                        break

                    print(f"⚠️  {comp_name} crashed! (Exit code: {p.returncode})")
                    
                    if COMPONENTS[i]["essential"]:
                        if comp_name == "Core":
                            print("❌ Core crashed. System stopping.")
                            shutdown_requested = True
                            break
                        else:
                            # 非核心组件崩溃，标记重启
                            restart_needed = True
                    else:
                        restart_needed = True

            if restart_needed and not shutdown_requested:
                # 简单的重启逻辑：重启所有崩溃的非核心进程
                # 注意：这里为了简单，实际生产环境可能需要更精细的重启策略
                for i, p in enumerate(processes):
                    if p.poll() is not None:
                        if not COMPONENTS[i]["essential"] or COMPONENTS[i]["name"] != "Core":
                             print(f"🔄 Restarting {COMPONENTS[i]['name']}...")
                             new_p = start_subprocess(COMPONENTS[i]["script"])
                             processes[i] = new_p
                             time.sleep(1)
            
            # 使用较短的睡眠，以便更快响应关闭信号
            time.sleep(1)

    except Exception as e:
        print(f"Launcher error: {e}")
    finally:
        # 无论正常退出还是异常，都执行清理
        cleanup_processes()
        print("✅ All components shut down.")
        sys.exit(0)

if __name__ == "__main__":
    main()