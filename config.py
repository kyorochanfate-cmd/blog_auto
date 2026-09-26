import os
import secrets
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / '.env')

GEMINI_API_KEY = os.environ['GEMINI_API_KEY']
GEMINI_MODEL = os.environ.get('GEMINI_MODEL', 'gemini-3.5-flash-lite')

# はてな初期シード用 (任意)
HATENA_ID = os.environ.get('HATENA_ID', '')
HATENA_API_KEY = os.environ.get('HATENA_API_KEY', '')
HATENA_BLOG_DOMAIN = os.environ.get('HATENA_BLOG_DOMAIN', '')

GOOGLE_OAUTH_CLIENT_ID = os.environ['GOOGLE_OAUTH_CLIENT_ID']
ALLOWED_EMAILS = set(
    e.strip().lower()
    for e in os.environ.get('ALLOWED_EMAILS', '').split(',')
    if e.strip()
)

FLASK_SECRET_KEY = os.environ.get('FLASK_SECRET_KEY') or secrets.token_urlsafe(32)

# 自動運用 (Cloud Scheduler が /auto-run を叩く際の共有シークレット)
# 未設定だと自動運用エンドポイントは常に 401 を返す (= 安全側に倒す)
AUTO_RUN_TOKEN = os.environ.get('AUTO_RUN_TOKEN', '')
