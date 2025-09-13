import os

class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "devkey")
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", "sqlite:///app.db")
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    TELEGRAM_BOT_TOKEN = os.getenv("6180505622:AAEW7vZO3IIXO91EPOaQFYt3gBO9GUcPoas", "")
    ADMIN_CHAT_ID = os.getenv("1241311689", "")
