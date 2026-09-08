import os
import pickle
import re
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload
from playwright.sync_api import sync_playwright

# --- CONFIGURATION & ENV VARS ---
FIVE9_USER = os.environ.get("FIVE9_USER")
FIVE9_PASS = os.environ.get("FIVE9_PASS")
SALES_FOLDER_ID = "10fCNy7z2nqxbIzGFwP7cRrYQm6PK--zp"
CLIENT_SECRET_FILE = "client_secret.json"
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def get_drive_service():
    """Authenticates using stored token or initiates OAuth flow."""
    creds = None
    if os.path.exists("token.pickle"):
        with open("token.pickle", "rb") as token:
            creds = pickle.load(token)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                CLIENT_SECRET_FILE, DRIVE_SCOPES
            )
            creds = flow.run_local_server(port=0)

        with open("token.pickle", "wb") as token:
            pickle.dump(creds, token)

    return build("drive", "v3", credentials=creds)


def is_already_in_drive(drive_service, call_id):
    """Checks if the file already exists in the destination folder."""
    query = f"'{SALES_FOLDER_ID}' in parents and name contains '{call_id}' and trashed = false"
    results = drive_service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get("files", [])
    return len(files) > 0


def upload_transcript_to_drive(drive_service, file_name, text_content):
    """Uploads transcript text directly from memory into Google Drive."""
    file_metadata = {
        "name": file_name,
        "parents": [SALES_FOLDER_ID]
    }
    media = MediaInMemoryUpload(text_content.encode("utf-8"), mimetype="text/plain")
    uploaded_file = drive_service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id"
    ).execute()
    print(f"Uploaded '{file_name}' to Drive. (ID: {uploaded_file.get('id')})")


def run_extraction():
    print("Authenticating with Google Drive...")
    drive_service = get_drive_service()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        print("Logging into Five9...")
        page.goto("https://admin.us.five9.net/")
        page.get_by_test_id("input").click()
        page.get_by_test_id("input").fill(FIVE9_USER)
        page.get_by_role("button", name="Next").click()
        page.get_by_role("textbox", name="Password Password").click()
        page.get_by_role("textbox", name="Password Password").fill(FIVE9_PASS)
        page.get_by_role("textbox", name="Password Password").press("Enter")
        page.get_by_role("button", name="Sign In").click()

        print("Navigating to AI Insights and setting filters...")
        page.locator("div").filter(has_text=re.compile(r"^AI InsightsExplore actionable AI-driven analytics$")).first.click()

        insight_frame = page.frame_locator('iframe[title="AI Insights"]')
        transcripts_frame = insight_frame.frame_locator('#Transcripts')
        inner_iframe = transcripts_frame.frame_locator('iframe')

        insight_frame.get_by_text("Transcripts").click()

        # Recorded filter interactions
        inner_iframe.get_by_test_id("filter-token").nth(2).click()
        inner_iframe.get_by_test_id("surface-content").get_by_role("combobox", name="any value").click()
        inner_iframe.get_by_role("dialog", name="contains contains Add").get_by_placeholder("any value").fill("Customer Sale")
        inner_iframe.get_by_role("button", name="Done").click()

        inner_iframe.get_by_role("button", name="Last 7 Days").click()
        inner_iframe.get_by_role("menuitem", name="Today").click()

        inner_iframe.get_by_role("button", name="contains Customer Sale").click()
        inner_iframe.locator("div").filter(has_text=re.compile(r"^Customer SaleDelete$")).click()
        inner_iframe.locator("div").filter(has_text=re.compile(r"^Customer SaleDelete$")).click()
        inner_iframe.get_by_role("dialog", name="contains contains Customer").get_by_label("any value").fill("New Sales")
        inner_iframe.get_by_role("button", name="Done").click()

        inner_iframe.get_by_role("button", name="Update").click()

        print("Waiting for grid to load...")
        page.wait_for_timeout(4000)

        call_buttons = inner_iframe.get_by_role("button", name=re.compile(r"^\d{7}$"))
        call_count = call_buttons.count()
        print(f"Found {call_count} calls in the filtered list.")

        for i in range(call_count):
            button = inner_iframe.get_by_role("button", name=re.compile(r"^\d{7}$")).nth(i)
            call_id = button.inner_text().strip()

            # 1. Deduplication Check via Google API
            if is_already_in_drive(drive_service, call_id):
                print(f"[{i+1}/{call_count}] Skipping {call_id}: Already in Drive.")
                continue

            print(f"[{i+1}/{call_count}] Processing {call_id}...")

            # 2. Open Transcript
            button.click()
            inner_iframe.get_by_role("menuitem", name="Explore View Transcript").click()

            transcripts_frame.get_by_test_id("Dropdown").get_by_role("button", name="Transcript").click()

            # 3. Intercept Download
            with page.expect_download() as download_info:
                transcripts_frame.get_by_role("menuitem", name="Download Transcript").click()

            download = download_info.value

            # 4. Save locally (temp), read, upload, delete
            temp_filepath = os.path.join(os.getcwd(), download.suggested_filename)
            download.save_as(temp_filepath)

            with open(temp_filepath, "r", encoding="utf-8") as f:
                file_content = f.read()

            upload_transcript_to_drive(drive_service, download.suggested_filename, file_content)
            os.remove(temp_filepath)

            # 5. Close Modal
            transcripts_frame.get_by_role("button", name="Close").click()
            page.wait_for_timeout(1000)

        context.close()
        browser.close()
        print("Extraction complete.")


if __name__ == "__main__":
    run_extraction()
