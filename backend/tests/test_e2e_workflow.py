import asyncio
import os
import threading
import time
import uvicorn
import pytest
from backend.app import main


def run_server():
    config = uvicorn.Config(main.app, host="127.0.0.1", port=8000, log_level="warning")
    server = uvicorn.Server(config)
    server.run()


@pytest.fixture(scope="module", autouse=True)
def live_server():
    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()
    # Wait for server to bind
    time.sleep(1.5)
    yield


@pytest.mark.skipif(os.environ.get("CARRYFLOW_PLAYWRIGHT_DISABLED", "").lower() in {"1", "true", "yes"}, reason="Playwright browser execution is disabled in this environment")
def test_complete_priority_workflow_with_playwright():
    main.init()
    main.reset()

    # 1. Open CarryFlow
    st = main.state()
    assert "facts" in st
    print("\n1. CarryFlow state loaded successfully")

    # 2. Load sample user's Smart Personal Data Passport
    started = main.start(main.StartBody(user_goal="Prepare two internship applications using my profile."))
    assert len(started["facts"]) == 8
    print(f"2. Passport loaded with {len(started['facts'])} facts for Aarav Mehta")

    # 3. Select existing internship demo portal (Portal A)
    async def _run_playwright_flow():
        # Start browser
        await main.automation_start()
        status = main.automation_status()
        assert status["running"] is True
        print("3a. Playwright Chromium started")

        # Navigate to Portal A
        nav = await main.automation_navigate(main.AutomationNavigateBody(url="http://127.0.0.1:8000/portal/a"))
        assert nav["ok"] is True
        print("3b. Playwright navigated to Portal A (Northstar Internships)")

        # 4 & 5. Analyse form fields using AI field translator & map to website fields
        prep = main.prepare("a")
        assert len(prep["receipts"]) == 6
        print(f"4 & 5. AI Field Translator generated {len(prep['receipts'])} semantic receipts")

        # 6. Identify missing or conflicting information (introduce and resolve conflict)
        conf = main.conflict()
        assert len(conf["conflicts"]) == 1
        print("6a. Conflict detected on phone number")

        res = main.resolve(main.ResolveBody(fact_id="phone", version=2))
        assert len(res["conflicts"]) == 0
        print("6b. Conflict resolved by user to v2")

        # 7. Use Playwright to actually populate the browser form
        filled = await main.automation_fill()
        assert filled["ok"] is True
        assert len(filled["filled"]) == 6
        assert filled["observed"]["candidate_name"] == "Aarav Mehta"
        assert filled["observed"]["phone"] == "9123456780"
        print(f"7a. Playwright successfully filled {len(filled['filled'])} form fields")
        print("7b. Observed values in real browser:", filled["observed"])

        # Capture screenshot
        shot = await main.automation_screenshot()
        assert shot.status_code == 200
        assert len(shot.body) > 1000
        print("7c. Live browser screenshot captured successfully")

        # 8. User review before submission
        prep_sub = main.submit_prepare()
        action = next(a for a in prep_sub["actions"] if a["action_type"] == "FINAL_SUBMISSION")
        assert action["status"] == "PREPARED"
        print("8a. Final submission prepared for user review")

        approved = main.submit_approve()
        action_app = next(a for a in approved["actions"] if a["action_type"] == "FINAL_SUBMISSION")
        assert action_app["status"] == "VERIFIED"
        assert approved["meta"]["status"] == "COMPLETED"
        print("8b. Final submission approved by user — status COMPLETED")

        await main.automation_stop()
        print("Workflow completed 100% successfully end-to-end!")

    asyncio.run(_run_playwright_flow())
