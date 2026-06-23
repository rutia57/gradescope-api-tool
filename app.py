import streamlit as st
import pandas as pd
import datetime
import streamlit_analytics_custom
import tempfile 
import json 
import os
import uuid
import traceback
from utils import (
    format_course_names,
    format_assignment_names,
    get_full_assignment_info,
    get_student_info,
    get_assignment_questions,
    get_grade_summary,
)
from gradescope_auth import (
    create_new_user, 
    login_with_token, 
    login_temporary, 
    save_profile_for_token,
    cleanup_old_profiles,
)

cleanup_old_profiles()

default_course_option = '<select a course>'
default_assignment_option = '<select an assignment>'

st.set_page_config(page_title="Gradescope API Tool", page_icon="🎓")
st.set_page_config(layout='wide')
st.markdown("# 🎓 Gradescope API Tool")
for var in ['gs_conn', 'secret_token', 'temp_profile_dir', 'selected_course_id', 'selected_assignment_id']:
    if var not in st.session_state:
        st.session_state[var] = None
if 'selected_course_name' not in st.session_state:
    st.session_state['selected_course_name'] = default_course_option
if 'selected_assignment_name' not in st.session_state:
    st.session_state['selected_assignment_name'] = default_assignment_option

for var_int in ['download_grade_summary_report_count', 'download_grade_feedback_files_count', 'download_original_submissions_count', 'download_graded_submissions_count', 'download_assignment_outline_count']: 
    if var_int not in st.session_state: 
        st.session_state[var_int] = 0
if 'session_id' not in st.session_state: 
    st.session_state.session_id = uuid.uuid4()

def update_session_id():
    st.session_state.session_id = uuid.uuid4()

if os.path.exists("firebase-key.json"):
    key_file = "firebase-key.json"
    firestore_collection_name_key = "gradescope-api-streamlit-counts-local"
else:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(json.loads(st.secrets["firebase_key"]), f)
        key_file = f.name
        firestore_collection_name_key = "gradescope-api-streamlit-counts-prod"

# Connecting to Gradescope UI
with st.expander('Connect to Gradescope', expanded=True):
    col1, col2 = st.columns([3, 3])
    with col1:
        st.text("Click here to connect to your Gradescope account. A browser window will open so you can log in.")
        col3, col4 = st.columns([4,4])
        with col3:
            token_input = st.text_input("Secret token", placeholder="Optional")
        with col4: 
            st.caption("")
            st.caption("If you saved a token from a previous session, the app can reuse the associated browser profile so that you don't have to log in again.")
        if st.button("Connect to Gradescope"):
            try:
                with st.spinner("Opening browser and connecting to Gradescope..."):
                    if token_input.strip():
                        token = token_input.strip()
                        conn = login_with_token(token)
                        st.session_state.secret_token = token
                    else:
                        conn, temp_profile_dir = login_temporary()
                        st.session_state.temp_profile_dir = temp_profile_dir
                st.session_state.gs_conn = conn
                st.rerun()
            except Exception as e:
                st.error(f"Login failed: {e}")
    if st.session_state.gs_conn is not None:
        with col2:
            st.success("✅ Successfully connected to Gradescope")
            st.text(f'You are now logged into Gradescope as {st.session_state.gs_conn.name} ({st.session_state.gs_conn.email}).')
            st.caption(
                "Now that you're logged in, you can click the button below to generate a secret token to reuse next time " \
                "you use the app so that you don't have to log in to Gradescope again. If you choose to create a token, save it " \
                "somewhere safe. The app won't store your credentials , and your browser profile will only be accessible by " \
                "entering your secret token. Secret tokens will be automatically deleted 30 days after they're created."
            )
            col5, col6 = st.columns([1,3])
            with col5:
                secret_token_button = st.button("Generate & Show Secret Token")
            with col6:
                if secret_token_button:
                    if st.session_state.temp_profile_dir and not st.session_state.secret_token:
                        st.session_state.secret_token = create_new_user()
                        save_profile_for_token(st.session_state.temp_profile_dir, st.session_state.secret_token)
                    st.code(st.session_state.secret_token)


# Course tools
if st.session_state.gs_conn is not None:
    # Select course
    st.markdown('## Assignment grades & feedback')
    col7, col8 = st.columns([4,4])
    with col7:
        courses = st.session_state.gs_conn.account.get_courses()
        course_name_mapping = format_course_names(courses)
        selected_course = st.selectbox('Select a course to view assignment data:', 
                                    options=[default_course_option] + list(course_name_mapping.keys()),
                                    on_change=update_session_id)
        st.session_state.selected_course_id = course_name_mapping[selected_course] if selected_course in course_name_mapping else None
        st.session_state.selected_course_name = selected_course
    with col8:
        if st.session_state.selected_course_id is not None:
            # Load course assignments
            assignments = st.session_state.gs_conn.account.get_assignments(st.session_state.selected_course_id)
            assignment_name_mapping = format_assignment_names(assignments)
            selected_assignment = st.selectbox('Select an assignment to view grade data:', 
                                    options=[default_assignment_option] + list(assignment_name_mapping.keys()),
                                    on_change=update_session_id)
            st.session_state.selected_assignment_id = assignment_name_mapping[selected_assignment] if selected_assignment in assignment_name_mapping else None
            st.session_state.selected_assignment_name = selected_assignment
            st.session_state.session_id = uuid.uuid4()
        else:
            st.session_state.selected_assignment_id = None
            st.session_state.selected_assignment_name = None
            st.session_state.session_id = uuid.uuid4()


    # Load assignment data
    if st.session_state.selected_assignment_id is not None: 
        with st.spinner('Loading assignment data...'):
            if st.session_state.selected_assignment_id == '<nan>': 
                st.warning('No grade data available for this assignment.')
            else: 
                assignment = [x for x in st.session_state.gs_conn.account.get_assignments(st.session_state.selected_course_id) if x.assignment_id == st.session_state.selected_assignment_id][0]
                submissions = get_full_assignment_info(st.session_state.gs_conn, st.session_state.selected_course_id, st.session_state.selected_assignment_id)
                questions = get_assignment_questions(st.session_state.gs_conn, st.session_state.selected_course_id, st.session_state.selected_assignment_id)

                with st.expander('Assignment summary', expanded=True):
                    release_date_str = (f'{assignment.release_date:%b %-d, %Y}') if assignment.release_date else '–'
                    due_date_str = (f'{assignment.due_date:%b %-d, %Y}') if assignment.due_date else '–'
                    c1, c2, c3, c4, c5, c6, c7, _ = st.columns([3,3,2,2,2,2,2,2])
                    c1.metric("Released Date", release_date_str)
                    c2.metric("Due Date", due_date_str)
                    c3.metric("Questions", len(questions))
                    c4.metric("Total Points", assignment.max_grade)
                    c5.metric("Total submissions", len(submissions['detailed_submissions']))
                    c6.metric("Fully-graded submissions", len([s for s in submissions['detailed_submissions'].values() if s['graded'] and s['grading_progress']==100]))
                    c7.metric("Partially-graded submissions", len([s for s in submissions['detailed_submissions'].values() if s['grading_progress']<100]))

                st.text('The following reports are available to preview and download for this asssignment:')

                # TODO REMOVE THIS
                # st.text(get_assignment_questions(st.session_state.gs_conn, st.session_state.selected_course_id, st.session_state.selected_assignment_id))

                students, max_name_length = get_student_info(st.session_state.gs_conn, st.session_state.selected_course_id)

                st.markdown(f'#### 1. Grade summary spreadsheet')
                st.caption('Table with each student\'s grade breakdown (by question and subquestion), comments, and total grade.')
                with st.expander('Preview grade summary', expanded=False):
                    grade_summary = get_grade_summary(st.session_state.gs_conn, st.session_state.selected_course_id, st.session_state.selected_assignment_id)
                    grade_summary_styled = grade_summary.copy()
                    grade_summary_styled = grade_summary_styled.style.apply(
                        lambda col: ["font-weight: bold; background-color: #e8edff;"]*len(col) if "assignment total" in col.name.lower() 
                        else (["font-weight: bold; background-color: #f0f0f0;"]*len(col) if "total" in col.name.lower() 
                        else (["background-color: #f1e8ff;"]*len(col) if ("email" in col.name.lower() or "student name" in col.name.lower()) else [""] * len(col))), axis=0
                    )
                    st.dataframe(grade_summary_styled, hide_index=True)
                download_grade_summary_report = st.download_button(
                    '**Download grade summary (.csv file)**', 
                    data=grade_summary.to_csv(index=False).encode("utf-8"),
                    file_name=f'{assignment.name.replace(" ","")}_grades_summary_{datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")}.csv'
                )
                if download_grade_summary_report: 
                    st.session_state.download_grade_summary_report_count += 1

                st.markdown(f'#### 2. Grade feedback files for students')
                st.caption('Text files with each student\'s grade breakdown and comments.')
                with st.expander('Select students and preview grade feedback', expanded=False):
                    st.multiselect('Select students', students, default=students, format_func=lambda x: f'{x[1]+" "+x[2]:<{max_name_length+1}} [{x[0]}]', key='selected_students_grades')
                    with st.expander('Preview grade feedback'):
                        st.text(f'selected {len(st.session_state.selected_students_grades)} students:')
                        st.text(st.session_state.selected_students_grades)
                        st.table([[1,2,3,4],[1,2,3,4]]) # TODO
                download_grade_feedback_files = st.download_button(f'**Download grade feedback for selected students ({len(st.session_state.selected_students_grades)}) (.zip containing .txt files)**', 'TODO')
                if download_grade_feedback_files: 
                    st.session_state.download_grade_feedback_files_count += 1

                st.markdown(f'#### 3. Submissions')  
                st.caption('Students\' submitted PDF files and graded PDF files with feedback.')
                with st.expander('Select students and preview submissions data', expanded=False):
                    st.multiselect('Select students', students, default=students, format_func=lambda x: f'{x[1]+" "+x[2]:<{max_name_length+1}} [{x[0]}]', key='selected_students_submissions')
                    with st.expander('Submissions summary'):
                        st.text(f'selected {len(st.session_state.selected_students_submissions)} students:')
                        st.text(st.session_state.selected_students_submissions)
                        st.table([[1,2,3,4],[1,2,3,4]]) # TODO
                download_original_submissions = st.download_button(f'**Download original submissions for selected students ({len(st.session_state.selected_students_submissions)}) (.zip containing .pdf files)**', 'TODO')
                download_graded_submissions = st.download_button(f'**Download graded submissions with feedback for selected students ({len(st.session_state.selected_students_submissions)}) (.zip containing .pdf files)**', 'TODO')
                if download_original_submissions: 
                    st.session_state.download_original_submissions_count += 1
                if download_graded_submissions: 
                    st.session_state.download_graded_submissions_count += 1

                st.markdown(f'#### 4. Assignment outline info')
                st.caption('Table with a summary of the questions on this assignment, including the rubric, possible comments, grader info, and grades stats.')
                with st.expander('Preview assignment outline info', expanded=False):
                    st.table([[1,2,3,4],[1,2,3,4]]) # TODO
                download_assignment_outline = st.download_button('**Download assignment outline info (.csv file)**', 'TODO')
                if download_assignment_outline: 
                    st.session_state.download_assignment_outline_count += 1

try:
    streamlit_analytics_custom.log_user_info(firestore_key_file="firebase-key.json", firestore_collection_name=firestore_collection_name_key)
except Exception:
    st.warning(f'Failed to save app analytics: {traceback.format_exc()}')


st.markdown(
    """<style>li[role="option"], li[role="option"] * {font-family: monospace !important;}
    div[data-testid="stDownloadButton"] button {background-color: #e8fbff; color: black; border: 1px solid #93c5fd;}
    div[data-testid="stDownloadButton"] button:hover {background-color: #bfdbfe;}
    [data-baseweb="tag"] {background-color: #dbeafe !important; color: #1e3a8a !important; border: 1px solid #93c5fd !important; max-width: none !important;}
    [data-baseweb="tag"] span {color: #1e3a8a !important; max-width: none !important; overflow: visible !important; text-overflow: unset !important;}</style>""", 
    unsafe_allow_html=True
)