import os
import pickle
import re
from datetime import datetime
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload
from playwright.sync_api import sync_playwright

# --- CONFIGURATION & ENV VARS ---
FIVE9_USER = os.environ.get("FIVE9_USER")
FIVE9_PASS = os.environ.get("FIVE9_PASS")

# Target Google Drive Folder ID
GOOGLE_FOLDER_ID = "10fCNy7z2nqxbIzGFwP7cRrYQm6PK--zp"
CLIENT_SECRET_FILE = "client_secret.json"
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def get_drive_service():
    """Authenticates using stored token or refreshes it safely without hanging CI workflows."""
    creds = None

    if os.path.exists("token.pickle") and os.path.getsize("token.pickle") > 0:
        try:
            with open("token.pickle", "rb") as token:
                creds = pickle.load(token)
        except Exception as e:
            print(f"Error loading token.pickle: {e}", flush=True)
            creds = None
    else:
        raise RuntimeError("token.pickle is missing or 0 bytes! Check TOKEN_PICKLE_B64 in GitHub Secrets.")

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                print("Google OAuth token expired. Refreshing token...", flush=True)
                creds.refresh(Request())
                with open("token.pickle", "wb") as token:
                    pickle.dump(creds, token)
                print("Google OAuth token refreshed successfully.", flush=True)
            except Exception as refresh_err:
                raise RuntimeError(
                    f"Failed to refresh Google OAuth token: {refresh_err}. Please re-generate token.pickle locally."
                )
        else:
            raise RuntimeError(
                "Google OAuth credentials invalid/missing refresh_token. Re-generate TOKEN_PICKLE_B64 locally."
            )

    return build("drive", "v3", credentials=creds)


def is_already_in_drive(drive_service, call_id):
    """Checks the target folder to avoid downloading duplicates."""
    query = f"'{GOOGLE_FOLDER_ID}' in parents and name contains '{call_id}' and trashed = false"
    results = drive_service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get("files", [])
    return len(files) > 0


def upload_transcript_to_drive(drive_service, file_name, text_content):
    """Uploads transcript text directly from memory into Google Drive."""
    file_metadata = {
        "name": file_name,
        "parents": [GOOGLE_FOLDER_ID]
    }
    media = MediaInMemoryUpload(text_content.encode("utf-8"), mimetype="text/plain")
    uploaded_file = drive_service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id"
    ).execute()
    print(f"Uploaded '{file_name}' to Drive. (ID: {uploaded_file.get('id')})", flush=True)


def run_hourly_extraction():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Launching Playwright browser...", flush=True)
    drive_service = get_drive_service()

    with sync_playwright() as p:
        headless_mode = os.environ.get("HEADLESS_MODE", "true").lower() == "true"
        browser = p.chromium.launch(headless=headless_mode)

        if os.path.exists("state.json"):
            context = browser.new_context(storage_state="state.json", accept_downloads=True)
        else:
            context = browser.new_context(accept_downloads=True)

        page = context.new_page()

        try:
            print("Navigating to Five9 Admin Console...", flush=True)
            page.goto("https://admin.us.five9.net/", wait_until="networkidle")
            page.wait_for_timeout(3000)

            # --- LOGIN DETECTOR ---
            dashboard_visible = page.get_by_text("AI Insights").first.is_visible()

            if not dashboard_visible:
                print("Dashboard not found. Login required. Entering credentials...", flush=True)
                page.get_by_test_id("input").click()
                page.get_by_test_id("input").fill(FIVE9_USER)
                page.get_by_role("button", name="Next").click()
                page.wait_for_timeout(2000)

                password_field = page.get_by_role("textbox", name=re.compile("Password"))
                password_field.click()
                password_field.fill(FIVE9_PASS)

                page.get_by_role("button", name="Sign In").click()

                print("Credentials submitted. Waiting for dashboard...", flush=True)
                page.wait_for_timeout(8000)

                context.storage_state(path="state.json")
                print("Login complete. Saved updated session state.", flush=True)
            else:
                print("Active session detected! AI Insights is visible.", flush=True)

            print("Navigating to AI Insights...", flush=True)
            if page.locator(".HomeCard-icon").first.is_visible():
                page.locator(".HomeCard-icon").first.click(force=True)

            page.goto("https://admin.us.five9.net/ai-insights", wait_until="domcontentloaded")
            page.wait_for_timeout(6000)

            # --- DEFINE IFRAME HIERARCHY ---
            ai_frame = page.frame_locator('iframe[title="AI Insights"]')
            transcripts_frame = ai_frame.frame_locator('#Transcripts')
            grid_frame = transcripts_frame.frame_locator('iframe')

            print("Navigating to Transcripts tab...", flush=True)
            ai_frame.get_by_role("menuitem", name="Transcripts").click()
            page.wait_for_timeout(5000)

            # --- APPLY FILTERS ---
            print("Applying custom filters...", flush=True)

            # 1. Disposition / Token Filter Setup
            try:
                grid_frame.get_by_test_id('filter-token').nth(2).click()
                page.wait_for_timeout(1000)
                grid_frame.locator("svg, input[type='checkbox']").nth(0).click(force=True)
                grid_frame.locator("svg, input[type='checkbox']").nth(1).click(force=True)
                grid_frame.get_by_role('button', name='Done').click()
                page.wait_for_timeout(1000)
            except Exception as e_f1:
                print(f"Filter 1 note: {e_f1}", flush=True)

            # 2. Talk Time Filter (Set >= 180 seconds)
            print("Setting Talk Time filter to >= 180 seconds...", flush=True)
            try:
                grid_frame.get_by_role('button', name=re.compile(r'is >=|Talk Time')).click()
                page.wait_for_timeout(500)
                num_input = grid_frame.get_by_test_id('single-number')
                num_input.click()
                num_input.fill("180")
                page.wait_for_timeout(500)
                grid_frame.get_by_role('button', name='Update').click()
                page.wait_for_timeout(3000)
            except Exception as e_f2:
                print(f"Talk time filter note: {e_f2}", flush=True)

            # 3. Exclude Dispositions ("doesn't contain" filters)
            try:
                if grid_frame.get_by_role('button', name='is any value').nth(4).is_visible():
                    grid_frame.get_by_role('button', name='is any value').nth(4).click()
                    page.wait_for_timeout(500)
                    grid_frame.get_by_text("doesn't contain").first.click()
                    page.wait_for_timeout(500)

                    search_box = grid_frame.get_by_placeholder("any value").first
                    if search_box.is_visible():
                        search_box.fill("dr")
                        page.wait_for_timeout(500)
                        grid_frame.locator("svg, input[type='checkbox']").first.click(force=True)

                        search_box.fill("could")
                        page.wait_for_timeout(500)
                        grid_frame.locator("svg, input[type='checkbox']").first.click(force=True)

                    grid_frame.get_by_role('button', name='Done').click()
                    grid_frame.get_by_role('button', name='Update').click()
                    page.wait_for_timeout(3000)
            except Exception as e_f3:
                print(f"Exclusion filter note: {e_f3}", flush=True)

            print("Filters applied! Waiting for grid to settle...", flush=True)
            page.wait_for_timeout(6000)

            # --- ITERATIVE SCROLL AND PROCESS LOOP ---
            print("Processing virtualized grid items...", flush=True)
            processed_call_ids = set()
            consecutive_empty_scrolls = 0

            while consecutive_empty_scrolls < 8:
                visible_ids = grid_frame.locator("button, a, [role='gridcell'], [role='button'], .ag-cell").evaluate_all(r"""
                    (elements) => elements
                        .map(el => el.innerText.trim())
                        .map(text => {
                            let match = text.match(/\b(\d{7})\b/);
                            return match ? match[1] : null;
                        })
                        .filter(id => id !== null)
                """)

                unprocessed_ids = [cid for cid in visible_ids if cid not in processed_call_ids]

                if unprocessed_ids:
                    call_id = unprocessed_ids[0]
                    processed_call_ids.add(call_id)
                    consecutive_empty_scrolls = 0

                    print(f"\n--- Processing Call ID: {call_id} ---", flush=True)

                    if is_already_in_drive(drive_service, call_id):
                        print(f"Skipping Call ID {call_id} (Already exists in Google Drive).", flush=True)
                        continue

                    try:
                        btn = grid_frame.get_by_role("button", name=re.compile(call_id)).first
                        btn.scroll_into_view_if_needed()
                        btn.click(force=True)
                        page.wait_for_timeout(1500)

                        view_btn = grid_frame.get_by_role("menuitem", name=re.compile("Explore View Transcript|View Transcript"))
                        view_btn.click()

                        # --- LOAD TIME ALLOWANCE FOR TRANSCRIPTS ---
                        print(f"Waiting 5 seconds for Call ID {call_id} transcript to load...", flush=True)
                        page.wait_for_timeout(5000)

                        transcripts_frame.get_by_test_id("Dropdown").get_by_role("button", name="Transcript").click()
                        page.wait_for_timeout(1000)

                        with page.expect_download(timeout=20000) as download_info:
                            transcripts_frame.get_by_role("menuitem", name="Download Transcript").click()

                        download = download_info.value
                        temp_filepath = os.path.join(os.getcwd(), download.suggested_filename)
                        download.save_as(temp_filepath)

                        with open(temp_filepath, "r", encoding="utf-8") as f:
                            file_content = f.read()

                        upload_transcript_to_drive(drive_service, download.suggested_filename, file_content)
                        os.remove(temp_filepath)

                        close_btn = transcripts_frame.get_by_role("button", name="Close")
                        cancel_btn = transcripts_frame.get_by_role("button", name="Cancel")

                        if close_btn.count() > 0 and close_btn.first.is_visible():
                            close_btn.first.click()
                        elif cancel_btn.count() > 0 and cancel_btn.first.is_visible():
                            cancel_btn.first.click()
                        else:
                            page.keyboard.press("Escape")

                        page.wait_for_timeout(1500)

                    except Exception as ex:
                        print(f"Failed to process Call ID {call_id}. Error: {ex}", flush=True)
                        if not page.is_closed():
                            page.keyboard.press("Escape")
                            page.wait_for_timeout(1500)

                else:
                    print("No new calls visible. Scrolling down to load more...", flush=True)
                    grid_frame.locator("body").click(force=True)
                    page.mouse.wheel(delta_x=0, delta_y=600)

                    consecutive_empty_scrolls += 1
                    page.wait_for_timeout(3000)

            print(f"\nSUCCESS! Completed extraction. Processed {len(processed_call_ids)} total calls.", flush=True)

        except Exception as e:
            print(f"Navigation error: {e}", flush=True)
            if not page.is_closed():
                page.screenshot(path="error_screenshot.png")

        finally:
            browser.close()


if __name__ == "__main__":
    run_hourly_extraction()
