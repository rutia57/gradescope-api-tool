import datetime

import streamlit as st
from google.cloud import firestore


def stringify_keys(obj):
    if isinstance(obj, dict):
        return {str(k): stringify_keys(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [stringify_keys(x) for x in obj]
    return obj


def save_new_doc_to_firestore(data, doc_name, service_account_json, collection_name):
    db = firestore.Client.from_service_account_json(service_account_json)
    col = db.collection(collection_name)
    doc = col.document(doc_name)
    doc.set(data)


def log_stats(firestore_key_file, firestore_collection_name):
    timestamp_str = datetime.datetime.now().isoformat()
    result = {
        "last_timestamp": timestamp_str,
        "session_id": str(st.session_state.session_id),
        "state_hash": str(st.session_state.state_hash),
        "user_name": st.session_state.gs_conn.name if st.session_state.gs_conn is not None else None,
        "user_email": st.session_state.gs_conn.email if st.session_state.gs_conn is not None else None,
        "selected_course": st.session_state.get("selected_course_name", None),
        "selected_assignment": st.session_state.get("selected_assignment_name", None),
    } | {
        f"{button_name}_button_count": st.session_state.button_click_counts[
            st.session_state.state_hash
        ][button_name]
        for button_name in [
            "download_grade_summary_report",
            "download_grade_feedback_files",
            "download_original_submissions",
            "download_graded_submissions",
            "download_assignment_outline",
        ]
    }
    save_new_doc_to_firestore(
        stringify_keys(result),
        f"metadata_{str(st.session_state.session_id)}_{st.session_state.state_hash}",
        firestore_key_file,
        firestore_collection_name,
    )
