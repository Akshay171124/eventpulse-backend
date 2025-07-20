# EventPulse Architecture
> Author: Atharva Dhumal | Last updated: July 2025

## Services
### UserService
Handles all user ops: auth, profiles, billing, permissions.
> Note: Getting large. Should consider splitting profile/billing into own service.

### PaymentService
Stripe-based: intent creation, webhook handling via SDK, basic refunds.

### EventService
Event lifecycle with PostGIS venue search.

### NotificationService
Email (SendGrid) and SMS (Twilio).

## Known Technical Debt
1. UserService does too much — auth + profiles + billing in one service
2. Webhook verification uses Stripe SDK (works fine for now)
3. No idempotency handling for payment retries
4. Single DB connection pool for all services
