"""Budgets for one-time-code (OTP) endpoints.

Sending a code and checking a code are budgeted separately. They used to share
one budget of 5 requests per IP per hour, so signing up, resending once and
mistyping a code twice locked a person out of verification for an hour. And
because browsers reach the API through Docker's published port, every visitor
shared the gateway IP, so one busy hour locked everyone out.

Brute force on a single code is already stopped by OTP_ATTEMPT_LIMIT (the code
is burned after 5 wrong tries); these budgets only bound volume.
"""

# Sending a code (sign-up, resend, password reset): bounds email volume.
OTP_SEND_IP_LIMIT = 20
OTP_SEND_EMAIL_LIMIT = 6
OTP_SEND_WINDOW_SECONDS = 60 * 60

# Checking a code (verify, reset): bounds guessing across many codes.
OTP_VERIFY_IP_LIMIT = 40
OTP_VERIFY_EMAIL_LIMIT = 15
OTP_VERIFY_WINDOW_SECONDS = 15 * 60

# Backwards-compatible names (the IP send budget).
OTP_IP_LIMIT = OTP_SEND_IP_LIMIT
OTP_IP_WINDOW_SECONDS = OTP_SEND_WINDOW_SECONDS
