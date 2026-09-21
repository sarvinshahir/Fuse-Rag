import os
import msal
import requests
from dotenv import load_dotenv

load_dotenv()

ROB_EMAIL = "rsafrata@fuse-power.com"
SCOPES = ["Files.Read.All"]
DELTA_TOKEN_FILE = "delta_token.txt"

def create_msal_app():
    return msal.PublicClientApplication(
        client_id=os.getenv("AZURE_CLIENT_ID"),
        authority=f"https://login.microsoftonline.com/{os.getenv('AZURE_TENANT_ID')}"
    )

def get_access_token(msal_app):
    accounts = msal_app.get_accounts()
    if accounts:
        result = msal_app.acquire_token_silent(SCOPES, account=accounts[0])
        if result and "access_token" in result:
            return result["access_token"]
    result = msal_app.acquire_token_interactive(scopes=SCOPES)
    return result["access_token"]

def get_delta_token(token, user_id):
    headers = {"Authorization": f"Bearer {token}"}
    # Start delta query from root
    url = f"https://graph.microsoft.com/v1.0/users/{user_id}/drive/root/delta"
    delta_token = None

    print("Fetching delta token — paging through all files...")
    page = 0
    while url:
        response = requests.get(url, headers=headers, timeout=30).json()
        page += 1
        print(f"  Page {page}...")

        # Next page or delta token
        url = response.get("@odata.nextLink")
        if not url:
            # Last page — extract delta token
            delta_link = response.get("@odata.deltaLink", "")
            if "token=" in delta_link:
                delta_token = delta_link.split("token=")[-1]

    return delta_token

def main():
    msal_app = create_msal_app()
    token = get_access_token(msal_app)

    print(f"Getting delta token for {ROB_EMAIL}...")
    delta_token = get_delta_token(token, ROB_EMAIL)

    if delta_token:
        with open(DELTA_TOKEN_FILE, "w") as f:
            f.write(delta_token)
        print(f"Delta token saved to {DELTA_TOKEN_FILE}")
        print("Future ingestion runs will only process changed files.")
    else:
        print("ERROR: Could not get delta token")

if __name__ == "__main__":
    main()