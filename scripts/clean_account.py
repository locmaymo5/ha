import sys
import logging
sys.path.insert(0, '.')
import os
import re
import json
import asyncio
import argparse
import random
import glob

import aiohttp
from camoufox.async_api import AsyncCamoufox
from playwright.async_api import expect, TimeoutError as PlaywrightTimeoutError

from config import config

# --- Cấu hình Argument ---
parser = argparse.ArgumentParser(
    description="Tool làm sạch tài khoản Google (đổi pass, gỡ recovery, bật 2FA) - Dùng state đã login",
    formatter_class=argparse.ArgumentDefaultsHelpFormatter
)

parser.add_argument('--accounts', type=str, help='File txt chứa thông tin acc (mail|pass|recovery|2fa)', default='accounts.txt')
parser.add_argument('--new-password', type=str, help='Mật khẩu mới cho tất cả tài khoản', default='locloc11')
parser.add_argument('--states-dir', type=str, help='Thư mục chứa các file state', default=None)

args = parser.parse_args()

# --- File lưu trạng thái ---
STATUS_FILE = 'clean_status.txt'
CLEANED_FILE = 'cleaned_accounts.txt'

def log_status(email, status, details=""):
    """Ghi trạng thái xử lý vào file"""
    with open(STATUS_FILE, 'a', encoding='utf-8') as f:
        if details:
            f.write(f"{email}|{status}|{details}\n")
        else:
            f.write(f"{email}|{status}\n")
    print(f"[{email}] Status: {status}")

def save_cleaned_account(email, new_password, two_fa_key=""):
    """Lưu account đã clean xong với thông tin mới"""
    with open(CLEANED_FILE, 'a', encoding='utf-8') as f:
        if two_fa_key:
            f.write(f"{email}|{new_password}||{two_fa_key}\n")
        else:
            f.write(f"{email}|{new_password}\n")

async def random_sleep(min_s=1, max_s=3):
    await asyncio.sleep(random.uniform(min_s, max_s))

async def get_2fa_code(secret):
    """Lấy mã 2FA từ API"""
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://2fa.live/tok/{secret.replace(' ', '')}"
            async with session.get(url) as resp:
                data = await resp.json()
                return data.get("token")
    except Exception as e:
        print(f"Lỗi lấy mã 2FA: {e}")
    return None

def load_account_info(accounts_file):
    """Đọc file accounts để lấy thông tin password và 2FA key"""
    account_map = {}
    if os.path.exists(accounts_file):
        with open(accounts_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line: continue
                parts = line.split('|')
                if len(parts) >= 2:
                    email = parts[0].strip()
                    password = parts[1].strip()
                    recovery = parts[2].strip() if len(parts) > 2 else ""
                    two_fa = parts[3].strip() if len(parts) > 3 else None
                    account_map[email] = {
                        'password': password,
                        'recovery': recovery,
                        'two_fa': two_fa
                    }
    return account_map

async def check_and_fill_password(page, password, email):
    """Kiểm tra và điền password nếu có yêu cầu"""
    try:
        password_input = page.locator('input[type="password"]')
        if await password_input.count() > 0 and await password_input.first.is_visible(timeout=2000):
            print(f"[{email}] Đang nhập mật khẩu...")
            await password_input.first.fill(password)
            await page.keyboard.press('Enter')
            await random_sleep(2, 3)
            return True
    except:
        pass
    return False

async def check_and_fill_2fa(page, two_fa_key, email):
    """
    Kiểm tra và điền 2FA nếu có yêu cầu.
    Thử 3 lần:
      - Lần 1 sai: đợi 3-4s rồi thử lại
      - Lần 2 sai: đợi 60s cho mã cũ hết hạn hoàn toàn khỏi hệ thống rồi thử lại
      - Lần 3 sai: bỏ cuộc
    """
    otp_selector = 'input#totpPin, input[type="tel"], input[name="totpPin"]'
    
    # Các selector lỗi 2FA
    error_selectors = [
        'div:has-text("Wrong code")',
        'div:has-text("Sai mã")',
        'div:has-text("Incorrect code")',
        'span:has-text("Wrong code")',
        'span:has-text("Sai mã")',
        'div:has-text("mã không chính xác")',
    ]
    
    try:
        otp_input = page.locator(otp_selector)
        if await otp_input.count() > 0 and await otp_input.first.is_visible(timeout=2000):
            if not two_fa_key:
                print(f"[{email}] Yêu cầu 2FA nhưng không có key!")
                return False
            
            # Thử tối đa 3 lần
            for attempt in range(3):
                print(f"[{email}] Đang lấy mã 2FA mới... (lần {attempt + 1}/3)")
                otp_code = await get_2fa_code(two_fa_key)
                
                if not otp_code:
                    print(f"[{email}] Không lấy được mã 2FA!")
                    return False
                
                print(f"[{email}] Nhập mã 2FA: {otp_code}")
                
                # Clear và nhập mã mới
                otp_input = page.locator(otp_selector)
                if await otp_input.count() > 0:
                    await otp_input.first.clear()
                    await otp_input.first.fill(otp_code)
                    await page.keyboard.press('Enter')
                    await random_sleep(2, 3)
                    
                    # Kiểm tra xem có lỗi không
                    has_error = False
                    for selector in error_selectors:
                        try:
                            error_el = page.locator(selector)
                            if await error_el.count() > 0 and await error_el.first.is_visible(timeout=1500):
                                has_error = True
                                break
                        except:
                            pass
                    
                    if has_error:
                        if attempt == 0:
                            print(f"[{email}] Mã 2FA sai/hết hạn! Đợi 3s và thử lại...")
                            await random_sleep(3, 4)
                            continue
                        elif attempt == 1:
                            print(f"[{email}] Mã 2FA vẫn sai sau 2 lần! Đợi 60s cho mã cũ hết hạn hoàn toàn...")
                            await asyncio.sleep(60)
                            continue
                        else:
                            print(f"[{email}] Mã 2FA vẫn sai sau 3 lần thử!")
                            return False
                    else:
                        # Thành công
                        if attempt > 0:
                            print(f"[{email}] ✓ Xác minh 2FA thành công (lần {attempt + 1})")
                        return True
                else:
                    print(f"[{email}] Không tìm thấy input 2FA!")
                    return False
            
            return False
    except Exception as e:
        print(f"[{email}] Lỗi 2FA: {e}")
    return True  # Không cần 2FA

async def handle_verification(page, password, two_fa_key, email):
    """Xử lý cả password và 2FA - gọi ở đầu mỗi bước"""
    # Kiểm tra password trước
    await check_and_fill_password(page, password, email)
    await random_sleep(1, 2)
    
    # Sau đó kiểm tra 2FA
    await check_and_fill_2fa(page, two_fa_key, email)
    await random_sleep(1, 2)

def update_account_2fa_secret(accounts_file, email, two_fa_secret):
    """Cập nhật 2FA secret vào file accounts.txt cho email tương ứng"""
    if not os.path.exists(accounts_file):
        print(f"File {accounts_file} không tồn tại!")
        return False
    
    # Đọc tất cả các dòng
    with open(accounts_file, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    updated = False
    new_lines = []
    
    for line in lines:
        stripped = line.strip()
        if not stripped:
            new_lines.append(line)
            continue
        
        parts = stripped.split('|')
        if len(parts) >= 1 and parts[0].strip() == email:
            # Tìm thấy email, cập nhật 2FA secret
            if len(parts) == 2:
                # email|pass -> email|pass||2fa
                new_line = f"{parts[0]}|{parts[1]}||{two_fa_secret}\n"
            elif len(parts) == 3:
                # email|pass|recovery -> email|pass|recovery|2fa
                new_line = f"{parts[0]}|{parts[1]}|{parts[2]}|{two_fa_secret}\n"
            elif len(parts) >= 4:
                # email|pass|recovery|old_2fa -> email|pass|recovery|new_2fa
                new_line = f"{parts[0]}|{parts[1]}|{parts[2]}|{two_fa_secret}\n"
            else:
                new_line = line
            new_lines.append(new_line)
            updated = True
            print(f"[{email}] Đã cập nhật 2FA secret vào file: {two_fa_secret}")
        else:
            new_lines.append(line)
    
    # Ghi lại file
    if updated:
        with open(accounts_file, 'w', encoding='utf-8') as f:
            f.writelines(new_lines)
    
    return updated

async def setup_authenticator(page, email, password, accounts_file):
    """
    Thiết lập ứng dụng xác thực (Authenticator) cho tài khoản.
    Trả về 2FA secret nếu thiết lập thành công, None nếu đã có hoặc thất bại.
    """
    print(f"[{email}] Kiểm tra trạng thái Authenticator...")
    
    try:
        await page.goto('https://myaccount.google.com/two-step-verification/authenticator')
        await random_sleep(2, 3)
        
        # Xử lý xác minh password nếu cần
        await check_and_fill_password(page, password, email)
        await random_sleep(2, 3)
        
        # Kiểm tra xem đã có authenticator chưa bằng cách tìm nút "Thiết lập ứng dụng xác thực"
        # Nếu có nút này thì CHƯA có authenticator
        setup_btn = page.locator('button:has-text("Thiết lập ứng dụng xác thực"), button:has-text("Set up authenticator")')
        
        if await setup_btn.count() == 0 or not await setup_btn.first.is_visible(timeout=5000):
            print(f"[{email}] Authenticator đã được thiết lập rồi. Bỏ qua.")
            return None
        
        print(f"[{email}] Chưa có Authenticator. Bắt đầu thiết lập...")
        
        # Click nút thiết lập
        await setup_btn.first.click()
        await random_sleep(2, 3)
        
        # Modal hiện ra với QR code - đợi nó load
        await random_sleep(1, 2)
        
        # Click vào link "Không thể quét mã?" hoặc "Can't scan it?" để hiển thị secret key dạng text
        # HTML thực tế: button trong div[jsname="Ptcard"] với class mUIrbf-LgbsSe
        cant_scan_selectors = [
            'div[jsname="Ptcard"] button',  # Selector chính xác nhất từ HTML
            'button.mUIrbf-LgbsSe',  # Class của nút "Can't scan it?"
            'button:has-text("Can\'t scan it")',
            'button:has-text("Can\'t scan")',
            'button:has-text("Không thể quét")',
            'button:has-text("không quét được")',
            'a:has-text("Can\'t scan")',
            'a:has-text("Không thể quét")',
        ]
        
        clicked_cant_scan = False
        for selector in cant_scan_selectors:
            try:
                link = page.locator(selector)
                if await link.count() > 0 and await link.first.is_visible(timeout=2000):
                    print(f"[{email}] Tìm thấy link (selector: {selector}). Click để lấy secret key...")
                    await link.first.click()
                    await random_sleep(3, 4)  # Đợi lâu hơn để modal chuyển sang màn manual key
                    clicked_cant_scan = True
                    break
            except Exception as e:
                continue
        
        if not clicked_cant_scan:
            print(f"[{email}] Không tìm thấy link 'Không thể quét mã'. Thử lấy secret key từ trang hiện tại...")
        
        # Đợi thêm để modal load xong
        await random_sleep(1, 2)
        
        # Bây giờ modal hiển thị màn hình với manual key
        # Tìm khóa trong thẻ <strong> trong wizard-step-uid="Security Center: StrongAuth: Authenticator:manualKey"
        secret_key = None
        
        # Tìm div chứa khóa - wizard step manualKey
        manual_key_step = page.locator('div[wizard-step-uid*="manualKey"]')
        if await manual_key_step.count() > 0:
            # Tìm thẻ strong chứa khóa (dạng "xxxx xxxx xxxx xxxx xxxx xxxx xxxx xxxx")
            strong_elements = manual_key_step.locator('strong')
            for i in range(await strong_elements.count()):
                text = await strong_elements.nth(i).inner_text()
                # Kiểm tra xem có phải là secret key không (chứa ký tự và dấu cách)
                if text and len(text) > 20 and ' ' in text:
                    secret_key = text.replace(' ', '').strip()
                    print(f"[{email}] Đã tìm thấy secret key: {text}")
                    break
        
        if not secret_key:
            # Thử tìm cách khác - tìm trong toàn bộ modal
            strong_elements = page.locator('div[role="dialog"] strong, div.qPtGzb strong')
            for i in range(await strong_elements.count()):
                text = await strong_elements.nth(i).inner_text()
                # Secret key thường có 32 ký tự (dạng base32) với dấu cách
                if text and len(text.replace(' ', '')) >= 16:
                    clean_text = text.replace(' ', '').strip().lower()
                    # Kiểm tra xem có phải base32 không (chỉ chứa a-z, 2-7)
                    if all(c in 'abcdefghijklmnopqrstuvwxyz234567' for c in clean_text):
                        secret_key = clean_text
                        print(f"[{email}] Đã tìm thấy secret key: {text}")
                        break
        
        if not secret_key:
            print(f"[{email}] Không tìm thấy secret key!")
            # Đóng modal
            cancel_btn = page.locator('button[data-id="gQ2Xie"]:has-text("Huỷ"), button:has-text("Cancel")')
            if await cancel_btn.count() > 0:
                await cancel_btn.first.click()
            return None
        
        # Lưu secret key vào file accounts.txt
        update_account_2fa_secret(accounts_file, email, secret_key)
        
        # Click "Tiếp theo" để đến bước nhập mã xác minh
        next_btn = page.locator('button[data-id="OCpkoe"]:has-text("Tiếp theo"), button:has-text("Next")')
        if await next_btn.count() > 0 and await next_btn.first.is_visible(timeout=5000):
            print(f"[{email}] Click Tiếp theo để xác minh mã...")
            await next_btn.first.click()
            await random_sleep(2, 3)
        
        # Lấy mã OTP từ 2fa.live
        otp_code = await get_2fa_code(secret_key)
        if not otp_code:
            print(f"[{email}] Không lấy được mã OTP!")
            cancel_btn = page.locator('button[data-id="gQ2Xie"]:has-text("Huỷ"), button:has-text("Cancel")')
            if await cancel_btn.count() > 0:
                await cancel_btn.first.click()
            return None
        
        print(f"[{email}] Nhập mã OTP: {otp_code}")
        
        # Nhập mã vào input - từ HTML: input#c1 với placeholder="Nhập mã"
        otp_input = page.locator('input#c1, input.qdOxv-fmcmS-wGMbrd, input[placeholder="Nhập mã"], input[placeholder="Enter code"]')
        if await otp_input.count() > 0:
            print(f"[{email}] Tìm thấy input OTP, đang nhập...")
            await otp_input.first.fill(otp_code)
            await random_sleep(1, 2)
        else:
            print(f"[{email}] Không tìm thấy input OTP!")
            return None
        
        # Click nút "Xác minh" / "Verify" - từ HTML: button[data-id="dtOep"]
        verify_btn = page.locator('button[data-id="dtOep"], button:has-text("Xác minh"), button:has-text("Verify")')
        if await verify_btn.count() > 0 and await verify_btn.first.is_visible(timeout=5000):
            print(f"[{email}] Click Xác minh...")
            await verify_btn.first.click()
            await random_sleep(3, 5)
        
        # Kiểm tra xem có lỗi không
        error_msg = page.locator('p[aria-hidden="false"]:has-text("sai"), p:has-text("incorrect"), p:has-text("wrong")')
        if await error_msg.count() > 0 and await error_msg.first.is_visible(timeout=2000):
            print(f"[{email}] Mã OTP sai! Thử lấy mã mới...")
            # Thử lại với mã mới
            await random_sleep(3, 4)
            otp_code = await get_2fa_code(secret_key)
            if otp_code:
                otp_input = page.locator('input#c1, input[placeholder*="Nhập mã"]')
                if await otp_input.count() > 0:
                    await otp_input.first.clear()
                    await otp_input.first.fill(otp_code)
                    await random_sleep(1, 2)
                    await verify_btn.first.click()
                    await random_sleep(3, 5)
        
        print(f"[{email}] ✓ Đã thiết lập Authenticator thành công!")
        return secret_key
        
    except Exception as e:
        print(f"[{email}] Lỗi thiết lập Authenticator: {e}")
        return None

# --- Các hàm xử lý chính ---

async def change_language_to_vietnamese(page, email, password, two_fa_key):
    """Đổi ngôn ngữ chính của tài khoản Google sang Tiếng Việt nếu chưa phải."""
    print(f"[{email}] Kiểm tra ngôn ngữ tài khoản...")
    
    try:
        await page.goto('https://myaccount.google.com/language')
        await random_sleep(2, 3)
        
        # Xử lý xác minh nếu cần
        await handle_verification(page, password, two_fa_key, email)
        await random_sleep(2, 3)
        
        # Kiểm tra ngôn ngữ chính hiện tại bằng data-id (không phụ thuộc ngôn ngữ hiển thị)
        # Ngôn ngữ chính là item đầu tiên trong phần "Ngôn ngữ bạn muốn dùng"
        first_lang_item = page.locator('ul.u7hyyf li[data-id]').first
        
        if await first_lang_item.count() > 0:
            current_lang_id = await first_lang_item.get_attribute('data-id')
            print(f"[{email}] Ngôn ngữ chính hiện tại: {current_lang_id}")
            
            if current_lang_id == 'vi':
                print(f"[{email}] Đã là Tiếng Việt. Bỏ qua.")
                return True
        else:
            print(f"[{email}] Không tìm thấy thông tin ngôn ngữ. Thử đổi sang Tiếng Việt...")
        
        # Ngôn ngữ chưa phải Tiếng Việt -> đổi
        print(f"[{email}] Đang đổi ngôn ngữ sang Tiếng Việt...")
        
        # Click nút chỉnh sửa (biểu tượng bút) của ngôn ngữ chính
        # Selector không phụ thuộc ngôn ngữ: nút đầu tiên có icon edit trong danh sách ngôn ngữ chính
        edit_btn = page.locator('ul.u7hyyf li[data-id] button[jsname="Pr7Yme"]').first
        
        if await edit_btn.count() == 0:
            # Fallback: tìm nút edit bất kỳ trong section ngôn ngữ chính
            edit_btn = page.locator('ul.u7hyyf li button').first
        
        if await edit_btn.count() == 0:
            print(f"[{email}] Không tìm thấy nút chỉnh sửa ngôn ngữ!")
            return False
        
        await edit_btn.click()
        await random_sleep(2, 3)
        
        # Modal hiện ra với input combobox để tìm ngôn ngữ
        # Tìm input combobox trong dialog (không phụ thuộc ngôn ngữ)
        lang_input = page.locator('div[role="dialog"] input[role="combobox"], input[data-axe="mdc-autocomplete"]')
        
        if await lang_input.count() == 0:
            print(f"[{email}] Không tìm thấy input tìm kiếm ngôn ngữ!")
            return False
        
        # Nhập "viet" để tìm Tiếng Việt (hoạt động với cả "Vietnamese" và "Tiếng Việt")
        await lang_input.first.fill('')
        await random_sleep(0.5, 1)
        await lang_input.first.type('viet', delay=100)
        await random_sleep(2, 3)
        
        # Chọn option Tiếng Việt từ dropdown bằng data-language-code="vi" (không phụ thuộc ngôn ngữ hiển thị)
        vi_option = page.locator('li[data-language-code="vi"], li[role="option"][data-language-code="vi"]')
        
        if await vi_option.count() > 0:
            await vi_option.first.click()
            await random_sleep(1, 2)
            print(f"[{email}] Đã chọn Tiếng Việt.")
        else:
            print(f"[{email}] Không tìm thấy option Tiếng Việt trong dropdown!")
            # Đóng modal
            cancel_btn = page.locator('button[data-mdc-dialog-action="gQ2Xie"]')
            if await cancel_btn.count() > 0:
                await cancel_btn.first.click()
            return False
        
        # Click nút Lưu/Save trong dialog
        # Dùng data-mdc-dialog-action="x8hlje" (không phụ thuộc ngôn ngữ)
        save_btn = page.locator('button[data-mdc-dialog-action="x8hlje"]')
        
        if await save_btn.count() > 0 and await save_btn.first.is_enabled(timeout=5000):
            await save_btn.first.click()
            await random_sleep(3, 5)
            print(f"[{email}] ✓ Đã đổi ngôn ngữ sang Tiếng Việt!")
            return True
        else:
            print(f"[{email}] Nút Lưu không khả dụng!")
            # Đóng modal
            cancel_btn = page.locator('button[data-mdc-dialog-action="gQ2Xie"]')
            if await cancel_btn.count() > 0:
                await cancel_btn.first.click()
            return False
        
    except Exception as e:
        print(f"[{email}] Lỗi đổi ngôn ngữ: {e}")
        return False

async def change_password(page, email, old_password, new_password, two_fa_key):
    """Đổi mật khẩu tài khoản"""
    print(f"[{email}] Bắt đầu đổi mật khẩu...")
    
    try:
        await page.goto('https://myaccount.google.com/signinoptions/password')
        await random_sleep(2, 3)
        
        # Xử lý xác minh (password + 2FA)
        await handle_verification(page, old_password, two_fa_key, email)
        
        # Đợi thêm cho trang load
        await random_sleep(2, 3)
        
        # Tìm form đổi mật khẩu mới (theo HTML: name="password" và name="confirmation_password")
        new_pass_input = page.locator('input[name="password"]')
        confirm_pass_input = page.locator('input[name="confirmation_password"]')
        
        if await new_pass_input.count() > 0 and await confirm_pass_input.count() > 0:
            print(f"[{email}] Nhập mật khẩu mới...")
            await new_pass_input.fill(new_password)
            await random_sleep(0.5, 1)
            await confirm_pass_input.fill(new_password)
            await random_sleep(0.5, 1)
            
            # Click nút đổi mật khẩu - thử nhiều selector
            # Đợi nút hiển thị
            await random_sleep(1, 2)
            
            # Selector: button type="submit" hoặc button có text "Đổi mật khẩu"/"Change password"
            change_btn = page.locator('button[type="submit"], button:has-text("Đổi mật khẩu"), button:has-text("Change password")')
            
            if await change_btn.count() > 0:
                print(f"[{email}] Click nút đổi mật khẩu...")
                await change_btn.first.click(force=True)
            else:
                print(f"[{email}] Không tìm thấy nút, thử Enter...")
                await page.keyboard.press('Enter')
            
            # Đợi trang xử lý - TĂNG THỜI GIAN ĐỢI
            await random_sleep(5, 8)
            
            # Kiểm tra xem có thông báo thành công không
            success_indicators = [
                'div:has-text("Mật khẩu đã được thay đổi")',
                'div:has-text("Mật khẩu của bạn đã được thay đổi")',
                'div:has-text("Password changed")',
                'div:has-text("Your password has been changed")',
            ]
            
            for indicator in success_indicators:
                try:
                    if await page.locator(indicator).count() > 0:
                        print(f"[{email}] Xác nhận: Đã đổi mật khẩu thành công!")
                        return True, new_password
                except:
                    pass
            
            print(f"[{email}] Đã gửi yêu cầu đổi mật khẩu.")
            return True, new_password
        else:
            print(f"[{email}] Không tìm thấy form đổi mật khẩu.")
            return False, old_password
            
    except Exception as e:
        print(f"[{email}] Lỗi đổi mật khẩu: {e}")
        return False, old_password

async def remove_recovery_info(page, email, password, two_fa_key):
    """Gỡ bỏ email và số điện thoại khôi phục"""
    print(f"[{email}] Bắt đầu gỡ thông tin khôi phục...")
    
    removed_items = []
    
    try:
        # === GỠ RECOVERY EMAIL ===
        print(f"[{email}] Đang xử lý recovery email...")
        await page.goto('https://myaccount.google.com/recovery/email')
        await random_sleep(2, 3)
        
        # Xử lý xác minh
        await handle_verification(page, password, two_fa_key, email)
        await random_sleep(2, 3)
        
        # Nút xóa email: jsname="nANYu" hoặc aria-label chứa "Xoá"/"Delete"
        delete_email_btn = page.locator('button[jsname="nANYu"], button[aria-label*="Xoá địa chỉ email"], button[aria-label*="Delete"]')
        
        if await delete_email_btn.count() > 0 and await delete_email_btn.first.is_visible(timeout=5000):
            print(f"[{email}] Tìm thấy recovery email. Đang xóa...")
            await delete_email_btn.first.click()
            await random_sleep(2, 3)
            
            # Click xác nhận trong modal
            # Dùng data-mdc-dialog-action="ok" (chính xác nhất, không phụ thuộc ngôn ngữ)
            confirm_btn = page.locator('button[data-mdc-dialog-action="ok"]')
            
            if await confirm_btn.count() > 0 and await confirm_btn.first.is_visible(timeout=5000):
                print(f"[{email}] Click xác nhận xóa trong modal...")
                await confirm_btn.first.click(force=True)
                await random_sleep(3, 5)
            else:
                # Fallback: tìm theo text
                fallback_btn = page.locator('button:has-text("Xóa"), button:has-text("Remove"), button:has-text("Delete")')
                if await fallback_btn.count() > 0 and await fallback_btn.first.is_visible(timeout=3000):
                    await fallback_btn.first.click(force=True)
                    await random_sleep(3, 5)
            
            removed_items.append("recovery_email")
            print(f"[{email}] Đã xóa recovery email.")
        else:
            print(f"[{email}] Không có recovery email hoặc không tìm thấy nút xóa.")
        
        # === GỠ RECOVERY PHONE ===
        print(f"[{email}] Đang xử lý recovery phone...")
        await page.goto('https://myaccount.google.com/signinoptions/rescuephone')
        await random_sleep(2, 3)
        
        # Xử lý xác minh
        await handle_verification(page, password, two_fa_key, email)
        await random_sleep(2, 3)
        
        # Nút xóa phone: jsname="uXqWSe" hoặc aria-label "Remove phone number"
        delete_phone_btn = page.locator('button[jsname="uXqWSe"], button[aria-label*="Remove phone"], button[aria-label*="Xóa số điện thoại"]')
        
        if await delete_phone_btn.count() > 0 and await delete_phone_btn.first.is_visible(timeout=5000):
            print(f"[{email}] Tìm thấy recovery phone. Đang xóa...")
            await delete_phone_btn.first.click()
            await random_sleep(2, 3)
            
            # Click xác nhận trong modal
            # Dùng data-mdc-dialog-action="ok" (chính xác nhất, không phụ thuộc ngôn ngữ)
            confirm_btn = page.locator('button[data-mdc-dialog-action="ok"]')
            
            if await confirm_btn.count() > 0 and await confirm_btn.first.is_visible(timeout=5000):
                print(f"[{email}] Click xác nhận xóa trong modal...")
                await confirm_btn.first.click(force=True)
                await random_sleep(3, 5)
            else:
                # Fallback: tìm theo text
                fallback_btn = page.locator('button:has-text("Xóa"), button:has-text("Remove"), button:has-text("Delete")')
                if await fallback_btn.count() > 0 and await fallback_btn.first.is_visible(timeout=3000):
                    await fallback_btn.first.click(force=True)
                    await random_sleep(3, 5)
            
            removed_items.append("recovery_phone")
            print(f"[{email}] Đã xóa recovery phone.")
        else:
            print(f"[{email}] Không có recovery phone hoặc không tìm thấy nút xóa.")
            
    except Exception as e:
        print(f"[{email}] Lỗi gỡ thông tin khôi phục: {e}")
    
    return removed_items

async def enable_2sv(page, email, password, two_fa_key):
    """Kiểm tra và bật xác minh 2 bước nếu chưa bật"""
    print(f"[{email}] Kiểm tra trạng thái xác minh 2 bước...")
    
    try:
        await page.goto('https://myaccount.google.com/signinoptions/twosv')
        await random_sleep(2, 3)
        
        # Xử lý xác minh
        await handle_verification(page, password, two_fa_key, email)
        await random_sleep(2, 3)
        
        # Kiểm tra xem 2FA đã bật chưa
        # Nếu đã bật thì không có nút "Bật tính năng Xác minh 2 bước"
        enable_2sv_btn = page.locator('button[aria-label="Bật tính năng Xác minh 2 bước"], button:has-text("Bật tính năng Xác minh 2 bước"), button:has-text("Turn on 2-Step Verification")')
        
        if await enable_2sv_btn.count() == 0:
            print(f"[{email}] Xác minh 2 bước đã được bật. Bỏ qua.")
            return True
        
        # Chưa bật 2FA - kiểm tra xem có Authenticator chưa
        # Nếu text "Thêm ứng dụng xác thực" có nghĩa là CHƯA có authenticator
        authenticator_item = page.locator('a[href="two-step-verification/authenticator"]')
        
        if await authenticator_item.count() > 0:
            item_text = await authenticator_item.inner_text()
            if "Thêm ứng dụng xác thực" in item_text or "Add authenticator" in item_text.lower():
                print(f"[{email}] Chưa có Authenticator. Không thể bật 2FA.")
                return False
        
        # Có Authenticator → bật 2FA
        print(f"[{email}] Đã có Authenticator. Đang bật xác minh 2 bước...")
        await enable_2sv_btn.first.click()
        await random_sleep(2, 3)
        
        # Sau khi click, có modal hỏi thêm số điện thoại → click "Bỏ qua"
        skip_phone_btn = page.locator('button[data-mdc-dialog-action="d7k1Xe"], button[aria-label="Bỏ qua"], button:has-text("Bỏ qua"), button:has-text("Skip")')
        
        if await skip_phone_btn.count() > 0 and await skip_phone_btn.first.is_visible(timeout=5000):
            print(f"[{email}] Bỏ qua thêm số điện thoại...")
            await skip_phone_btn.first.click()
            await random_sleep(3, 5)
        
        # Kiểm tra lại xem đã bật thành công chưa
        await page.goto('https://myaccount.google.com/signinoptions/twosv')
        await random_sleep(2, 3)
        
        enable_btn_check = page.locator('button[aria-label="Bật tính năng Xác minh 2 bước"], button:has-text("Bật tính năng Xác minh 2 bước")')
        if await enable_btn_check.count() == 0:
            print(f"[{email}] ✓ Đã bật xác minh 2 bước thành công!")
            return True
        else:
            print(f"[{email}] Không thể bật xác minh 2 bước.")
            return False
            
    except Exception as e:
        print(f"[{email}] Lỗi khi bật xác minh 2 bước: {e}")
        return False

# --- Hàm xử lý chính cho 1 tài khoản (dùng state đã login) ---
async def process_account(browser, state_path, account_info, new_password, accounts_file):
    email = os.path.basename(state_path).replace('.json', '')
    
    print(f"\n{'='*60}")
    print(f"[{email}] BẮT ĐẦU XỬ LÝ (từ state đã login)...")
    print(f"{'='*60}")
    
    # Lấy thông tin từ account_info (nếu có)
    password = account_info.get('password', '') if account_info else ''
    two_fa_key = account_info.get('two_fa', None) if account_info else None
    
    print(f"[{email}] Password: {'Có' if password else 'Không có trong file'}")
    print(f"[{email}] 2FA key: {'Có' if two_fa_key else 'Không'}")
    
    results = {
        'authenticator_setup': False,
        'password_changed': False,
        'recovery_removed': [],
        '2sv_enabled': False
    }
    
    current_password = password
    
    try:
        # Tạo context từ state đã lưu (ĐÃ ĐĂNG NHẬP SẴN)
        context = await browser.new_context(
            storage_state=state_path,
            locale='vi-VN',
            timezone_id='Asia/Ho_Chi_Minh'
        )
        
        page = await context.new_page()
        
        # Kiểm tra state còn hợp lệ không
        print(f"[{email}] Kiểm tra state...")
        await page.goto('https://myaccount.google.com/')
        await random_sleep(2, 3)
        
        current_url = page.url
        if "accounts.google.com" in current_url and "signin" in current_url:
            print(f"[{email}] State đã hết hạn! Bỏ qua...")
            log_status(email, "FAILED", "State expired")
            await context.close()
            return
        
        print(f"[{email}] State còn hợp lệ! Bắt đầu clean...")
        
        # 0. ĐỔI NGÔN NGỮ SANG TIẾNG VIỆT (nếu chưa phải)
        lang_changed = await change_language_to_vietnamese(page, email, password, two_fa_key)
        results['language_changed'] = lang_changed
        
        # 1. THIẾT LẬP AUTHENTICATOR (nếu chưa có)
        if not two_fa_key:
            print(f"[{email}] Chưa có 2FA key, kiểm tra và thiết lập Authenticator...")
            new_2fa_key = await setup_authenticator(page, email, password, accounts_file)
            if new_2fa_key:
                two_fa_key = new_2fa_key
                results['authenticator_setup'] = True
                print(f"[{email}] ✓ Đã thiết lập Authenticator với key: {two_fa_key}")
            else:
                print(f"[{email}] Authenticator đã có hoặc không thể thiết lập.")
        else:
            print(f"[{email}] Đã có 2FA key trong file, bỏ qua thiết lập Authenticator.")
        
        # 2. ĐỔI MẬT KHẨU (chỉ khi có password cũ và password cũ khác password mới)
        if password:
            if password == new_password:
                print(f"[{email}] ⚠ Mật khẩu đã là '{new_password}' rồi, bỏ qua đổi mật khẩu.")
                results['password_changed'] = False
                current_password = password
            else:
                success, current_password = await change_password(page, email, password, new_password, two_fa_key)
                results['password_changed'] = success
                if success:
                    print(f"[{email}] ✓ Đã đổi mật khẩu thành: {new_password}")
        else:
            print(f"[{email}] ⚠ Bỏ qua đổi mật khẩu (không có password cũ trong file)")
        
        # 3. GỠ THÔNG TIN KHÔI PHỤC
        removed = await remove_recovery_info(page, email, current_password, two_fa_key)
        results['recovery_removed'] = removed
        if removed:
            print(f"[{email}] ✓ Đã gỡ: {', '.join(removed)}")
        
        # 4. BẬT XÁC MINH 2 BƯỚC (nếu có 2FA key)
        if two_fa_key:
            success = await enable_2sv(page, email, current_password, two_fa_key)
            results['2sv_enabled'] = success
        else:
            print(f"[{email}] ⚠ Bỏ qua bật 2FA (không có 2FA key)")
            results['2sv_enabled'] = False
        
        # Tổng kết
        status_parts = []
        if results.get('language_changed'):
            status_parts.append("lang_vi")
        if results.get('authenticator_setup'):
            status_parts.append("auth_setup")
        if results['password_changed']:
            status_parts.append("pass_changed")
        if results['recovery_removed']:
            status_parts.append(f"removed:{','.join(results['recovery_removed'])}")
        if results.get('2sv_enabled'):
            status_parts.append("2sv_enabled")
        
        if status_parts:
            log_status(email, "SUCCESS", " | ".join(status_parts))
            save_cleaned_account(email, current_password if results['password_changed'] else password, two_fa_key or "")
        else:
            log_status(email, "PARTIAL", "No changes made")
        
        await context.close()
        
    except Exception as e:
        print(f"[{email}] LỖI KHÔNG XÁC ĐỊNH: {e}")
        log_status(email, "ERROR", str(e))

async def main():
    states_dir = args.states_dir or config.StatesDir
    
    # Đọc thông tin acc từ file (để lấy password và 2FA key)
    account_map = load_account_info(args.accounts)
    print(f"Đã load thông tin {len(account_map)} tài khoản từ {args.accounts}")
    
    # Tìm các file state
    state_files = glob.glob(os.path.join(states_dir, "*.json"))
    if not state_files:
        print(f"Không tìm thấy file state nào trong {states_dir}")
        return

    print(f"Tìm thấy {len(state_files)} states để xử lý.")
    print(f"Mật khẩu mới sẽ là: {args.new_password}")
    print(f"Kết quả sẽ lưu vào: {STATUS_FILE}")
    print(f"Account đã clean sẽ lưu vào: {CLEANED_FILE}")
    print("-" * 60)

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
        headless=config.Headless,
        proxy=proxy,
        geoip=True if proxy else False,
    ) as browser:
        
        for state_file in state_files:
            email = os.path.basename(state_file).replace('.json', '')
            account_info = account_map.get(email, None)
            
            await process_account(browser, state_file, account_info, args.new_password, args.accounts)
            print("-" * 60)
            await asyncio.sleep(3)

    print("\n" + "=" * 60)
    print("HOÀN TẤT!")
    print(f"Xem kết quả chi tiết tại: {STATUS_FILE}")
    print(f"Danh sách acc đã clean: {CLEANED_FILE}")
    print("=" * 60)

if __name__ == "__main__":
    asyncio.run(main())
