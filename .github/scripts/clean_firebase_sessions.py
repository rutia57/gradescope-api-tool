import os
from datetime import datetime, timezone
from google.cloud import firestore

db = firestore.Client.from_service_account_json(os.environ["GOOGLE_APPLICATION_CREDENTIALS"]) # type: ignore

def cleanup_expired_sessions() -> None:
    now = datetime.now(timezone.utc)
    expired_docs = (
        db.collection("sessions")
        .where("expires", "<=", now)
        .stream()
    )
    for doc in expired_docs:
        doc.reference.delete()

cleanup_expired_sessions()