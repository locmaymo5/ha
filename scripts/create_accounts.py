import pickle
import os.path
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import time

# --- CẤU HÌNH ---
SERVICE_ACCOUNT_FILE = 'service_account.json' # File key tải từ Google Cloud
ADMIN_EMAIL = 'locmaymo2@phamloc.me' # Email của user admin cao nhất (Super Admin)
SCOPES = ['https://www.googleapis.com/auth/admin.directory.user']

# Thông tin user muốn tạo
DOMAIN = 'phamloc.me'
DEFAULT_PASS = 'locloc11' # Mật khẩu (8-100 ký tự ASCII theo tài liệu)
RECOVERY_MAIL = 'locmaymo2@phamloc.me' # Email khôi phục để né verify phone

def create_users_bulk():
    # 1. Xác thực
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )
    # Service Account cần "đóng giả" admin để có quyền tạo user
    delegated_creds = creds.with_subject(ADMIN_EMAIL)

    service = build('admin', 'directory_v1', credentials=delegated_creds)

    # 2. Vòng lặp tạo từ locmaymo48 đến locmaymo52
    for i in range(1,2):
        username = f"locmaymo{i}"
        user_email = f"{username}@{DOMAIN}"
        
        print(f"Dang tao user: {user_email} ...")

        # Cấu trúc JSON body theo tài liệu bạn cung cấp
        body = {
            "primaryEmail": user_email,
            "name": {
                "givenName": username,
                "familyName": "User"
            },
            "password": DEFAULT_PASS, # Password dạng clear text
            "changePasswordAtNextLogin": False, # Không bắt đổi pass
            
            # QUAN TRỌNG: Nạp sẵn email khôi phục để tăng độ Trust
            "recoveryEmail": RECOVERY_MAIL, 
            
            # Có thể thêm tổ chức nếu muốn
            "organizations": [
                {
                    "name": "Team MMO",
                    "title": "Staff",
                    "primary": True,
                    "type": "work"
                }
            ]
        }

        try:
            # Gọi API users.insert()
            service.users().insert(body=body).execute()
            print(f"✅ Tạo thành công: {user_email} | Pass: {DEFAULT_PASS}")
            
        except HttpError as err:
            # Xử lý lỗi (ví dụ trùng user)
            if err.resp.status == 409:
                print(f"⚠️ User {user_email} đã tồn tại!")
            elif err.resp.status == 503:
                # Tài liệu khuyên dùng exponential back-off nếu dính lỗi 503 (quá tải)
                print("⏳ Quota exceeded, nghỉ 5s...")
                time.sleep(5)
            else:
                print(f"❌ Lỗi: {err}")

if __name__ == '__main__':
    create_users_bulk()