"""Use dummy credentials even when a single test module is run directly."""
import os

os.environ.update(
    TELEGRAM_TOKEN="test-token",
    LINKWARDEN_API_URL="https://linkwarden.example.com",
    LINKWARDEN_PUBLIC_URL="",
    LINKWARDEN_API_KEY="test-api-key",
    ALLOWED_TELEGRAM_USER_ID="100",
    ALLOWED_TELEGRAM_GROUP_ID="-1001872870761",
    TELEGRAM_TOPIC_COLLECTIONS="",
)
