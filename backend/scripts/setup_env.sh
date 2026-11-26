#!/usr/bin/env bash
# Create backend/.env for a server deployment, with fresh random secrets.
# Usage (from the backend/ folder):
#   bash scripts/setup_env.sh <domain> <contact-email> "<site name>"
# Refuses to overwrite an existing .env (that would lose the token encryption key).
set -euo pipefail
cd "$(dirname "$0")/.."

DOMAIN="${1:-}"; EMAIL="${2:-}"; NAME="${3:-Bản tin tổng hợp}"
if [ -z "$DOMAIN" ] || [ -z "$EMAIL" ]; then
  echo "Cách dùng: bash scripts/setup_env.sh <ten-mien> <email-lien-he> \"<ten trang>\"" >&2
  exit 1
fi
if [ -e .env ]; then
  echo ".env đã tồn tại; không ghi đè để tránh mất khóa mã hóa. Xóa thủ công nếu chắc chắn." >&2
  exit 1
fi

rand() { python3 -c "import secrets; print(secrets.token_urlsafe(40))"; }
fernet() { python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"; }

cp .env.example .env
chmod 600 .env
set_var() {
  local key="$1" value="$2"
  python3 - "$key" "$value" <<'PY'
import re, sys
key, value = sys.argv[1], sys.argv[2]
text = open(".env", encoding="utf-8").read()
line = f"{key}={value}"
if re.search(rf"^{re.escape(key)}=.*$", text, flags=re.M):
    text = re.sub(rf"^{re.escape(key)}=.*$", lambda m: line, text, count=1, flags=re.M)
else:
    text += f"\n{line}\n"
open(".env", "w", encoding="utf-8").write(text)
PY
}

set_var API_KEY "$(rand)"
set_var FACEBOOK_HUB_API_KEY ""
set_var ALLOWED_ORIGINS "https://$DOMAIN"
set_var SITE_DOMAIN "$DOMAIN"
set_var PUBLIC_SITE_NAME "$NAME"
set_var PUBLIC_CONTACT_EMAIL "$EMAIL"
set_var META_REDIRECT_URI "https://$DOMAIN/api/facebook-hub/meta/callback"
set_var META_TOKEN_ENCRYPTION_KEY "$(fernet)"
set_var META_PUBLISHING_ENABLED "false"

echo "Đã tạo .env cho https://$DOMAIN"
echo "API key (mật khẩu vào ứng dụng quản lý) — lưu vào trình quản lý mật khẩu, KHÔNG gửi cho ai:"
grep '^API_KEY=' .env | cut -d= -f2-
