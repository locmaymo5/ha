"""
Script mở browser với state đã login để thao tác thủ công.
Dùng để xóa phiên đăng nhập và các thao tác cần làm tay.
Mở 2 browser cùng lúc để tiết kiệm thời gian load.
"""
import sys
import os
import asyncio
import argparse
import glob

sys.path.insert(0, '.')
from camoufox.async_api import AsyncCamoufox
from config import config

parser = argparse.ArgumentParser(
    description="Mở browser với state đã login để thao tác thủ công (2 browser song song)",
    formatter_class=argparse.ArgumentDefaultsHelpFormatter
)

parser.add_argument('--email', type=str, help='Email cụ thể để mở (optional)', default=None)
parser.add_argument('--states-dir', type=str, help='Thư mục chứa các file state', default=None)
parser.add_argument('--url', type=str, help='URL mở đầu tiên', default='https://myaccount.google.com')

args = parser.parse_args()

# Global browser instance
browser = None

async def create_browser_context(state_path, start_url):
    """Tạo context và page từ state"""
    global browser
    
    email = os.path.basename(state_path).replace('.json', '')
    
    context = await browser.new_context(
        storage_state=state_path,
        locale='vi-VN',
        timezone_id='Asia/Ho_Chi_Minh'
    )
    
    page = await context.new_page()
    
    # Mở URL (không đợi)
    print(f"[{email}] Đang load: {start_url}")
    await page.goto(start_url)
    print(f"[{email}] ✓ Đã load xong!")
    
    return email, context, page

async def wait_for_user_input(email):
    """Đợi người dùng nhấn Enter"""
    print(f"\n>>> [{email}] Nhấn Enter khi hoàn tất thao tác...")
    await asyncio.get_event_loop().run_in_executor(None, input)

async def main():
    global browser
    
    states_dir = args.states_dir or config.StatesDir
    
    if args.email:
        # Mở 1 email cụ thể - không cần pipeline
        state_file = os.path.join(states_dir, f"{args.email}.json")
        if not os.path.exists(state_file):
            print(f"Không tìm thấy state cho email: {args.email}")
            return
        state_files = [state_file]
    else:
        # Lấy tất cả state files
        state_files = glob.glob(os.path.join(states_dir, "*.json"))
        if not state_files:
            print(f"Không tìm thấy file state nào trong {states_dir}")
            return
    
    print(f"Tìm thấy {len(state_files)} states.")
    print(f"Sẽ mở 2 browser cùng lúc theo kiểu pipeline.")
    print(f"URL mặc định: {args.url}")
    print("=" * 60)
    
    proxy = None
    if config.Proxy:
        proxy = {
            "server": config.Proxy.server,
            "username": config.Proxy.username,
            "password": config.Proxy.password,
        }
    
    async with AsyncCamoufox(
        main_world_eval=True,
        headless=False,  # LUÔN HIỂN THỊ
        proxy=proxy,
        geoip=True if proxy else False,
    ) as br:
        browser = br
        
        # Danh sách contexts đang mở
        active_contexts = []  # [(email, context, page), ...]
        current_index = 0
        
        # Mở 2 browser đầu tiên
        print("\n[INIT] Đang mở 2 browser đầu tiên...")
        
        for i in range(min(2, len(state_files))):
            state_file = state_files[i]
            email, context, page = await create_browser_context(state_file, args.url)
            active_contexts.append((email, context, page))
            current_index = i + 1
        
        print("\n" + "=" * 60)
        print("ĐÃ SẴN SÀNG! Thao tác trên browser và nhấn Enter khi xong.")
        print("Browser tiếp theo sẽ được load sẵn trong khi bạn thao tác.")
        print("=" * 60)
        
        # Xử lý lần lượt
        while active_contexts:
            # Lấy browser đầu tiên trong queue
            email, context, page = active_contexts.pop(0)
            
            print(f"\n[ACTIVE] >>> Đang xử lý: {email}")
            print(f"[QUEUE] Còn {len(active_contexts)} browser đang chờ")
            
            # Hiển thị thông tin
            remaining = len(state_files) - current_index
            print(f"[INFO] Đã xử lý: {current_index - len(active_contexts) - 1}/{len(state_files)}, Còn lại: {remaining + len(active_contexts) + 1}")
            
            # Đợi người dùng thao tác xong
            await wait_for_user_input(email)
            
            # Đóng context hiện tại
            print(f"[{email}] Đang đóng...")
            await context.close()
            print(f"[{email}] ✓ Đã đóng!")
            
            # Mở browser tiếp theo (nếu còn)
            if current_index < len(state_files):
                state_file = state_files[current_index]
                next_email, next_context, next_page = await create_browser_context(state_file, args.url)
                active_contexts.append((next_email, next_context, next_page))
                current_index += 1
        
        print("\n" + "=" * 60)
        print("HOÀN TẤT TẤT CẢ!")
        print(f"Đã xử lý {len(state_files)} accounts.")
        print("=" * 60)

if __name__ == "__main__":
    asyncio.run(main())
