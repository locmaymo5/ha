import sys
import os
import asyncio
import re
import json
import logging
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from camoufox.async_api import AsyncCamoufox

sys.path.insert(0, '.')
from config import config
from scripts.auto_login_create_state import process_account

# --- CẤU HÌNH ADMIN SDK ---
SERVICE_ACCOUNT_FILE = 'service_account.json'
ADMIN_EMAIL = 'locmaymo2@phamloc.me' # Email Super Admin
SCOPES = ['https://www.googleapis.com/auth/admin.directory.user']

# --- CẤU HÌNH TẠO USER ---
DOMAIN = 'phamloc.me'
DEFAULT_PASS = 'locloc11'
RECOVERY_MAIL = 'locmaymo2@phamloc.me'
START_INDEX = 27 # thêm 36 tài khoản từ index 16
END_INDEX = 52  # Ví dụ tạo từ locmaymo16 đến locmaymo52

async def create_google_user(service, username):
    user_email = f"{username}@{DOMAIN}"
    body = {
        "primaryEmail": user_email,
        "name": {"givenName": username, "familyName": "User"},
        "password": DEFAULT_PASS,
        "changePasswordAtNextLogin": False,
        "recoveryEmail": RECOVERY_MAIL
    }
    try:
        service.users().insert(body=body).execute()
        print(f"✅ Đã tạo user: {user_email}")
        return True
    except HttpError as err:
        if err.resp.status == 409:
            print(f"⚠️ User {user_email} đã tồn tại, tiến hành đăng nhập.")
            return True
        print(f"❌ Lỗi tạo user {user_email}: {err}")
        return False

async def main():
    # 1. Khởi tạo Admin SDK
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    ).with_subject(ADMIN_EMAIL)
    admin_service = build('admin', 'directory_v1', credentials=creds)

    # 2. Cấu hình Browser
    proxy = None
    if config.Proxy:
        proxy = {
            "server": config.Proxy.server,
            "username": config.Proxy.username,
            "password": config.Proxy.password,
        }

    async with AsyncCamoufox(
        main_world_eval=True,
        headless=False,
        proxy=proxy,
        geoip=True if proxy else False,
    ) as browser:
        
        for i in range(START_INDEX, END_INDEX + 1):
            username = f"loc{i}"
            email = f"{username}@{DOMAIN}"
            
            # Bước 1: Tạo tài khoản
            success = await create_google_user(admin_service, username)
            
            if success:
                # Bước 2: Đăng nhập và lưu state (Sử dụng hàm từ auto_login_create_state)
                print(f"--- Đang xử lý Login cho {email} ---")
                await process_account(browser, email, DEFAULT_PASS, RECOVERY_MAIL)
                await asyncio.sleep(3) # Nghỉ giữa các luồng

if __name__ == '__main__':
    asyncio.run(main())