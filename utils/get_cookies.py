import asyncio
from playwright.async_api import async_playwright

async def create_storage_state():
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=False)  # 非无头模式方便操作
    context = await browser.new_context(storage_state="tokens/storage_state.json")
    page = await context.new_page()
    
    # 👉 手动在浏览器中登录目标网站
    #await page.goto("https://www.bilibili.com/")
    #await page.goto("https://www.github.com/")
    #await page.goto("https://bangumi.tv/")
    #await page.goto("https://chat.qwen.ai/")
    b = await page.evaluate("1")
    print(b)
    print("请在打开的浏览器中完成登录操作，按回车继续...")
    input()  # 等待用户手动操作
    
    # 保存状态到文件
    await context.storage_state(path="tokens/storage_state.json")
    print("✅ storage_state.json 已保存")
    
    await browser.close()
    await playwright.stop()

# 执行一次即可
asyncio.run(create_storage_state())