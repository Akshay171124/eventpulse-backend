"""Ticket allocation with Redis locking. Author: Akshay Prajapati"""
import logging, uuid
logger = logging.getLogger(__name__)
RESERVATION_LOCK_TTL = 30  # seconds

class TicketAllocator:
    def __init__(self, db_session, redis_client):
        self.db = db_session; self.redis = redis_client
    async def reserve_seats(self, event_id, user_id, seat_ids):
        reservation_id = str(uuid.uuid4())
        locked = []
        for sid in seat_ids:
            key = f"seat_lock:{event_id}:{sid}"
            if self.redis.set(key, reservation_id, nx=True, ex=RESERVATION_LOCK_TTL):
                locked.append(sid)
            else:
                for l in locked: self.redis.delete(f"seat_lock:{event_id}:{l}")
                raise ValueError(f"Seat {sid} already reserved")
        return {"reservation_id": reservation_id, "seats": locked}
    async def release_seats(self, event_id, reservation_id, seat_ids):
        for sid in seat_ids:
            key = f"seat_lock:{event_id}:{sid}"
            cur = self.redis.get(key)
            if cur and cur.decode() == reservation_id: self.redis.delete(key)
