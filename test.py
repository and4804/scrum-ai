import os
import requests
from dotenv import load_dotenv

load_dotenv()

key = os.getenv("NOTION_API_KEY")
db_id = "3561be702f5680eca62af485b2731007"   # replace with your actual Notion DB id

headers = {
    "Authorization": f"Bearer {key}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json"
}

r = requests.post(
    f"https://api.notion.com/v1/databases/{db_id}/query",
    headers=headers,
    json={"page_size": 1}
)

print("Status:", r.status_code)
print("Response:", r.json())