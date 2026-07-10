import base64
import datetime
import io
import json
import os
import tempfile
import traceback
import uuid
import warnings
import zipfile
from collections import defaultdict
from google.cloud import firestore

from analytics import (
    log_error,
    log_stats,
    error_logged_section,
)
import streamlit as st
from gradescope_auth import (
    SAMPLE_PLACEHOLDER_GS_CONN,
    login_with_cookies,
    read_session_doc_from_firestore,
)
from st_aggrid import AgGrid  # type: ignore[import-untyped]
from utils import (
    build_feedback_files,
    format_assignment_names,
    format_course_names,
    format_grade_summary_df,
    get_assignment_outline_and_stats,
    get_assignment_questions,
    get_grade_breakdowns,
    get_grade_summary,
    get_graded_submissions_zip_bytes,
    get_grader_by_question_submission,
    get_grades_metadata,
    get_instructor_info,
    get_original_submissions_zip_bytes,
    get_question_to_question_submissions,
    get_raw_data_by_question_submission,
    get_raw_submissions_metadata,
    get_student_info,
    get_student_to_assignment_submissions,
    get_submission_summary,
    get_user_mapping,
    is_arrow_compatible,
    placeholder_assignment_object,
)

warnings.filterwarnings("ignore", message=".*cached function.*widget.*")

st.set_page_config(page_title="Gradescope API Tool", page_icon="extension/icon.png")
st.set_page_config(layout='wide')
st.markdown("# 🎓 Gradescope API Tool")
st.session_state['session_from_ext'] = st.query_params.get("session_from_ext")

if os.path.exists("firebase-key.json"):
    key_file = "firebase-key.json"
    firestore_collection_name_key = "gradescope-api-streamlit-counts-local"
else:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(dict(st.secrets["firebase"]), f)
        key_file = f.name
        if st.query_params.get("automatic_ping") is not None:
            firestore_collection_name_key = "gradescope-api-streamlit-counts-auto"
        else:
            firestore_collection_name_key = "gradescope-api-streamlit-counts-prod"

if 'firestore_db' not in st.session_state:
    st.session_state.firestore_db = firestore.Client.from_service_account_json(key_file) # type: ignore

def show_error(message: str, *, context: str | None = None) -> None:
    log_error(firestore_db=st.session_state.firestore_db, error=message, context=context or "st.error")
    st.error(message)

with error_logged_section(firestore_db=st.session_state.firestore_db, name="Show installation instructions"):
    if st.session_state.session_from_ext is None:
        with st.expander('Installation instructions', expanded=True):
            st.markdown("""
                Welcome to the Gradescope API tool! This tool lets you extract various info about assignments, submissions, grades, etc.
                for courses for which you're an instructor.

                Here's how to use it:

                <div style="margin-left:20px">

                <p>1) Add this extension to Chrome:
                <a href="https://chromewebstore.google.com/detail/nhnebenbafkkclppjgmeegokeikljogo?utm_source=item-share-cb">
                Gradescope API Tool Extension</a>.</p>

                <p>2) The extension should appear in your Chrome toolbar under "Extensions" (the puzzle icon to the right of the address bar). Optionally, pin the extension to your toolbar for easier access.</p>

                <p>3) Go to <a href="https://www.gradescope.com">Gradescope</a> and log in.</p>

                <p>4) Once you're logged in, click the icon of the extension. This will redirect you back to this tool where you can extract grade summary reports, grade feedback files, etc.</p>

                </div>

                Once you have the extension installed, whenever you're logged in on any Gradescope page, you can click on the extension to launch the tool in a new tab.

                In the meantime, you can browse some sample reports below to see what data is available in this tool, what the UI looks like, the format of each of the reports, etc.
                """, unsafe_allow_html=True)

if st.session_state.session_from_ext:
    container = st.container()
else:
    container = st.expander('View sample grade reports')


with container:

    with error_logged_section(firestore_db=st.session_state.firestore_db, name="Build authenticated Gradescope session"):
        if st.session_state.session_from_ext:
            try:
                session_from_ext_id = st.query_params.get("session_from_ext_id")
                if session_from_ext_id is not None:
                    session = read_session_doc_from_firestore(session_id=session_from_ext_id, firestore_db=st.session_state.firestore_db)
                    st.session_state['session_info'] = json.loads(base64.b64decode(session).decode("utf-8"))
                else:
                    st.session_state['session_info'] = json.loads(base64.b64decode(st.session_state.session_from_ext).decode("utf-8"))
            except Exception as e:
                st.error('There was an error logging in to Gradescope. Your session has likely expired; try opening Gradescope '
                'and clicking the extension icon again to create a new session.')

        default_course_option = '<select a course>'
        default_assignment_option = '<select an assignment>'

    with error_logged_section(firestore_db=st.session_state.firestore_db, name="Set default session state values"):
        for var in ['gs_conn', 'selected_course_id', 'selected_assignment_id']:
            if var not in st.session_state:
                st.session_state[var] = None
        if st.session_state.session_from_ext:
            if 'selected_course_name' not in st.session_state:
                st.session_state['selected_course_name'] = default_course_option
            if 'selected_assignment_name' not in st.session_state:
                st.session_state['selected_assignment_name'] = default_assignment_option
        else:
            st.session_state['gs_conn'] = SAMPLE_PLACEHOLDER_GS_CONN
            if 'selected_course_name' not in st.session_state:
                st.session_state['selected_course_name'] = 'TEST'
            if 'selected_assignment_name' not in st.session_state:
                st.session_state['selected_assignment_name'] = 'Assignment 1'


        if 'button_click_counts' not in st.session_state:
            st.session_state['button_click_counts'] = defaultdict(lambda: defaultdict(int))
        if 'session_id' not in st.session_state:
            st.session_state.session_id = uuid.uuid4()
        if 'state_hash' not in st.session_state:
            st.session_state.state_hash = hash(
                f'{st.session_state.gs_conn.name if st.session_state.gs_conn is not None else None}_'
                f'{st.session_state.gs_conn.email if st.session_state.gs_conn is not None else None}_'
                f'{st.session_state.selected_assignment_name}_'
                f'{st.session_state.selected_course_name}_'
            )

        # session vars used for graded zip download
        if "graded_submissions_bytes" not in st.session_state:
            st.session_state.graded_submissions_bytes = None
        if "download_button_disabled" not in st.session_state:
            st.session_state.download_button_disabled = True

        def update_state_hash() -> None:
            st.session_state.state_hash = hash(
                f'{st.session_state.gs_conn.name if st.session_state.gs_conn is not None else None}_'
                f'{st.session_state.gs_conn.email if st.session_state.gs_conn is not None else None}_'
                f'{st.session_state.selected_assignment_name}_'
                f'{st.session_state.selected_course_name}_'
            )

        def update_selected_student_grade_preview() -> None:
            if st.session_state.selected_students_grades:
                st.session_state['selected_student_grade_preview'] = st.session_state.selected_students_grades[0]
            else:
                st.session_state['selected_student_grade_preview'] = None

        def reset_selected_students() -> None:
            st.session_state.reset_student_selection = True
            update_state_hash()
            update_selected_student_grade_preview()

        def increment_button_count(button_name: str) -> None:
            st.session_state.button_click_counts[st.session_state['state_hash']][button_name] += 1

    if st.session_state.session_from_ext:
        try:
            st.session_state.gs_conn, user = login_with_cookies(st.session_state.session_info)
            st.success(f"✅ Successfully logged in to Gradescope as {user['name']} ({user['email']})")
        except Exception as e:
            show_error('There was an error logging in to Gradescope. Your session has likely expired; try opening Gradescope '
            'and clicking the extension icon again to create a new session.')

    # Course tools
    if st.session_state.gs_conn is not None:
        # Select course
        with error_logged_section(firestore_db=st.session_state.firestore_db, name="Load course & assignment info"):

            st.markdown('## Assignment grades & feedback' if st.session_state.session_from_ext else '## Sample assignment grades & feedback')
            if st.session_state.session_from_ext:
                col7, col8 = st.columns([4,4])
                with col7:
                    courses = st.session_state.gs_conn.account.get_courses()
                    course_name_mapping = format_course_names(courses)
                    selected_course = st.selectbox('Select a course to view assignment data:',
                                                options=[default_course_option] + list(course_name_mapping.keys()),
                                                on_change=update_state_hash)
                    st.session_state.selected_course_id = course_name_mapping[selected_course] if selected_course in course_name_mapping else None
                    st.session_state.selected_course_name = selected_course
                with col8:
                    if st.session_state.selected_course_id is not None:
                        # Load course assignments
                        assignments = st.session_state.gs_conn.account.get_assignments(st.session_state.selected_course_id)
                        assignment_name_mapping = format_assignment_names(assignments)
                        selected_assignment = st.selectbox('Select an assignment to view grade data:',
                                                options=[default_assignment_option] + list(assignment_name_mapping.keys()),
                                                on_change=update_state_hash)
                        st.session_state.selected_assignment_id = assignment_name_mapping[selected_assignment] if selected_assignment in assignment_name_mapping else None
                        st.session_state.selected_assignment_name = selected_assignment
                    else:
                        st.session_state.selected_assignment_id = None
                        st.session_state.selected_assignment_name = None
            else:
                assignments = [placeholder_assignment_object]
                st.write('Viewing grade info for Assignment 1 for a sample course. \n\n When you open the tool from an authenticated Gradescope session via the extension, '
                        'you\'ll instead be able to choose a course from your Gradescope courses and an assignment from that course.')

            # Load assignment data
            if st.session_state.selected_assignment_id is not None or st.session_state.session_from_ext is None:
                with st.spinner('Loading assignment data... (for larger courses/assignments, this may take a couple of minutes)', show_time=True):
                    if st.session_state.selected_assignment_id == '<nan>':
                        st.warning('No grade data available for this assignment.')
                    else:
                        try:
                            conn = st.session_state.gs_conn
                            assignment = [x for x in assignments if x.assignment_id == st.session_state.selected_assignment_id][0]
                            assignment_id = st.session_state.selected_assignment_id
                            course_id = st.session_state.selected_course_id

                            with st.spinner('Loading assignment & grade data from Gradescope...', show_time=True):
                                students, max_student_name_length = get_student_info(conn, course_id)
                                instructors = get_instructor_info(conn, course_id)
                                student_mapping = get_user_mapping(students)
                                instructor_mapping = get_user_mapping(instructors)
                                users = students + instructors
                                user_mapping = student_mapping | instructor_mapping
                                questions, questions_order = get_assignment_questions(conn, course_id, assignment_id)
                                raw_submissions_metadata = get_raw_submissions_metadata(conn, course_id, assignment_id)
                                grades_metadata = get_grades_metadata(conn, course_id, assignment_id, instructors, users)
                                student_to_assignment_submissions = get_student_to_assignment_submissions(users, raw_submissions_metadata, grades_metadata)
                                grader_by_question_submission = get_grader_by_question_submission(conn, course_id, questions)
                                question_to_submissions = get_question_to_question_submissions(conn, course_id, questions)
                                comments, total_scores, student_to_question_to_question_submission = get_raw_data_by_question_submission(conn, course_id, users, questions, question_to_submissions, student_to_assignment_submissions)
                                grade_breakdowns = get_grade_breakdowns(users, questions, comments, total_scores, student_to_question_to_question_submission, grader_by_question_submission, questions_order)
                                users_with_grades = [u for u in users if grades_metadata[u.email_address]['submitted']]

                                if st.session_state.pop("reset_student_selection", False):
                                    st.session_state.selected_students_grades = users_with_grades
                                    st.session_state.selected_students_submissions = users_with_grades
                                    st.session_state.selected_student_grade_preview = users_with_grades[0] if users_with_grades else None

                            with error_logged_section(firestore_db=st.session_state.firestore_db, name="Show assignment summary"):

                                with st.expander('Assignment summary', expanded=True):
                                    release_date_str = (f'{assignment.release_date:%b %-d, %Y}') if assignment.release_date else '–'
                                    due_date_str = (f'{assignment.due_date:%b %-d, %Y}') if assignment.due_date else '–'
                                    c1, c2, c3, c4, c5, c6, c7, _ = st.columns([3,3,2,2,2,2,2,2])
                                    c1.metric("Released Date", release_date_str)
                                    c2.metric("Due Date", due_date_str)
                                    c3.metric("Questions", len(questions))
                                    c4.metric("Total Points", assignment.max_grade)
                                    c5.metric("Total submissions", len(raw_submissions_metadata['detailed_submissions']))
                                    c6.metric("Fully-graded submissions", len([s for s in raw_submissions_metadata['detailed_submissions'].values() if s['graded'] and s['grading_progress']==100]))
                                    c7.metric("Partially-graded submissions", len([s for s in raw_submissions_metadata['detailed_submissions'].values() if s['grading_progress']<100]))

                            st.text('The following reports are available to preview and download for this asssignment:')

                            with error_logged_section(firestore_db=st.session_state.firestore_db, name="Load course & assignment info"):
                                st.markdown(f'#### 1. Grade summary spreadsheet')
                                st.caption('Table with each student\'s grade breakdown (by question and subquestion), comments, and total grade.')
                                st.caption('Note: "[^]" before a comment indicates that this comment is linked to a location in the submission PDF')
                                grade_summary = get_grade_summary(
                                    assignment.name,
                                    assignment.due_date.isoformat(sep=' ') if assignment.due_date else '–',
                                    assignment.max_grade,
                                    users,
                                    questions,
                                    user_mapping,
                                    grade_breakdowns,
                                    grades_metadata,
                                    student_to_assignment_submissions,
                                )
                                with error_logged_section(firestore_db=st.session_state.firestore_db, name="Show grade summary preview"):
                                    if st.toggle('Show grade summary preview'):
                                        grade_summary_styled, grid_options, preview_height, custom_css = format_grade_summary_df(grade_summary)
                                        df_pa_compatible, error_message = is_arrow_compatible(grade_summary_styled)
                                        if df_pa_compatible:
                                            AgGrid(
                                                grade_summary_styled,
                                                grid_options,
                                                fit_columns_on_grid_load=True,
                                                height=preview_height,
                                                allow_unsafe_jscode=True,
                                                custom_css=custom_css,
                                                width='50%',
                                            )
                                        else:
                                            st.warning('Error loading preview – download .csv file below to see data.')
                                            with st.expander('Show error traceback:'):
                                                st.code(error_message)

                                download_grade_summary_report = st.download_button(
                                    '**Download grade summary (.csv file)**',
                                    data=grade_summary.to_csv(index=False).encode("utf-8"),
                                    file_name=f'{assignment.name.replace(" ","")}_grades_summary_{datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")}.csv',
                                    on_click=lambda: increment_button_count('download_grade_summary_report'),
                                )

                            with error_logged_section(firestore_db=st.session_state.firestore_db, name="Show grade feedback files section"):
                                st.markdown(f'#### 2. Grade feedback files for students')
                                st.caption('Text files with each student\'s grade breakdown and comments.')
                                st.caption('Note: "[^]" before a comment indicates that this comment is linked to a location in the submission PDF')
                                with st.expander('Select students and preview grade feedback', expanded=False):
                                    st.multiselect('Select students', users_with_grades, default=users_with_grades, format_func=lambda x: f'{x.first_name+" "+x.last_name:<{max_student_name_length+1}} [{x.email_address}]', key='selected_students_grades',
                                                on_change=reset_selected_students)
                                    if st.session_state.selected_students_grades:
                                        with st.expander('Preview grade feedback'):
                                            grade_feedback_strings = build_feedback_files(
                                                assignment.name,
                                                assignment.max_grade,
                                                st.session_state.selected_students_grades,
                                                questions,
                                                user_mapping,
                                                grade_breakdowns,
                                                grades_metadata,
                                            )
                                            st.text(f'Selected {len(st.session_state.selected_students_grades)} students.')
                                            c1, c2 = st.columns([3,10])
                                            with c1:
                                                if 'selected_student_grade_preview' not in st.session_state:
                                                    st.session_state['selected_student_grade_preview'] = st.session_state.selected_students_grades[0]
                                                if st.session_state.selected_student_grade_preview is not None:
                                                    st.text(f'Previewing feedback file for {st.session_state.selected_student_grade_preview.first_name} {st.session_state.selected_student_grade_preview.last_name}:')
                                            with c2:
                                                st.selectbox('Select a student to preview their feedback file:',
                                                    options=st.session_state.selected_students_grades,
                                                    format_func=lambda x: f'{x.first_name+" "+x.last_name:<{max_student_name_length+1}} [{x.email_address}]',
                                                    key='selected_student_grade_preview',
                                                )
                                            st.code(grade_feedback_strings[st.session_state.selected_student_grade_preview.identifier], language=None)
                                            buffer = io.BytesIO()
                                            with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                                                for student_id, text in grade_feedback_strings.items():
                                                    zf.writestr(f'{assignment.name.replace(" ","")}_{user_mapping[student_id].last_name}_{user_mapping[student_id].first_name}_grade_breakdown_and_feedback.txt', text)
                                            grade_feedback_files_zip_file_bytes = buffer.getvalue()
                                    else:
                                        grade_feedback_files_zip_file_bytes = b''
                                download_grade_feedback_files = st.download_button(
                                    f'**Download grade feedback for selected students ({len(st.session_state.selected_students_grades)}) (.zip containing .txt files)**',
                                    grade_feedback_files_zip_file_bytes,
                                    file_name=f'{assignment.name.replace(" ","")}_grade_feedback_files_{datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")}.zip',
                                    on_click=lambda: increment_button_count('download_grade_feedback_files')
                                )

                            with error_logged_section(firestore_db=st.session_state.firestore_db, name="Show submission downloads section"):
                                st.markdown(f'#### 3. Submissions')
                                st.caption('Students\' submitted PDF files and graded PDF files with feedback.')
                                download_original_submissions_container = st.container()
                                with download_original_submissions_container:
                                    download_original_submissions_expander = st.expander('Select students and preview submissions data', expanded=False)
                                    with download_original_submissions_expander:
                                        st.multiselect('Select students', users_with_grades, default=users_with_grades, format_func=lambda x: f'{x.first_name+" "+x.last_name:<{max_student_name_length+1}} [{x.email_address}]', key='selected_students_submissions', on_change=reset_selected_students)
                                export_button_col,c2,c3,_ = st.columns([4,3,1,2])
                                with c3:
                                    success_message_placeholder = st.empty()
                                grades_download_button_slot = st.empty()
                                grades_download_button_slot.link_button(
                                    f'**Download all graded submissions with feedback ({len(users_with_grades)}) (.zip containing .pdf files)**',
                                    url='',
                                    on_click=lambda: increment_button_count('download_graded_submissions'),
                                    disabled=True,
                                    key=str(uuid.uuid4()),
                                )
                                with c2:
                                    progress_placeholder = st.empty()
                                    def progress_cb(n: float) -> None:
                                        progress_placeholder.progress(min(n,1.0))
                                        if n <= 1:
                                            success_message_placeholder.empty()
                                            grades_download_button_slot.link_button(
                                                    f'**Download all graded submissions with feedback ({len(users_with_grades)}) (.zip containing .pdf files)**',
                                                    url='',
                                                    on_click=lambda: increment_button_count('download_graded_submissions'),
                                                    disabled=True,
                                                    key=str(uuid.uuid4()),
                                                )
                                        else:
                                            success_message_placeholder.empty()
                                            success_message_placeholder.success('Export complete!')
                                            if isinstance(st.session_state.graded_submissions_url, bytes):
                                                grades_download_button_slot.download_button(
                                                    f'**Download all graded submissions with feedback ({len(users_with_grades)}) (.zip containing .pdf files)**',
                                                    data=st.session_state.graded_submissions_url,
                                                    on_click=lambda: increment_button_count('download_graded_submissions'),
                                                    file_name=f'{assignment.name.replace(" ","")}_graded_submissions_with_comments_{datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")}.zip',
                                                    disabled=False,
                                                    key=str(uuid.uuid4()),
                                                )
                                            else:
                                                grades_download_button_slot.link_button(
                                                    f'**Download all graded submissions with feedback ({len(users_with_grades)}) (.zip containing .pdf files)**',
                                                    url=st.session_state.graded_submissions_url,
                                                    on_click=lambda: increment_button_count('download_graded_submissions'),
                                                    disabled=False,
                                                    key=str(uuid.uuid4()),
                                                )

                            with error_logged_section(firestore_db=st.session_state.firestore_db, name="Show assignment outline & stats section"):
                                st.markdown(f'#### 4. Assignment outline info & question stats')
                                st.caption('Table with a summary of the questions on this assignment, including the rubric with possible comments, grader info, and grade stats for each question.')
                                with st.expander('Preview assignment outline & question stats', expanded=False):
                                    assignment_outline_and_stats_df = get_assignment_outline_and_stats(questions, questions_order, grade_breakdowns, users_with_grades)
                                    st.markdown(assignment_outline_and_stats_df.map(lambda x: x.replace('\n', '<br>') if isinstance(x, str) else x).to_html(escape=False, index=False), unsafe_allow_html=True)

                                download_assignment_outline = st.download_button(
                                    '**Download assignment outline and question stats (.csv file)**',
                                    assignment_outline_and_stats_df.to_csv(index=False),
                                    file_name=f'{assignment.name.replace(" ","")}_assignment_outline_and_question_stats_{datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")}.csv',
                                    on_click=lambda: increment_button_count('download_assignment_outline'),
                                )

                            with error_logged_section(firestore_db=st.session_state.firestore_db, name="Graded submissions export & download"):
                                with export_button_col:
                                    export_button = st.button(f"Export graded submissions with feedback for all students ({len(users_with_grades)}) (.zip containing .pdf files)")
                                    st.caption('🐌 Warning: This export can take a while (up to ~30 mins if the Gradescope server is busy) for classes with many (80+) students, even if not all students are selected. You\'ll get an email when the export is complete.')
                                    if export_button:
                                        with st.spinner('Downloading graded submissions...', show_time=True):
                                            st.session_state.graded_submissions_url = get_graded_submissions_zip_bytes(
                                                conn,
                                                course_id,
                                                assignment_id,
                                                {submission_id: user_mapping[student_id].first_name.replace(' ','_')+"_"+user_mapping[student_id].last_name.replace(' ','_') for (student_id, submission_id) in student_to_assignment_submissions.items() if submission_id},
                                                assignment.name,
                                                f'{assignment.name.replace(" ","")}_graded_submissions_with_comments_{datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")}',
                                                submission_ids={sub for sub in set([student_to_assignment_submissions[s.identifier] for s in st.session_state.selected_students_submissions]) if sub is not None},
                                                _progress_callback=lambda n: progress_cb(n),
                                            )
                                            progress_cb(1.1)

                                with download_original_submissions_container:
                                    with download_original_submissions_expander:
                                        with st.spinner('Downloading original PDF submissions...', show_time=True):
                                            original_submissions_paths_metadata, successfully_downloaded_original_submission, too_large = get_original_submissions_zip_bytes(
                                                conn,
                                                course_id,
                                                assignment_id,
                                                assignment.name.replace(" ",""),
                                                [
                                                    (
                                                        sid,
                                                        f"{s.first_name.replace(' ', '_')}_{s.last_name.replace(' ', '_')}"
                                                    )
                                                    for s in st.session_state.selected_students_submissions
                                                    if (sid := student_to_assignment_submissions[s.identifier]) is not None
                                                ]
                                            )
                                            if len(too_large) > 0:
                                                st.warning(f'Note: The following {len(too_large)} files failed to download because they\'re too large. You can download '
                                                           f'them manually from the Gradescope website. [{", ".join(too_large)}]')
                                        with st.expander('Submissions summary'):
                                            submission_summary_df = get_submission_summary(st.session_state.selected_students_submissions, grades_metadata, successfully_downloaded_original_submission)
                                            st.markdown(submission_summary_df.map(lambda x: x.replace('\n', '<br>') if isinstance(x, str) else x).to_html(escape=False, index=False, header=False), unsafe_allow_html=True)
                                    if original_submissions_paths_metadata:
                                        c1, c2, c3 = st.columns([5,3,4])
                                        if len(original_submissions_paths_metadata) > 1:
                                            with c2:
                                                st.write(f"Select which part of {len(original_submissions_paths_metadata)} to download (large files are split into multiple parts to avoid failures due to memory constraints):")
                                            with c3:
                                                original_submissions_download_part = st.selectbox(
                                                    label="",
                                                    options=original_submissions_paths_metadata,
                                                format_func=lambda x: f"[Part {x[0]} of {len(original_submissions_paths_metadata)}] ({x[1]} files, {x[2]/(1024*1024):.2f} MB)",
                                                label_visibility="collapsed",
                                            )
                                        else:
                                            original_submissions_download_part = original_submissions_paths_metadata[0]
                                        with c1:
                                            download_original_submissions = st.download_button(
                                                f'**Download original submissions for selected students ({original_submissions_download_part[1]}) (.zip containing .pdf files) (part {original_submissions_download_part[0]} of {len(original_submissions_paths_metadata)})**',
                                                open(original_submissions_download_part[3], 'rb').read(),
                                                file_name=f'{assignment.name.replace(" ","")}_original_submissions_{original_submissions_download_part[0]}_of_{len(original_submissions_paths_metadata)}_{datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")}.zip',
                                                on_click=lambda: increment_button_count('download_original_submissions'),
                                            )

                        except NotImplementedError as e:
                            show_error(str(e))


    try:
        log_stats(firestore_db=st.session_state.firestore_db, firestore_collection_name=firestore_collection_name_key)
    except Exception:
        st.warning(f'Failed to save app analytics: {traceback.format_exc()}')


    st.markdown(
        """<style>li[role="option"], li[role="option"] * {font-family: monospace !important;}
        div[data-testid="stDownloadButton"] button {background-color: #e8fbff; color: black; border: 1px solid #93c5fd;}
        div[data-testid="stDownloadButton"] button:hover {background-color: #bfdbfe;}
        div[data-testid="stLinkButton"] button {background-color: #e8fbff; color: black; border: 1px solid #93c5fd;}
        div[data-testid="stLinkButton"] button:hover {background-color: #bfdbfe;}
        [data-baseweb="tag"] {background-color: #dbeafe !important; color: #1e3a8a !important; border: 1px solid #93c5fd !important; max-width: none !important;}
        [data-baseweb="tag"] span {color: #1e3a8a !important; max-width: none !important; overflow: visible !important; text-overflow: unset !important;}</style>""",
        unsafe_allow_html=True
    )