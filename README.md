# EventPulse

**Real-time event ticketing platform built for scale.**

EventPulse is a full-featured event management and ticketing system designed to handle high-throughput ticket sales, real-time venue availability, and seamless payment processing via Stripe. Built with FastAPI, PostgreSQL, Redis, and Celery.

---

## Features

- **Event Management** -- Create, update, and manage events with rich metadata, categories, and media attachments.
- **Venue & Seating** -- GIS-powered venue search with interactive seat maps and capacity management via GeoAlchemy2.
- **Ticket Allocation** -- Atomic seat reservation with Redis-backed distributed locks to prevent overselling.
- **Payments** -- Stripe integration with support for split payments, partial refunds, promo codes, and automatic payout scheduling.
- **Authentication** -- JWT-based auth with role-based access control (organizer, attendee, admin).
- **Notifications** -- Transactional emails (SendGrid) and SMS (Twilio) for order confirmations, reminders, and event updates.
- **Search** -- Full-text and geospatial event search with configurable radius and category filters.
- **Background Jobs** -- Celery workers for async tasks: refund reconciliation, notification dispatch, report generation.

## Tech Stack

| Layer         | Technology                        |
|---------------|-----------------------------------|
| API Framework | FastAPI 0.104                     |
| Database      | PostgreSQL 15 + SQLAlchemy 2.0    |
| Cache / Locks | Redis 7                           |
| Payments      | Stripe API v7                     |
| Task Queue    | Celery 5.3 + Redis broker         |
| Email         | SendGrid                          |
| SMS           | Twilio                            |
| GIS           | GeoAlchemy2 + PostGIS             |

## Getting Started

### Prerequisites

- Python 3.11+
- PostgreSQL 15 with PostGIS extension
- Redis 7+
- Stripe account (test keys are fine for development)

### Installation

```bash
git clone https://github.com/your-org/eventpulse-backend.git
cd eventpulse-backend
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Configuration

Copy the example environment file and fill in your credentials:

```bash
cp .env.example .env
```

Required environment variables:

| Variable              | Description                          |
|-----------------------|--------------------------------------|
| `DATABASE_URL`        | PostgreSQL connection string         |
| `REDIS_URL`           | Redis connection string              |
| `STRIPE_SECRET_KEY`   | Stripe secret API key                |
| `STRIPE_WEBHOOK_SECRET` | Stripe webhook signing secret     |
| `SENDGRID_API_KEY`    | SendGrid API key                     |
| `TWILIO_ACCOUNT_SID`  | Twilio account SID                   |
| `TWILIO_AUTH_TOKEN`   | Twilio auth token                    |
| `JWT_SECRET`          | Secret for signing JWT tokens        |

### Running Locally

```bash
# Start the API server
uvicorn app.main:app --reload --port 8000

# Start Celery worker (separate terminal)
celery -A app.worker worker --loglevel=info

# Run database migrations
alembic upgrade head
```

### Running Tests

```bash
pytest tests/ -v --cov=app
```

## API Documentation

Once the server is running, interactive API docs are available at:

- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

## Project Structure

```
eventpulse-backend/
├── app/
│   ├── main.py
│   ├── models/
│   ├── routes/
│   ├── services/
│   ├── schemas/
│   └── worker.py
├── config/
│   ├── settings.py
│   └── stripe_config.py
├── migrations/
├── tests/
├── docs/
└── requirements.txt
```

## Contributing

1. Create a feature branch from `main`
2. Write tests for new functionality
3. Ensure `pytest` and `ruff` pass before opening a PR
4. Request review from at least one team member

## License

Proprietary -- Internal use only.
