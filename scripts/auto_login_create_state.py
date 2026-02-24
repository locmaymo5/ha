import sys
import logging
sys.path.insert(0, '.')
import os
import re
import json
import asyncio
import concurrent.futures
import argparse
import random

import aiohttp
from camoufox.async_api import AsyncCamoufox
from playwright.async_api import expect, TimeoutError as PlaywrightTimeoutError

from config import config

# --- Cấu hình Argument ---
parser = argparse.ArgumentParser(
    description="Tool tự động đăng nhập AI Studio/Google hàng loạt",
    formatter_class=argparse.ArgumentDefaultsHelpFormatter
)

parser.add_argument('--file', type=str, help='Đường dẫn file txt chứa acc (mail|pass|recovery)', default='accounts.txt')
parser.add_argument('--remote', type=str, help='Push state lên server HAGMI', metavar='http://127.0.0.1:8000')

args = parser.parse_args()

# --- File lưu các email login fail ---
LOGIN_FAIL_FILE = 'login_fail.txt'

def log_login_fail(email, reason=""):
    """Ghi email login fail vào file"""
    with open(LOGIN_FAIL_FILE, 'a', encoding='utf-8') as f:
        if reason:
            f.write(f"{email}|{reason}\n")
        else:
            f.write(f"{email}\n")
    print(f"[{email}] Đã ghi vào {LOGIN_FAIL_FILE}")

# --- Hàm hỗ trợ delay ngẫu nhiên để giống người thật ---
async def random_sleep(min_s=1, max_s=3):
    await asyncio.sleep(random.uniform(min_s, max_s))

async def get_2fa_code(secret):
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://2fa.live/tok/{secret.replace(' ', '')}"
            async with session.get(url) as resp:
                data = await resp.json()
                return data.get("token")
    except Exception as e:
        print(f"Lỗi lấy mã 2FA: {e}")
    return None

# --- Hàm xử lý login cho 1 tài khoản ---
async def process_account(browser, email, password, recovery_email, two_fa_key=None):
    print(f"[{email}] Bắt đầu xử lý...")
    print(f"[{email}] Recovery email: {'Có' if recovery_email else 'Không có'} | 2FA key: {'Có' if two_fa_key else 'Không có'}")
    
    state_path = f'{config.StatesDir}/{email}.json'
    login_success = False  # Track trạng thái login
    
    # Tạo context mới (sạch sẽ, không dính cookie cũ)
    context = await browser.new_context(
        locale='en-US', # Nên để tiếng Anh để dễ bắt selector
        timezone_id='Asia/Ho_Chi_Minh'
    )
    
    try:
        page = await context.new_page()
        
        # 1. Vào trang Login
        await page.goto(f'{config.AIStudioUrl}/prompts/new_chat')
        
        # Chờ chuyển hướng sang Google Login
        try:
            await page.wait_for_url(re.compile(r"^https://accounts\.google\.com/"), timeout=10000)
            print(f"[{email}] Đã chuyển hướng sang trang đăng nhập Google.")
        except:
            # Nếu không chuyển hướng, có thể đã login rồi (nếu load state cũ - nhưng ở đây ta login mới)
            pass

        # 2. Nhập Email
        if await page.locator('input[type="email"]').is_visible():
            print(f"[{email}] Đang nhập email...")
            await page.fill('input[type="email"]', email)
            await random_sleep()
            # Click vào button bên trong div#identifierNext
            next_btn = page.locator('#identifierNext button, #identifierNext')
            await next_btn.first.click(force=True)
        
        # 3. Nhập Password
        try:
            # Đợi input password hiện ra
            await page.wait_for_selector('input[type="password"]', state='visible', timeout=10000)
            print(f"[{email}] Đang nhập password...")
            await random_sleep()
            await page.fill('input[type="password"]', password)
            await random_sleep()
            # Click vào button bên trong div#passwordNext
            pass_btn = page.locator('#passwordNext button, #passwordNext')
            await pass_btn.first.click(force=True)
        except PlaywrightTimeoutError:
            print(f"[{email}] Không thấy chỗ nhập pass (Có thể mail sai hoặc cần xác minh khác).")

        # 3.5. Xử lý màn hình xác minh qua App (Mobile Prompt) hoặc Danh sách lựa chọn 2FA
        # LOGIC: Chọn phương thức dựa trên những gì CÓ TRÊN MÀN HÌNH và trong txt
        await random_sleep(3, 5)
        verification_done = False  # Đánh dấu đã xử lý xác minh chưa
        
        try:
            # Trường hợp 1: Có nút "Thử cách khác" (Try another way)
            try_another_way_selector = 'button:has-text("Thử cách khác"), button:has-text("Try another way")'
            if await page.locator(try_another_way_selector).is_visible(timeout=3000):
                print(f"[{email}] Phát hiện nút 'Thử cách khác'. Clicking...")
                await page.click(try_another_way_selector)
                await random_sleep(3, 4)

            # Selector cho các phương thức
            recovery_option_selector = 'div[role="link"][data-challengetype="12"]'
            # Authenticator: type 6 VÀ không phải "Try another way" (data-accountrecovery="false")
            auth_option_selector = 'div[role="link"][data-challengetype="6"][data-accountrecovery="false"]'
            
            recovery_locator = page.locator(recovery_option_selector)
            auth_locator = page.locator(auth_option_selector)
            
            # Dùng count() thay vì is_visible() để tránh strict mode error
            recovery_count = await recovery_locator.count()
            auth_count = await auth_locator.count()
            recovery_visible = recovery_count > 0
            auth_visible = auth_count > 0
            
            print(f"[{email}] Options trên màn hình: Recovery={recovery_visible}, Authenticator={auth_visible}")
            
            # LOGIC CHỌN PHƯƠNG THỨC:n
            # 1. Nếu có recovery_email VÀ có option recovery trên màn hình -> chọn recovery
            if recovery_email and recovery_visible:
                print(f"[{email}] Chọn 'Confirm recovery email'...")
                await recovery_locator.click()
                await random_sleep(2, 3)
                
                # Điền recovery email
                input_selector = 'input[name="knowledgePreregisteredEmailResponse"], input[type="email"], input[aria-label*="recovery"], input[aria-label*="email"]'
                try:
                    await page.wait_for_selector(input_selector, state='visible', timeout=10000)
                    print(f"[{email}] Đang nhập recovery email: {recovery_email}")
                    target_input = page.locator(input_selector).first
                    await target_input.fill(recovery_email)
                    await random_sleep(0.5, 1)
                    await page.keyboard.press('Enter')
                    print(f"[{email}] Đã submit recovery email.")
                    verification_done = True
                except Exception as e:
                    print(f"[{email}] Lỗi khi nhập recovery email: {e}")
                    log_login_fail(email, f"Lỗi nhập recovery email: {e}")
            
            # 2. Nếu có 2fa_key VÀ có option Authenticator trên màn hình -> chọn Authenticator
            elif two_fa_key and auth_visible:
                print(f"[{email}] Chọn 'Google Authenticator'...")
                await auth_locator.first.click()
                verification_done = True  # Sẽ xử lý nhập OTP ở bước 4
            
            # 3. Có recovery email nhưng KHÔNG có option recovery, VÀ có 2fa_key VÀ có Authenticator -> fallback sang Authenticator
            elif recovery_email and not recovery_visible and two_fa_key and auth_visible:
                print(f"[{email}] Không thấy option recovery, fallback sang Authenticator...")
                await auth_locator.first.click()
                verification_done = True
            
            # 4. Không có gì phù hợp
            else:
                if recovery_email and not recovery_visible:
                    print(f"[{email}] Có recovery email nhưng không thấy option recovery trên màn hình.")
                if two_fa_key and not auth_visible:
                    print(f"[{email}] Có 2FA key nhưng không thấy option Authenticator trên màn hình.")
                if not recovery_email and not two_fa_key:
                    print(f"[{email}] Không có recovery email và 2FA key trong txt.")
                    
        except Exception as e:
            print(f"[{email}] Bỏ qua bước chọn phương thức xác minh: {e}")


        # 4. Xử lý 2FA (TOTP) - CHỈ khi có two_fa_key trong txt
        await random_sleep(3, 5)
        otp_selector = 'input#totpPin, input[type="tel"], input[name="totpPin"]'
        if await page.locator(otp_selector).is_visible():
            if two_fa_key:
                print(f"[{email}] Phát hiện ô nhập 2FA và có key. Đang lấy mã...")
                otp_code = await get_2fa_code(two_fa_key)
                if otp_code:
                    print(f"[{email}] Nhập mã 2FA: {otp_code}")
                    await page.fill(otp_selector, otp_code)
                    await page.keyboard.press('Enter')
                else:
                    print(f"[{email}] Không lấy được mã 2FA từ API.")
                    log_login_fail(email, "Không lấy được mã 2FA")
            else:
                print(f"[{email}] Yêu cầu 2FA nhưng KHÔNG có key trong txt. Bỏ qua.")
                log_login_fail(email, "Yêu cầu 2FA nhưng không có key")

        # 4.5. Xử lý màn hình "Simplify your sign-in" (Passkeys)
        await random_sleep(2, 4)
        try:
            not_now_selector = 'button:has-text("Not now"), [role="button"]:has-text("Not now")'
            if await page.locator(not_now_selector).is_visible(timeout=5000):
                print(f"[{email}] Phát hiện màn hình Passkey. Click 'Not now'...")
                await page.click(not_now_selector)
        except Exception:
            pass

        # 4.6. Xử lý màn hình "Đảm bảo bạn có thể đăng nhập" (Recovery phone/email update)
        await random_sleep(2, 4)
        try:
            # Sử dụng jsname="ZUkOIc" từ HTML bạn cung cấp để xác định chính xác nút Huỷ
            cancel_selector = 'button[jsname="ZUkOIc"], button:has-text("Huỷ"), button:has-text("Not now"), [role="button"]:has-text("Huỷ")'
            cancel_btn = page.locator(cancel_selector)
            if await cancel_btn.is_visible(timeout=5000):
                print(f"[{email}] Phát hiện màn hình cập nhật thông tin khôi phục. Click 'Huỷ'...")
                await cancel_btn.click()
        except Exception:
            pass

        # 4.7. Xử lý màn hình "Đặt địa chỉ nhà riêng" (Home address)
        await random_sleep(2, 4)
        try:
            # Sử dụng jsname="ZUkOIc" và text "Bỏ qua" cho màn hình địa chỉ
            skip_address_selector = 'button[jsname="ZUkOIc"]:has-text("Bỏ qua"), button:has-text("Bỏ qua"), button:has-text("Skip")'
            skip_btn = page.locator(skip_address_selector)
            if await skip_btn.is_visible(timeout=5000):
                print(f"[{email}] Phát hiện màn hình đặt địa chỉ nhà. Click 'Bỏ qua'...")
                await skip_btn.click()
        except Exception:
            pass

        # 5. Xử lý các Challenge - FALLBACK (nếu bước 3.5 chưa xử lý xong)
        # Kiểm tra nếu vẫn còn ở màn hình challenge
        await random_sleep(2, 5)
        
        if "challenge" in page.url or await page.locator('div[role="link"][data-challengetype]').count() > 0:
            print(f"[{email}] (Fallback) Vẫn ở màn hình xác minh. Đang xử lý...")
            
            recovery_selector = 'div[role="link"][data-challengetype="12"]'
            # Authenticator: type 6 VÀ data-accountrecovery="false"
            auth_selector = 'div[role="link"][data-challengetype="6"][data-accountrecovery="false"]'
            
            recovery_loc = page.locator(recovery_selector)
            auth_loc = page.locator(auth_selector)
            
            try:
                # Dùng count() để tránh strict mode error
                recovery_visible = await recovery_loc.count() > 0
                auth_visible = await auth_loc.count() > 0
                
                # Ưu tiên recovery nếu có
                if recovery_email and recovery_visible:
                    print(f"[{email}] (Fallback) Chọn recovery email...")
                    await recovery_loc.click()
                    await asyncio.sleep(2)
                    
                    input_selector = 'input[name="knowledgePreregisteredEmailResponse"], input[type="email"], input[aria-label*="recovery"]'
                    await page.wait_for_selector(input_selector, state='visible', timeout=10000)
                    await page.locator(input_selector).first.fill(recovery_email)
                    await page.keyboard.press('Enter')
                    print(f"[{email}] (Fallback) Đã submit recovery email.")
                    
                # Fallback sang Authenticator nếu có 2FA key
                elif two_fa_key and auth_visible:
                    print(f"[{email}] (Fallback) Chọn Authenticator...")
                    await auth_loc.first.click()
                    # OTP sẽ được xử lý ở bước 4 đã chạy hoặc chạy lại
                    
                else:
                    # In debug
                    options = await page.locator('div[role="link"][data-challengetype]').all()
                    if options:
                        print(f"[{email}] (Fallback) Các option có sẵn:")
                        for opt in options:
                            c_type = await opt.get_attribute("data-challengetype")
                            text = await opt.inner_text()
                            print(f"   - Type: {c_type} | Text: {text.strip().replace(chr(10), ' ')}")
                    log_login_fail(email, "Không có phương thức xác minh phù hợp")
                    
            except Exception as e:
                print(f"[{email}] (Fallback) Lỗi: {e}")
                log_login_fail(email, f"Fallback error: {e}")
        
        # 5.5. Nhập OTP nếu đang ở màn hình nhập 2FA (sau Fallback hoặc trực tiếp)
        await random_sleep(2, 3)
        otp_selector = 'input#totpPin, input[type="tel"], input[name="totpPin"]'
        if await page.locator(otp_selector).is_visible(timeout=3000):
            if two_fa_key:
                print(f"[{email}] (Post-Fallback) Phát hiện ô nhập 2FA. Đang lấy mã...")
                otp_code = await get_2fa_code(two_fa_key)
                if otp_code:
                    print(f"[{email}] (Post-Fallback) Nhập mã 2FA: {otp_code}")
                    await page.fill(otp_selector, otp_code)
                    await page.keyboard.press('Enter')
                    await random_sleep(2, 3)
                else:
                    print(f"[{email}] (Post-Fallback) Không lấy được mã 2FA từ API.")
                    log_login_fail(email, "Không lấy được mã 2FA (post-fallback)")
            else:
                print(f"[{email}] (Post-Fallback) Yêu cầu 2FA nhưng KHÔNG có key.")
                log_login_fail(email, "Yêu cầu 2FA nhưng không có key (post-fallback)")

        # 6. [MỚI] Xử lý màn hình "Welcome" (Speedbump) của Google Workspace
        # Dấu hiệu: Có form id="tos_form" và nút id="confirm" (Tôi hiểu / I understand)
        print(f"[{email}] Đang kiểm tra màn hình Welcome/Speedbump...")
        
        try:
            # Selector nhắm vào nút "Tôi hiểu"
            # Dùng ID #confirm là chuẩn nhất theo HTML bạn gửi
            welcome_btn_selector = '#confirm, input[name="confirm"]'
            
            # Chờ ngắn 5s xem có hiện không
            if await page.locator(welcome_btn_selector).is_visible(timeout=5000):
                print(f"[{email}] Phát hiện màn hình 'Chào mừng/Tôi hiểu'. Đang click...")
                
                # Click nút
                await page.click(welcome_btn_selector, force=True)
                
                # Vì đây là submit form, cần đợi nó load trang tiếp theo
                await page.wait_for_load_state('networkidle')
                print(f"[{email}] Đã xác nhận Welcome.")
            else:
                print(f"[{email}] Không thấy màn hình Welcome, đi tiếp.")
                
        except Exception as e:
            # Không phải lỗi nghiêm trọng, có thể do không hiện
            print(f"[{email}] Bỏ qua bước Welcome (ko thấy hoặc lỗi): {e}")

        # 6.5. Xử lý màn hình "Add your birthday" (cho tài khoản Google cổ chưa đặt ngày sinh)
        # Dùng selector không phụ thuộc ngôn ngữ (attribute-based, không dùng text)
        await random_sleep(2, 4)
        try:
            # Phát hiện trang birthday bằng attribute data-year-required (có trong div chứa form nhập ngày sinh)
            birthday_form_selector = '[data-year-required="true"]'
            if await page.locator(birthday_form_selector).is_visible(timeout=5000):
                print(f"[{email}] Phát hiện màn hình nhập ngày sinh. Đang nhập...")

                # Chọn tháng từ dropdown (Material Design combobox)
                # Ưu tiên dùng jsname="cyoKE" (container tháng) rồi tìm combobox bên trong
                month_dropdown = page.locator('[jsname="cyoKE"] [role="combobox"]').first
                if await month_dropdown.count() == 0:
                    # Fallback: tìm combobox đầu tiên trong form birthday
                    month_dropdown = page.locator('.MW5eyd [role="combobox"]').first

                await month_dropdown.click()
                await random_sleep(0.5, 1)

                # Chọn tháng ngẫu nhiên (1-12) bằng data-value (không phụ thuộc ngôn ngữ)
                random_month = random.randint(1, 12)
                month_option = page.locator(f'li[role="option"][data-value="{random_month}"]')
                await month_option.click()
                print(f"[{email}] Đã chọn tháng: {random_month}")
                await random_sleep(0.5, 1)

                # Nhập ngày (Day) - dùng placeholder "DD" hoặc id "i7" (không phụ thuộc ngôn ngữ)
                day_input = page.locator('input[placeholder="DD"], input#i7').first
                await day_input.fill("15")
                print(f"[{email}] Đã nhập ngày: 15")
                await random_sleep(0.3, 0.8)

                # Nhập năm (Year) - dùng placeholder "YYYY" hoặc id "i8" (năm 2000 = 26 tuổi)
                year_input = page.locator('input[placeholder="YYYY"], input#i8').first
                birth_year = "2000"
                await year_input.fill(birth_year)
                print(f"[{email}] Đã nhập năm sinh: {birth_year}")
                await random_sleep(1, 2)

                # Click nút Save - dùng jsname="x8hlje" (không phụ thuộc ngôn ngữ)
                save_btn = page.locator('button[jsname="x8hlje"]').first
                await save_btn.click()
                print(f"[{email}] Đã click nút Save/Lưu.")
                await random_sleep(2, 4)

                # Xử lý modal xác nhận birthday
                try:
                    confirm_modal_selector = '[role="dialog"]'
                    if await page.locator(confirm_modal_selector).is_visible(timeout=5000):
                        # Dùng data-mdc-dialog-action="ok" để tìm nút xác nhận (không phụ thuộc ngôn ngữ)
                        confirm_btn = page.locator('[role="dialog"] button[data-mdc-dialog-action="ok"]').first
                        if await confirm_btn.is_visible(timeout=3000):
                            await confirm_btn.click()
                            print(f"[{email}] Đã xác nhận ngày sinh (Confirm).")
                            await random_sleep(2, 4)
                        else:
                            print(f"[{email}] Không thấy nút xác nhận trong modal.")
                    else:
                        print(f"[{email}] Không thấy modal xác nhận ngày sinh.")
                except Exception as e:
                    print(f"[{email}] Lỗi khi xử lý modal xác nhận ngày sinh: {e}")

                # Xử lý trang "Thank you" sau khi xác nhận birthday - bấm nút Done
                try:
                    # Dùng jsname="AHldd" cho nút Done (không phụ thuộc ngôn ngữ)
                    done_btn = page.locator('button[jsname="AHldd"]').first
                    if await done_btn.is_visible(timeout=5000):
                        await done_btn.click()
                        print(f"[{email}] Đã click Done trên trang Thank you.")
                        await random_sleep(2, 4)
                    else:
                        print(f"[{email}] Không thấy nút Done trên trang Thank you.")
                except Exception as e:
                    print(f"[{email}] Bỏ qua trang Thank you: {e}")

            else:
                print(f"[{email}] Không thấy màn hình nhập ngày sinh, đi tiếp.")
        except Exception as e:
            print(f"[{email}] Bỏ qua bước nhập ngày sinh (lỗi hoặc ko thấy): {e}")


        # 7. Đợi chuyển hướng về AI Studio
        print(f"[{email}] Đang đợi chuyển hướng về AI Studio...")
        try:
            # Sửa đổi: Chỉ cần chứa domain aistudio.google.com là được, không bắt buộc /prompts/
            await page.wait_for_url(re.compile(r"https://aistudio\.google\.com/.*"), timeout=30000)
            await page.wait_for_load_state("domcontentloaded")
        except PlaywrightTimeoutError:
            print(f"[{email}] LỖI: Không thấy chuyển hướng về AI Studio. Có thể bị kẹt ở màn hình xác minh khác.")
            log_login_fail(email, "Không chuyển hướng về AI Studio")
            await context.close()
            return

        # 8. Xử lý Terms of Service (ToS)
        await random_sleep(3, 5) # Chờ một chút để ToS load hẳn
        try:
            tos_selector = 'ms-tos-dialog'
            chat_selector = 'textarea, [contenteditable="true"]'
            
            # Kiểm tra xem có bảng ToS không
            if await page.locator(tos_selector).is_visible(timeout=10000):
                print(f"[{email}] Đang chấp nhận ToS...")
                
                # Click checkbox - Thử nhiều selector cho chắc chắn
                checkbox = page.locator('mat-checkbox, .mat-checkbox-inner-container, input[type="checkbox"]')
                if await checkbox.count() > 0:
                    await checkbox.first.click()
                    await random_sleep(1, 2)
                
                # Click nút Continue/Accept
                continue_btn = page.locator('button:has-text("Continue"), button:has-text("Accept"), button:has-text("Agree")')
                if await continue_btn.is_visible():
                    await continue_btn.click()
                    print(f"[{email}] Đã click nút Continue.")
                
                # Đợi bảng ToS biến mất
                await asyncio.sleep(3)
        except Exception as e:
            print(f"[{email}] Bỏ qua xử lý ToS (có thể đã chấp nhận trước đó): {e}")

        # 9. Lưu State - Kiểm tra xem đã vào được vùng chat chưa
        try:
            await page.wait_for_selector('textarea, [contenteditable="true"]', timeout=20000)
            print(f"[{email}] Login thành công! Đang lưu state...")
            
            if not os.path.exists(config.StatesDir):
                os.makedirs(config.StatesDir)
                
            await context.storage_state(path=state_path)
            login_success = True  # Đánh dấu thành công
            
            if args.remote:
                await upload_state(state_path, email)
        except PlaywrightTimeoutError:
            print(f"[{email}] LỖI: Đã vào AI Studio nhưng không thấy ô nhập liệu (Chat box).")
            log_login_fail(email, "Không thấy ô chat")
        except Exception as e:
            print(f"[{email}] LỖI lưu state: {e}")
            log_login_fail(email, f"Lỗi lưu state: {e}")
        
        # 10. Upload Remote (nếu có)
        if args.remote and login_success:
            await upload_state(state_path, email)

    except Exception as e:
        print(f"[{email}] LỖI KHÔNG XÁC ĐỊNH: {e}")
        log_login_fail(email, f"Lỗi: {e}")
    finally:
        await context.close()

async def upload_state(path, email):
    try:
        async with aiohttp.ClientSession() as session:
            url = f'{args.remote}/admin/upload_state'
            with open(path, 'rb') as f:
                data = aiohttp.FormData()
                data.add_field('state', f, filename=f'{email}.json', content_type='application/octet-stream')
                async with session.post(url, params={'key': config.AuthKey}, data=data) as resp:
                    if resp.status == 200:
                        print(f"[{email}] Upload state thành công.")
                    else:
                        print(f"[{email}] Upload thất bại: {resp.status}")
    except Exception as e:
        print(f"[{email}] Lỗi upload: {e}")

async def main():
    # Đọc file accounts
    if not os.path.exists(args.file):
        print(f"Không tìm thấy file: {args.file}")
        return

    accounts = []
    with open(args.file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line: continue
            parts = line.split('|')
            if len(parts) >= 2:
                email = parts[0].strip()
                password = parts[1].strip()
                recovery = parts[2].strip() if len(parts) > 2 else ""
                two_fa = parts[3].strip() if len(parts) > 3 else None
                if two_fa:
                    accounts.append((email, password, recovery, two_fa))
                else:
                    accounts.append((email, password, recovery))

    print(f"Đã tìm thấy {len(accounts)} tài khoản.")

    proxy = None
    if config.Proxy:
        proxy = {
            "server": config.Proxy.server,
            "username": config.Proxy.username,
            "password": config.Proxy.password,
        }

    # Khởi chạy Browser
    async with AsyncCamoufox(
        main_world_eval=True,
        headless=config.Headless, # Để False để bạn còn quan sát xem nó làm gì
        proxy=proxy,
        geoip=True if proxy else False,
    ) as browser:
        
        for acc in accounts:
            if len(acc) == 4:
                email, password, recovery, two_fa = acc
            else:
                email, password, recovery = acc
                two_fa = None
                
            await process_account(browser, email, password, recovery, two_fa)
            print("-" * 50)
            await asyncio.sleep(2) # Nghỉ giữa các acc

if __name__ == "__main__":
    asyncio.run(main())