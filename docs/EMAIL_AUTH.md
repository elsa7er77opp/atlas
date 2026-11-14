# Email-only account verification and recovery

Atlas now supports public Viewer registration and email-based verification and password recovery. New public accounts are created as inactive `viewer` accounts. They become active only after the six-digit email verification code is accepted. Administrator-created accounts continue to follow the administrator-only user lifecycle rules.

## Development mode

The API defaults to `EMAIL_MODE=console`. This means no external email provider is called. For local testing only, set `EMAIL_DEV_LOG_CODE=true`; the API log will include the development code. Do not use that setting in a real deployment.

## SMTP mode

For actual delivery, set the following values in the local `.env` file and never commit or send that file:

```env
EMAIL_MODE=smtp
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USER=your-smtp-user
SMTP_PASSWORD=your-smtp-password
SMTP_FROM=Atlas <no-reply@example.com>
SMTP_USE_TLS=true
EMAIL_DEV_LOG_CODE=false
OTP_TTL_MIN=10
OTP_MAX_ATTEMPTS=5
```

The implementation uses Python SMTP with STARTTLS when enabled. A delivery failure is logged without the plaintext code; the API still returns a generic response so account existence is not disclosed.

## Apply the database migration

The existing PostgreSQL volume does not automatically re-run initialization files. Apply the idempotent API migration once from the project root:

```bash
set -a; source .env; set +a
docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  < infra/postgres/init/02_api.sql
```

This adds `users.email_verified_at`, a unique normalized-email index, and the hashed `auth_challenges` table. OTP plaintext is never persisted or written to audit logs.

## User flow

The login screen now exposes `Create an account` and `Forgot password?`. Registration requires username, email, and a password and always creates the Viewer role. The verification screen consumes a one-time code. The reset flow accepts a one-time code and a new password, then revokes all refresh sessions for that user.

Responses for unknown or ineligible email addresses are intentionally generic. This prevents the endpoint from becoming an account-enumeration oracle. Codes expire after `OTP_TTL_MIN` minutes and are limited by `OTP_MAX_ATTEMPTS`.

## Administrator policy

Creating an Administrator is not available through public registration. An existing Administrator may manage ordinary users through the authenticated Users screen; bootstrap and out-of-band administrator recovery remain separate operational paths.
