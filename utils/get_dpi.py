# test_click_input.py
import asyncio
import sys
from pathlib import Path
from playwrightgui import PlaywrightComputer


async def get_info():
    # === 配置 ===
    task_dir = "./test_output"  # 输出目录
    test_url = "https://www.google.com"  # 测试页面，可替换为你的目标页面
    # ===========
    
    # 创建输出目录
    Path(task_dir).mkdir(parents=True, exist_ok=True)
    Path(f"{task_dir}/trajectory").mkdir(exist_ok=True)
    Path(f"{task_dir}/trajectory_som").mkdir(exist_ok=True)
    
    # 初始化计算机
    computer = PlaywrightComputer(
        task_dir=task_dir,
        initial_url=test_url,
        highlight_mouse=True  # 开启鼠标高亮便于观察
    )

    await computer.reset()
    await asyncio.sleep(1)  # 等待页面完全加载
    
    viewport_info = await computer._page.evaluate("""() => ({
        innerWidth: window.innerWidth,
        innerHeight: window.innerHeight,
        scrollX: window.scrollX,
        scrollY: window.scrollY,
        dpr: window.devicePixelRatio
    })""")
    print(f"📐 视口: {viewport_info['innerWidth']}x{viewport_info['innerHeight']}")
    print(f"📜 滚动: {viewport_info['scrollX']},{viewport_info['scrollY']}")
    print(f"🔍 DPI: {viewport_info['dpr']}")

if __name__ == "__main__":
    # Windows 系统确保事件循环策略正确
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    
    asyncio.run(get_info())