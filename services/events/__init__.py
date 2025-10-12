"""
Events module — core event management, venue search, and ticket allocation.

Maintained by: Akshay Prajapati (@Akshay171124)
"""

from services.events.event_service import EventService
from services.events.venue_search import VenueSearchService
from services.events.ticket_allocator import TicketAllocator

__all__ = [
    "EventService",
    "VenueSearchService",
    "TicketAllocator",
]
