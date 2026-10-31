"""Read-only schema probe. Run inside the live bot using its existing environment."""
import os
import requests


def probe():
    base = os.environ["LINKWARDEN_API_URL"].rstrip("/")
    headers = {"Authorization": "Bearer " + os.environ["LINKWARDEN_API_KEY"]}
    for resource in ("collections", "tags", "links"):
        try:
            response = requests.get(
                f"{base}/api/v1/{resource}", headers=headers,
                timeout=15, allow_redirects=False,
            )
            print(f"{resource}: HTTP {response.status_code}")
            if response.status_code != 200:
                return False
            body = response.json()
            if not isinstance(body, dict):
                return False
            if resource == "tags":
                page = body.get("data")
                valid = (body.get("success") is True and isinstance(page, dict)
                         and isinstance(page.get("tags"), list) and "nextCursor" in page)
                if not valid:
                    return False
                print("data.tags: list; nextCursor:", type(page["nextCursor"]).__name__)
            else:
                if not isinstance(body.get("response"), list):
                    return False
                print("response: list")
            print("Schema OK")
        except (requests.RequestException, ValueError):
            print("Probe failed; response details omitted.")
            return False
    return True


if __name__ == "__main__":
    raise SystemExit(0 if probe() else 1)
