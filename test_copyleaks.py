import requests

email = "ВАШ_EMAIL_ИЗ_COPYLEAKS"
api_key = "ВАШ_API_KEY_ИЗ_COPYLEAKS"

url_login = "https://id.copyleaks.com/v3/account/login/api"
resp = requests.post(
    url_login,
    json={"email": email, "apiKey": api_key},
    headers={"Content-Type": "application/json"}
)
print("Status code:", resp.status_code)
print("Body:\n", resp.text)
