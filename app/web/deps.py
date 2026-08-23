# app\web\deps.py

from fastapi import Cookie, HTTPException
from sqlalchemy.orm import Session

from app.core.security import decode_token
from app.core.config import settings
from app.db.session import SessionLocal
from app.models.user import User


def get_current_admin(admin_token: str | None = Cookie(default=None)):
    if not admin_token:
        raise HTTPException(status_code=401)

    try:
        payload = decode_token(admin_token)
    except Exception:
        raise HTTPException(status_code=401)

    db: Session = SessionLocal()
    try:
        user = db.query(User).filter(User.id == int(payload["sub"])).first()

        if not user or not user.is_active:
            raise HTTPException(status_code=401)

        return user
    finally:
        db.close()