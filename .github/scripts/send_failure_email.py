import argparse
import os
import smtplib
from email.mime.text import MIMEText

parser = argparse.ArgumentParser()
parser.add_argument(
    "--verbose",
    action="store_true",
    help="Print debug information."
)
args = parser.parse_args()

GMAIL_USERNAME = os.environ["GMAIL_USERNAME"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
EMAIL_TO = os.environ["EMAIL_TO"]

if args.verbose:
    print(f"Sending email from {GMAIL_USERNAME} to {EMAIL_TO}")

subject = "🚨 Automatic Gradescope API Tool Streamlit Check Failed"
body = """
Your Streamlit app (https://gradescope-api-tool.streamlit.app/) health check failed.

The scheduled GitHub Action detected that the app did not load correctly or timed out.

Check log here: https://github.com/rutia57/gradescope-api-tool/actions/workflows/ping_streamlit_app.yml
"""

msg = MIMEText(body)
msg["Subject"] = subject
msg["From"] = GMAIL_USERNAME
msg["To"] = EMAIL_TO

with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
    if args.verbose:
        print("Logging in to Gmail...")
        print(GMAIL_USERNAME)
        print(GMAIL_APP_PASSWORD)
        print(EMAIL_TO)
    server.login(GMAIL_USERNAME, GMAIL_APP_PASSWORD)

    if args.verbose:
        print("Sending email...")
    server.sendmail(GMAIL_USERNAME, EMAIL_TO, msg.as_string())

print("Email sent.")
