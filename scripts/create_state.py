import sys
import logging
sys.path.insert(0, '.')
import os
import re
import json
import asyncio
import concurrent.futures
import argparse

import aiohttp
from camoufox.async_api import AsyncCamoufox
from playwright.async_api import expect

from config import config


parser = argparse.ArgumentParser(
    description="登录AI Studio 并保存登录状态",
    formatter_class=argparse.ArgumentDefaultsHelpFormatter
)

parser.add_argument(
    '--email', type=str, help='Google 帐号自动填充', metavar='user@example.com',
)

parser.add_argument(
    '--password', type=str, help='Google 帐号密码自动填充(可选)'
)

parser.add_argument(
    '--remote', type=str, help='推送到远程HAGMI实例', metavar='http://127.0.0.1:8000'
)

args = parser.parse_args()

async def login():
    proxy = None
    if config.Proxy:
        proxy = {
            "server": config.Proxy.server,
            "username": config.Proxy.username,
            "password": config.Proxy.password,
        }
    storage_state = None
    if os.path.exists(f'{config.StatesDir}/{args.email}.json'):
        storage_state = f'{config.StatesDir}/{args.email}.json'
    async with AsyncCamoufox(
                main_world_eval=True,
                headless=False,
                proxy=proxy,
                geoip=True if proxy else False,
            ) as browser:
        context = await browser.new_context(storage_state=storage_state)
        page = await context.new_page()
        await page.goto(f'{config.AIStudioUrl}/prompts/new_chat')

        if page.url.startswith('https://accounts.google.com/'):
            print("Vui lòng thực hiện đăng nhập trên trình duyệt (bao gồm cả 2FA nếu có)...")
            if args.email:
                try:
                    await page.locator('input#identifierId').fill(args.email)
                    await page.locator('#identifierNext button').click()
                except:
                    pass

        # 1. Đợi cho đến khi URL chuyển sang trang prompts
        print("Đang đợi chuyển hướng vào AI Studio...")
        await page.wait_for_url(re.compile(rf"^{config.AIStudioUrl}/prompts/.*"), timeout=0)

        # 2. Đợi trang ổn định một chút để bảng ToS kịp hiện ra (nếu có)
        await page.wait_for_load_state("networkidle")

        # 3. Kiểm tra và xử lý bảng điều khoản (ToS)
        try:
            # Đợi ms-tos-dialog hoặc textarea (dấu hiệu đã vào chat) xuất hiện
            # Cái nào xuất hiện trước thì xử lý cái đó
            print("Đang kiểm tra trạng thái trang (ToS hoặc Chat)...")
            
            # Sử dụng selector để đợi bảng ToS hoặc nội dung trang chat
            # Nếu thấy ms-tos-dialog thì mới xử lý tiếp
            tos_selector = 'ms-tos-dialog'
            chat_selector = 'textarea'
            
            # Đợi 1 trong 2 xuất hiện
            while True:
                if await page.locator(tos_selector).is_visible():
                    print("Phát hiện bảng điều khoản, đang tự động xử lý...")
                    # Tích vào checkbox đầu tiên (bắt buộc)
                    # Sử dụng click vào label hoặc input đều được
                    checkbox = page.locator('mat-checkbox#mat-mdc-checkbox-0')
                    await checkbox.click()
                    
                    # Đợi nút Continue sáng lên và bấm
                    continue_button = page.get_by_role("button", name=re.compile(r"Continue|Accept|Agree", re.IGNORECASE))
                    await expect(continue_button).to_be_enabled(timeout=5000)
                    await continue_button.click()
                    print("Đã tự động nhấn Continue.")
                    # Đợi cho bảng biến mất hẳn
                    await page.wait_for_selector(tos_selector, state="hidden")
                    break
                
                if await page.locator(chat_selector).is_visible():
                    print("Đã vào thẳng trang chat, không thấy bảng ToS.")
                    break
                
                await asyncio.sleep(1) # Check mỗi giây
        except Exception as e:
            print(f"Lưu ý: Có thể cần thao tác tay nếu bảng điều khoản vẫn hiện: {e}")

        # 4. Đợi thành phần chính của trang chat hiện ra chắc chắn
        await page.wait_for_selector('textarea', timeout=30000)
        print("Đã đăng nhập thành công! Đang lưu state...")

        # Thử lấy email từ dữ liệu trang web
        regex = re.compile(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$')
        emails = []
        for _ in range(15):
            # global_data = await page.evaluate('mw:window.WIZ_global_data')
            global_data = json.loads(await page.locator('script[type="application/json"]').text_content())
            if not global_data:
                await asyncio.sleep(1)
                continue
            emails = [v for v in (global_data).values() if isinstance(v, str) and regex.match(v)]
        auto_email = emails[0] if len(emails) == 1 else None
        email = args.email
        if not email and auto_email:
            email = auto_email
        loop = asyncio.get_running_loop()
        while not email:
            email = await loop.run_in_executor(
                concurrent.futures.ThreadPoolExecutor(),
                input,
                '无法自动获取帐号, 请输入帐号以保存登录状态:'
            )

        state_path = f'{config.StatesDir}/{email}.json'
        await context.storage_state(path=state_path)
        if args.remote:
            async with aiohttp.ClientSession() as session:
                url = f'{args.remote}/admin/upload_state'
                with open(state_path, 'rb') as f:
                    data = aiohttp.FormData()
                    data.add_field(
                        'state',
                        f,
                        filename=f'{email}.json',
                        content_type='application/octet-stream'
                    )
                    async with session.post(url, params={'key': config.AuthKey}, data=data) as resp:
                        print(f"Status: {resp.status}")
                        print(f"Response: {await resp.text()}")


asyncio.run(login())