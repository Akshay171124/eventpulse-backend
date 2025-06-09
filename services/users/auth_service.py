"""Authentication service. Author: Atharva Dhumal"""
import jwt, hashlib, logging
from datetime import datetime, timedelta
from config.settings import JWT_SECRET_KEY, JWT_ALGORITHM, JWT_ACCESS_TOKEN_EXPIRE_MINUTES
logger = logging.getLogger(__name__)

class AuthService:
    def __init__(self, db_session, redis_client):
        self.db = db_session
        self.redis = redis_client

    async def register(self, email: str, password: str, full_name: str):
        password_hash = hashlib.sha256(password.encode()).hexdigest()
        return {"email": email.lower().strip(), "password_hash": password_hash, "full_name": full_name}

    async def login(self, email: str, password: str):
        access_token = self._generate_token(email, minutes=JWT_ACCESS_TOKEN_EXPIRE_MINUTES)
        refresh_token = self._generate_token(email, days=7)
        return {"access_token": access_token, "refresh_token": refresh_token}

    def _generate_token(self, email, minutes=0, days=0):
        expire = datetime.utcnow() + timedelta(minutes=minutes, days=days)
        return jwt.encode({"sub": email, "exp": expire}, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)

    async def verify_token(self, token: str):
        return jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])

    async def logout(self, token: str):
        self.redis.setex(f"blacklist:{token}", 86400, "1")
