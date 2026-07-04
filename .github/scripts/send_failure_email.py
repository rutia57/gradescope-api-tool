import os
import smtplib
from email.mime.text import MIMEText

GMAIL_USERNAME = os.environ["GMAIL_USERNAME"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
EMAIL_TO = os.environ["EMAIL_TO"]

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
    server.login(GMAIL_USERNAME, GMAIL_APP_PASSWORD)
    server.sendmail(GMAIL_USERNAME, EMAIL_TO, msg.as_string())

print("Email sent.")