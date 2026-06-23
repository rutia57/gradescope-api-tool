import datetime
from google.cloud import firestore
import streamlit as st

def stringify_keys(obj):
    if isinstance(obj, dict):
        return {
            str(k): stringify_keys(v)
            for k, v in obj.items()
        }
    elif isinstance(obj, list):
        return [stringify_keys(x) for x in obj]
    return obj


def save_new_doc_to_firestore(data, doc_name, service_account_json, collection_name): 
    db = firestore.Client.from_service_account_json(service_account_json)
    col = db.collection(collection_name)
    doc = col.document(doc_name)
    doc.set(data)


def log_user_info(firestore_key_file, firestore_collection_name): 
    timestamp_str = datetime.datetime.now().isoformat()
    user_info = {
        "last_timestamp": timestamp_str,
        "session_id": str(st.session_state.session_id),
        "user_name": st.session_state.gs_conn.name if st.session_state.gs_conn is not None else None,
        "user_email": st.session_state.gs_conn.email if st.session_state.gs_conn is not None else None,
        "selected_course": st.session_state.get('selected_course_name', None),
        "selected_assignment": st.session_state.get('selected_assignment_name', None),
        'download_grade_summary_report_count': st.session_state.download_grade_summary_report_count,
        'download_grade_feedback_files_count': st.session_state.download_grade_feedback_files_count,
        'download_original_submissions_count': st.session_state.download_original_submissions_count, 
        'download_graded_submissions_count': st.session_state.download_graded_submissions_count, 
        'download_assignment_outline_count': st.session_state.download_assignment_outline_count,
    }
    save_new_doc_to_firestore(stringify_keys(user_info), f'metadata_{str(st.session_state.session_id)}', firestore_key_file, firestore_collection_name)
