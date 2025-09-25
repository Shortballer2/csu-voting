import os
from datetime import datetime

# Security key
SECRET_KEY = os.getenv("SECRET_KEY", "supersecret")

# Allowed CSU email domain
ALLOWED_DOMAIN = "student.csuniv.edu"

# Voting window (Oct 1, 7 AM – 7 PM)
VOTING_START = datetime(2025, 10, 1, 7, 0, 0)
VOTING_END = datetime(2025, 10, 1, 19, 0, 0)

# Email settings (will pull from Render env variables)
MAIL_SERVER = "smtp.gmail.com"
MAIL_PORT = 587
MAIL_USE_TLS = True
MAIL_USERNAME = os.getenv("EMAIL_USER")
MAIL_PASSWORD = os.getenv("EMAIL_PASS")
