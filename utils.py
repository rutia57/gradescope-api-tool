from __future__ import annotations

import datetime
import html
import io
import json
import pickle
import re
import time
import traceback
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from enum import StrEnum
from functools import reduce, wraps
from numbers import Number
from pathlib import Path
from statistics import mean, median
from typing import Literal

import numpy as np
import pandas as pd
import pyarrow as pa  # type: ignore[import-untyped]
import requests
import streamlit as st
from bs4 import BeautifulSoup
from cachetools import TTLCache, cached
from gradescope_auth import SAMPLE_PLACEHOLDER_GS_CONN
from st_aggrid import GridOptionsBuilder, JsCode  # type: ignore[import]


BULLETS = ['•', '◦', '▪']

@dataclass
class RubricItem: 
    rubric_item_id: str 
    question_id: str 
    points: float 
    description: str
    rubric_group_id: str | None
    rubric_group_description: str

@dataclass
class Question: 
    course_id: str
    assignment_id: str
    question_id: str 
    title: str
    scoring_type: Literal['negative', 'positive']
    parent: Question | None 
    children: list[Question] 
    max_grade: float 
    rubric_items: dict[str, RubricItem]

    def __eq__(self, other):
        if not isinstance(other, Question):
            return NotImplemented
        return self.course_id == other.course_id and self.assignment_id == other.assignment_id and self.question_id == other.question_id

    def __hash__(self):
        return hash((self.course_id,self.assignment_id,self.question_id))

@dataclass
class Student: 
    email_address: str
    first_name: str
    last_name: str
    student_id: str 
    user_id: str
    role: str
    @property
    def identifier(self) -> str:
        return self.user_id or self.email_address
    
@dataclass
class RawCommentData: 
    item_id: str 
    description: str
    points: float 
    child_id: str 
    child_description: str
    child_points: float 
    linked: bool

@dataclass
class CommentNode:
    id: str
    description: str
    points: float
    linked: bool = False
    children: dict[str, "CommentNode"] = field(default_factory=dict)

@dataclass
class GradeInfo: 
    total_score: float 
    max_grade: float 
    comments_blurb: str
    question_title: str 
    question_id: str
    grader: str 
    parent_item_id: str 
    parent_item_title: str

def sample_report_available(func):
    @wraps(func)
    def wrapper(_conn, *args, **kwargs):
        if _conn != SAMPLE_PLACEHOLDER_GS_CONN:
            return func(_conn, *args, **kwargs)
        path = Path("sample_reports_data") / f"{func.__name__}.pkl"
        with open(path, "rb") as f:
            return pickle.load(f)
    return wrapper

@dataclass
class PlaceholderAssignment: 
    assignment_id: str | None
    name: str = 'Assignment 1'
    release_date: datetime.datetime = datetime.datetime.strptime("2026-01-28 00:00:00", "%Y-%m-%d %H:%M:%S")
    due_date: datetime.datetime = datetime.datetime.strptime("2026-02-10 00:00:00", "%Y-%m-%d %H:%M:%S")
    max_grade: str = '9.0'

placeholder_assignment_object = PlaceholderAssignment(assignment_id=None)

class Endpoint(StrEnum):
    MEMBERSHIP_ENDPOINT =               "{base_url}/courses/{course_id}/memberships"
    RUBRIC_ENDPOINT =                   "{base_url}/courses/{course_id}/assignments/{assignment_id}/rubric/edit"
    REVIEW_GRADES_ENDPOINT =            "{base_url}/courses/{course_id}/assignments/{assignment_id}/review_grades"
    SUBMISSIONS_ENDPOINT =              "{base_url}/courses/{course_id}/assignments/{assignment_id}/submissions"
    SUBMISSION_ENDPOINT =               "{base_url}/courses/{course_id}/assignments/{assignment_id}/submissions/{submission_id}"
    QUESTION_SUBMISSIONS_ENDPOINT =     "{base_url}/courses/{course_id}/questions/{question_id}/submissions"
    QUESTION_SUBMISSION_ENDPOINT =      "{base_url}/courses/{course_id}/questions/{question_id}/submissions/{question_submission_id}/grade"
    EXPORT_ENDPOINT =                   "{base_url}/courses/{course_id}/assignments/{assignment_id}/export"
    GRADED_SUBMISSIONS_ENDPOINT =       "{base_url}/courses/{course_id}/generated_files/{file_id}/"
    ZIP_FILE_ENDPOINT =                 "{base_url}/courses/{course_id}/assignments/{assignment_id}/export.zip"

def query_endpoint(endpoint: Endpoint, conn, *args, **kwargs):
    url = endpoint.format(base_url=conn.account.gradescope_base_url, *args, **kwargs)
    resp = conn.account.session.get(url)
    return resp.content

############################### Format info for Streamlit ######################################
def format_course_names(courses_dict): 
    course_roles = ['instructor', 'student']
    max_course_id_length = max(set.union(*[{len(k) for k in courses_dict[role].keys()} for role in course_roles]))
    max_course_name_length = max(set.union(*[{len(v.name) for v in courses_dict[role].values()} for role in course_roles]))
    max_course_full_name_length = max(set.union(*[{len(v.full_name) for v in courses_dict[role].values()} for role in course_roles]))
    return reduce(lambda d1, d2: d1 | d2, [{
        f"{'['+course_id+']':<{max_course_id_length+3}}{course.name:<{max_course_name_length+1}}– ".replace(' ','\u00a0') +\
        f"{course.full_name:<{max_course_full_name_length+1}}[{role}]".replace(' ','\u00a0'): course_id 
        for (course_id, course) in courses_dict[role].items()
    } for role in course_roles])

def format_assignment_names(assignments_list): 
    if not [a for a in assignments_list if a.assignment_id]: 
        return {}
    max_assignment_id_length = max(len(a.assignment_id) for a in assignments_list if a.assignment_id)
    return {f"{('['+(a.assignment_id or '<nan>')+']'):<{max_assignment_id_length+4}}{a.name}".replace(' ','\u00a0'):
             (a.assignment_id or '<nan>') for a in assignments_list}

def get_user_mapping(users): 
    return {u.identifier: u for u in users}

@st.cache_data(ttl=3600)
def filter_submission_zip(zip_bytes: bytes, submission_id_to_student_name_mapping, assignment_name, zip_file_name: str, submission_ids: set[str] | None=None) -> bytes:
    input_zip = io.BytesIO(zip_bytes)
    output_zip = io.BytesIO()
    try:
        with zipfile.ZipFile(input_zip, "r") as zin:
            with zipfile.ZipFile(output_zip, "w", compression=zipfile.ZIP_DEFLATED,) as zout:
                for info in zin.infolist():
                    filename = info.filename
                    if filename.endswith("submission_metadata.yml"):
                        zout.writestr(f"{zip_file_name}/{filename.split('/')[-1]}", zin.read(filename))
                        continue
                    basename = filename.rsplit("/", 1)[-1]
                    if submission_ids:
                        if any(submission_id.lower() in basename.lower() for submission_id in submission_ids):
                            submission_id = [submission_id for submission_id in submission_ids if (submission_id.lower() in basename.lower())][0]
                            student_name = submission_id_to_student_name_mapping[submission_id]
                            zout.writestr(f"{zip_file_name}/{assignment_name}_{student_name}_{submission_id}_graded_submission.pdf", zin.read(filename))
                    else: 
                        submission_id = filename.split('/')[-1].split('.')[0]
                        if submission_id in submission_id_to_student_name_mapping:
                            student_name = submission_id_to_student_name_mapping[submission_id]
                            zout.writestr(f"{zip_file_name}/{assignment_name}_{student_name}_{submission_id}_graded_submission.pdf", zin.read(filename))
                        else: 
                            zout.writestr(f"{zip_file_name}/{filename.split('/')[-1]}", zin.read(filename))
        return output_zip.getvalue()
    except Exception:
        print(zip_bytes)
        traceback.print_exc()
        return b''
            
def ignore_some_args(conn, course_id, assignment_id, progress_callback):
    return hash((course_id, assignment_id))

def format_name(s): 
    if s.first_name and s.last_name:
        return s.first_name, s.last_name
    name_parts = s.full_name.split(' ')
    if len(name_parts) <= 1:
        return '', s.full_name
    elif len(name_parts) == 2: 
        return f'{name_parts[0]}', f'{name_parts[1]}'
    else: 
        return f'{" ".join(name_parts[0:-1])}', f'{name_parts[-1]}'

############################# Get submission files from Gradescope #############################
@st.cache_data(ttl=3600, hash_funcs={Question: lambda q: (q.course_id, q.assignment_id, q.question_id)})
def get_submission_original_pdf_bytes(_conn, course_id, assignment_id, submission_id): 
    submission_endpoint = f"{_conn.account.gradescope_base_url}/courses/{course_id}/assignments/{assignment_id}/submissions/{submission_id}"
    resp = _conn.account.session.get(submission_endpoint)
    resp_json = resp.json()
    if 'pdf_attachment' in resp_json and resp_json['pdf_attachment'] is not None:
        pdf_url = resp_json['pdf_attachment']['url']
        pdf_resp = requests.get(pdf_url)
        return pdf_resp.content
    return None

@st.cache_data(ttl=3600)
def get_original_submissions_zip_bytes(_conn, course_id, assignment_id, assignment_name, submission_ids_and_student_names): 
    if _conn == SAMPLE_PLACEHOLDER_GS_CONN: 
        output_zip = io.BytesIO()
        with open("sample_reports_data/get_original_submissions_zip_bytes.pkl", "rb") as f:
            zip_bytes_original, _ = pickle.load(f)
            input_zip = io.BytesIO(zip_bytes_original)
            with zipfile.ZipFile(input_zip, "r") as zin:
                with zipfile.ZipFile(output_zip, "w", compression=zipfile.ZIP_DEFLATED,) as zout:
                    for info in zin.infolist():
                        filename = info.filename
                        submission_id = filename.split('_')[-3]
                        if submission_id in (s[0] for s in submission_ids_and_student_names):
                            zout.writestr(filename, zin.read(filename))
        output_zip.seek(0)
        return output_zip.getvalue(), {s[1] for s in submission_ids_and_student_names}

    output_zip = io.BytesIO()
    successfully_downloaded = set()
    for submission_id, student_name in submission_ids_and_student_names:
        pdf_bytes = get_submission_original_pdf_bytes(_conn, course_id, assignment_id, submission_id)
        if pdf_bytes:
            with zipfile.ZipFile(output_zip, "a", compression=zipfile.ZIP_DEFLATED) as zout:
                filename = f'{assignment_name}_{student_name}_{submission_id}_original_submission.pdf'
                zout.writestr(filename, pdf_bytes)
                successfully_downloaded.add(student_name)
    output_zip.seek(0)
    return output_zip.getvalue(), successfully_downloaded

@sample_report_available
@cached(cache=TTLCache(maxsize=100, ttl=3600), key=ignore_some_args)
def get_graded_submission_zip_bytes_helper(_conn, course_id, assignment_id, progress_callback):
    review_grades_url = f"{_conn.account.gradescope_base_url}/courses/{course_id}/assignments/{assignment_id}/review_grades"
    review_grades_resp = _conn.account.session.get(review_grades_url)
    csrf = BeautifulSoup(review_grades_resp.text, "html.parser").find("meta", {"name": "csrf-token"})["content"]
    headers = {"X-CSRF-Token": csrf, "X-Requested-With": "XMLHttpRequest", "Referer": review_grades_url, "Origin": "https://www.gradescope.com"}
    export_endpoint = f"{_conn.account.gradescope_base_url}/courses/{course_id}/assignments/{assignment_id}/export"
    resp = _conn.account.session.post(export_endpoint, headers=headers)
    file_id = resp.json()["generated_file_id"]
    # poll
    while True:
        graded_submissions_endpoint = f"{_conn.account.gradescope_base_url}/courses/{course_id}/generated_files/{file_id}/"
        resp = _conn.account.session.get(graded_submissions_endpoint)
        file_status_data = resp.json()
        if progress_callback:
            progress_callback(file_status_data["progress"])
        if file_status_data["status"] == "completed":
            progress_callback(1.0)
            break
        time.sleep(1)
    # download full .zip with all students 
    zip_file_url = f"{_conn.account.gradescope_base_url}/courses/{course_id}/assignments/{assignment_id}/export.zip"
    for _ in range(10):
        try: 
            resp = _conn.account.session.get(zip_file_url)
            zipfile.ZipFile(io.BytesIO(resp.content), "r")
            return resp.content
        except Exception:
            time.sleep(1)
    return b'' 

def get_graded_submissions_zip_bytes(_conn, course_id, assignment_id, submission_id_to_student_name_mapping, assignment_name, zip_file_name: str, submission_ids=None, _progress_callback=None): 
    zip_bytes = get_graded_submission_zip_bytes_helper(_conn, course_id, assignment_id, _progress_callback)
    return filter_submission_zip(zip_bytes, submission_id_to_student_name_mapping, assignment_name, zip_file_name, submission_ids)

############################### Extract raw data from Gradescope ################################
@sample_report_available
@st.cache_data(ttl=3600)
def get_raw_submissions_metadata(_conn, course_id, assignment_id): 
    # submissions metadata incl. IDs, time submitted, grading progress
    submissions_endpoint = f"{_conn.account.gradescope_base_url}/courses/{course_id}/assignments/{assignment_id}/submissions"
    resp = _conn.account.session.get(submissions_endpoint)
    return resp.json()

@sample_report_available
@st.cache_data(ttl=3600)
def get_grades_metadata(_conn, course_id, assignment_id, instructors, users):
    # submissions grades metadata incl. total grade, submitted or not, and timestamp
    review_grades_endpoint = f'{_conn.account.gradescope_base_url}/courses/{course_id}/assignments/{assignment_id}/review_grades'
    resp = _conn.account.session.get(review_grades_endpoint)
    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table", class_="js-reviewGradesTable")
    
    headers = {th.get_text(" ", strip=True).lower(): i for i, th in enumerate(table.find("thead").find_all("th"))}

    def header_substr_to_cell(cells, header_substr):
        for h in headers:
            if header_substr.lower() in h.lower():
                return cells[headers[h]]
        return None
        
    tbody = table.find("tbody")
    default_instructor_results = {i.email_address: {"submission_id": None, "student_name": format_name(i), "score": None, "submitted": False, "submitted_at": None} for i in instructors}
    results = {}
    for row in tbody.find_all("tr"):
        cells = row.find_all("td")
        name_cell = header_substr_to_cell(cells, 'name')
        time_cell = header_substr_to_cell(cells, 'time')
        link = name_cell.find("a")
        if link is None:
            student_name = name_cell.get_text(strip=True)
            email = header_substr_to_cell(cells, 'email').get_text(strip=True)
            if any(u.email_address == email for u in users):
                results[email] = {"submission_id": None, "student_name": student_name, "score": None, "submitted": False, "submitted_at": None,}
            continue
        href = link["href"]
        submission_id = re.search(r"/submissions/(\d+)", href).group(1)
        student_name = link.get_text(strip=True)
        email = header_substr_to_cell(cells, 'email').get_text(strip=True)
        score = float(header_substr_to_cell(cells, 'score').get_text(strip=True)) if header_substr_to_cell(cells, 'score').get_text(strip=True) else None
        submitted_at = datetime.datetime.strptime(time_cell.find("time")["datetime"], "%Y-%m-%d %H:%M:%S %z")
        if any(u.email_address == email for u in users):
            results[email] = {"submission_id": submission_id, "student_name": student_name, "score": score, "submitted": True, "submitted_at": submitted_at}
    return default_instructor_results | results

@sample_report_available
@st.cache_data(ttl=3600)
def get_student_info(_conn, course_id) -> tuple[list[Student], int]: 
    # student metadata incl. name, ID, email
    members_list = _conn.account.get_course_users(course_id)
    if not members_list: 
        raise NotImplementedError("Grade breakdown analysis is currently only implemented for courses for which you are an instructor.")
    max_student_name_len = max([len(format_name(s)[0])+len(format_name(s)[1])+1 for s in members_list if s.role=='Student'])
    return (
        sorted([Student(
            s.email, 
            format_name(s)[0], 
            format_name(s)[1], 
            s.sid, 
            s.user_id,
            'Student',
        ) for s in members_list if s.role=='Student'], key=lambda x: (x.last_name,x.first_name)), 
        max_student_name_len
    )

@sample_report_available
@st.cache_data(ttl=3600)
def get_instructor_info(_conn, course_id) -> list[Student]: 
    # student metadata incl. name, ID, email
    members_list = _conn.account.get_course_users(course_id)
    if not members_list: 
        raise NotImplementedError("Grade breakdown analysis is currently only implemented for courses for which you are an instructor.")
    return sorted([Student(
            s.email, 
            format_name(s)[0], 
            format_name(s)[1], 
            s.sid, 
            s.user_id,
            'Instructor',
        ) for s in members_list if s.role!='Student'], key=lambda x: (x.last_name,x.first_name))

@sample_report_available
@st.cache_data(ttl=3600)
def get_assignment_questions(_conn, course_id, assignment_id): 
    # question info incl. outline, max grade, available rubric items, scoring type, etc.
    rubric_endpoint = f"{_conn.account.gradescope_base_url}/courses/{course_id}/assignments/{assignment_id}/rubric/edit"
    response = _conn.account.session.get(rubric_endpoint)
    if response.status_code != 200:
        raise NotImplementedError("Grade breakdown analysis is currently only implemented for courses for which you are an instructor.")
    soup = BeautifulSoup(response.text, "html.parser")
    element = soup.find("div", {"data-react-class": "AssignmentRubric"})
    props = json.loads(element["data-react-props"])
    qs = dict()
    qs_order = []
    for q in props['questions']: 
        qs_order.append(str(q['id']))
        qs[str(q['id'])] =  Question(
            course_id=course_id, 
            assignment_id=assignment_id, 
            question_id=str(q['id']), 
            title=q['title'], 
            parent=None, 
            children=[], 
            max_grade=float(q['weight']), 
            rubric_items={}, 
            scoring_type=q['scoring_type']
        )
        if q['children']:
            for c in q['children']:
                qs_order.append(str(c['id']))
                qs[str(c['id'])] = Question(
                    course_id=course_id, 
                    assignment_id=assignment_id, 
                    question_id=str(c['id']), 
                    title=c['title'], 
                    parent=qs[str(q['id'])], 
                    children=[], 
                    max_grade=float(c['weight']), 
                    rubric_items={}, 
                    scoring_type=q['scoring_type']
                )
                qs[str(q['id'])].children.append(qs[str(c['id'])])
    for r in props['rubric_items']:
        qs[str(r['question_id'])].rubric_items[str(r['id'])] = RubricItem(
            rubric_item_id=str(r['id']), 
            question_id=str(r['question_id']), 
            points=float(r['weight']) * (1 if qs[str(r['question_id'])].scoring_type=='positive' else -1), 
            description=r['description'],
            rubric_group_id=str(r['group_id']) if r['group_id'] else None, 
            rubric_group_description=[g for g in props['rubric_item_groups'] if g['id']==r['group_id']][0]['description'] if r['group_id'] else None
        )
    return qs, qs_order

@st.cache_data(ttl=3600)
def load_single_question_submission_data(_conn, course_id, question_id, question_submission_id):
    # helper function (used for parallelization) for scraping data for a single question submission
    url = f"{_conn.account.gradescope_base_url}/courses/{course_id}/questions/{question_id}/submissions/{question_submission_id}/grade"
    resp = _conn.account.session.get(url)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    div = soup.find("div", attrs={"data-react-props": True})
    data = json.loads(html.unescape(div["data-react-props"]))
    assignment_submission_id = str(data["submission"]["assignment_submission_id"])
    return assignment_submission_id, question_id, question_submission_id, data

@sample_report_available
@st.cache_data(ttl=3600, hash_funcs={Question: lambda q: (q.course_id, q.assignment_id, q.question_id)})
def get_grader_by_question_submission(_conn, course_id, questions):
    # get (most recent) grader for each question submission (one per question per student)
    grader_by_question_submission = {}
    for question_id in questions: 
        url = f'{_conn.account.gradescope_base_url}/courses/{course_id}/questions/{question_id}/submissions'
        resp = _conn.account.session.get(url)
        soup = BeautifulSoup(resp.text, "html.parser")
        table = soup.find("table", id="question_submissions")
        if not table: 
            continue
        grader_by_question_submission[question_id] = {}
        for row in table.find_all("tr")[1:]:   # skip header
            cells = row.find_all("td")
            link = cells[1].find("a")["href"]
            question_submission_id = str(re.search(r"/submissions/(\d+)/grade", link).group(1))
            grader = cells[2].get_text(strip=True)
            grader_by_question_submission[question_id][question_submission_id] = grader
    return grader_by_question_submission

@sample_report_available
@st.cache_data(ttl=3600, hash_funcs={Question: lambda q: (q.course_id, q.assignment_id, q.question_id)})
def get_question_to_question_submissions(_conn, course_id, questions):
    question_to_submissions = defaultdict(list)
    for question_id in questions:
        url = f"{_conn.account.gradescope_base_url}/courses/{course_id}/questions/{question_id}/submissions/"
        resp = _conn.account.session.get(url)
        soup = BeautifulSoup(resp.text, "html.parser")
        table = soup.find("table", id="question_submissions")
        if table is None:
            continue
        for a in table.select("td.table--primaryLink a"):
            submission_id = a["href"].split("/submissions/")[1].split("/")[0]
            question_to_submissions[question_id].append(submission_id)
    return question_to_submissions

@sample_report_available
@st.cache_data(ttl=3600, hash_funcs={Question: lambda q: (q.course_id, q.assignment_id, q.question_id)})
def get_raw_data_by_question_submission(_conn, course_id, students, questions, question_to_submissions, student_to_assignment_submissions):
    # given question metadata and mapping of IDs, extracts all grade and comment info
    assignment_submission_to_question_submissions = {s: {} for s in set(student_to_assignment_submissions.values()) if s is not None}
    question_submission_to_comment_data = {}

    futures = []
    with ThreadPoolExecutor(max_workers=16) as executor:
        for question_id in questions:
            for question_submission_id in question_to_submissions[question_id]:
                futures.append(executor.submit(load_single_question_submission_data, _conn, course_id, question_id, question_submission_id))
        for future in as_completed(futures):
            assignment_submission_id, question_id, question_submission_id, comment_data = future.result()
            if assignment_submission_id in assignment_submission_to_question_submissions:
                assignment_submission_to_question_submissions[assignment_submission_id][question_id] = question_submission_id
            question_submission_to_comment_data[question_submission_id] = comment_data

    comments, total_scores, student_to_question_to_question_submission = {}, {}, {}
    for student in students:
        student_id = student.identifier
        comments[student_id], total_scores[student_id], student_to_question_to_question_submission[student_id] = {}, {}, {}
        for question_id in questions:
            comments[student_id][question_id] = []
            total_scores[student_id][question_id] = None
            assignment_submission_id = student_to_assignment_submissions[student_id]
            if assignment_submission_id is not None:
                if not questions[question_id].children:
                    question_submission_id = assignment_submission_to_question_submissions[assignment_submission_id][question_id]
                    student_to_question_to_question_submission[student_id][question_id] = question_submission_id
                    data = question_submission_to_comment_data[question_submission_id]
                    total_scores[student_id][question_id] = float(data['submission']['score']) if data['submission']['score'] else None
                    for rubric_item_id, present in data['rubric_item_applications'].items(): 
                        if present: 
                            rubric_item = questions[question_id].rubric_items[str(rubric_item_id)]
                            if rubric_item.rubric_group_id: 
                                comments[student_id][question_id].append(RawCommentData(rubric_item.rubric_group_id, rubric_item.rubric_group_description, 0.0, rubric_item.rubric_item_id, rubric_item.description, rubric_item.points, False))
                            else:
                                comments[student_id][question_id].append(RawCommentData(rubric_item.rubric_item_id, rubric_item.description, rubric_item.points, None, None, None, False))
                    for annotation in data['annotations']: 
                        if annotation['links']:
                            for link in annotation['links']:
                                if link['linkable_type'] == 'RubricItem': 
                                    rubric_item = questions[question_id].rubric_items[str(link['rubric_item']['id'])]
                                    comments[student_id][question_id].append(RawCommentData(rubric_item.rubric_item_id, rubric_item.description, rubric_item.points, None, None, None, True))
                                    if link['annotation_comments']:
                                        for annotation_comment in link['annotation_comments']:
                                            comments[student_id][question_id].append(RawCommentData(rubric_item.rubric_item_id, rubric_item.description, rubric_item.points, f'{rubric_item.rubric_item_id}_comment', annotation_comment, None, False))
                        elif annotation['content']: 
                            comments[student_id][question_id].append(RawCommentData(None, annotation['content'], None, None, None, None, True))
                    if data['evaluation']:
                        if data['evaluation']['comments'] or data['evaluation']['points']: 
                            comments[student_id][question_id].append(RawCommentData(None, data['evaluation']['comments'] or '[point adjustment for this submission]', float(data['evaluation']['points']) if data['evaluation']['points'] else 0.0, None, None, None, False))
    return comments, total_scores, student_to_question_to_question_submission

######################## Reorganize data into more convenient structures ########################
def get_student_to_assignment_submissions(students, submission_metadata, grades_metadata):
    student_to_assignment_submissions = {student.identifier: None for student in students}
    for submission_id, submission_info in submission_metadata['detailed_submissions'].items():
        for user in submission_info['active_user_ids']: 
            if str(user) in student_to_assignment_submissions:
                student_to_assignment_submissions[str(user)] = str(submission_id)
            elif any(data['submission_id']==str(submission_id) for (_id, data) in grades_metadata.items()):
                matching_grade_info = [_id for (_id, data) in grades_metadata.items() if data['submission_id']==str(submission_id)][0]
                student_to_assignment_submissions[matching_grade_info] = str(submission_id)
    return student_to_assignment_submissions

def build_tree(rows):
    # helper function to build reduced tree of comments with full info from unstructured parsed partial info data
    def make_id(id_, description):
        return id_ if id_ is not None else f"{hash(description)}"
    nodes = {}
    def get_node(id_, description, points, linked):
        node_id = make_id(id_, description)
        if node_id not in nodes:
            nodes[node_id] = CommentNode(id=node_id, description=description, points=points, linked=linked)
        else:
            node = nodes[node_id]
            if description is not None:
                node.description = description
            if points is not None:
                node.points = points
            node.linked |= linked
        return nodes[node_id]
    has_parent = set()
    for raw_comment_data in rows:
        parent = get_node(raw_comment_data.item_id, raw_comment_data.description, raw_comment_data.points, raw_comment_data.linked)
        if raw_comment_data.child_description is None:
            continue
        child = get_node(raw_comment_data.child_id, raw_comment_data.child_description, raw_comment_data.child_points, False)
        parent.children[child.id] = child
        has_parent.add(child.id)
    return [node for node_id, node in nodes.items() if node_id not in has_parent]

def format_tree(roots):
    # format tree of comments & points for a question into a blurb of ordered comments by line
    def subtree_has_points(node):
        return (node.points is not None and node.points != 0) or any(subtree_has_points(child) for child in node.children.values())
    def format_node(node, indent=0, extra_indent=0):
        prefix = ""
        if node.linked:
            prefix += "[^] "
        if node.points is not None and node.points > 0:
            prefix += f"+{node.points} "
        elif node.points is not None and node.points < 0:
            prefix += f"{node.points} "
        else: 
            prefix += f"{BULLETS[indent % len(BULLETS)]} "
        line = "    " * indent + " " * extra_indent + prefix + node.description
        children = list(node.children.values())
        # Stable partition: scored subtrees first, preserving relative order.
        children.sort(key=lambda c: not subtree_has_points(c))
        lines = [line]
        for child in children:
            lines.extend(format_node(child, indent + 1, extra_indent=(5 if node.linked else 0)))
        return lines
    roots = list(roots)
    roots.sort(key=lambda r: not subtree_has_points(r))
    return "\n".join(line for root in roots for line in format_node(root))

def get_grade_breakdowns(students, questions, comments, total_scores, student_to_question_to_question_submission, grader_by_question_submission, questions_order): 
    # gets list of GradeInfo objects for all questions for each student 
    results = {}
    for student in students:
        student_id = student.identifier
        results[student_id] = []
        if student_id in comments:
            for question_id in questions:
                if not questions[question_id].parent:
                    if not questions[question_id].children: 
                        if question_id in comments[student_id]:
                            results[student_id].append(GradeInfo(
                                total_scores[student_id][question_id],
                                questions[question_id].max_grade,
                                format_tree(build_tree(comments[student_id][question_id])),
                                questions[question_id].title,
                                question_id,
                                grader_by_question_submission[question_id][student_to_question_to_question_submission[student_id][question_id]] if question_id in student_to_question_to_question_submission[student_id] else None,
                                question_id, 
                                questions[question_id].title
                            ))
                    else: 
                        for child in questions[question_id].children:
                            if child.question_id in comments[student_id]:
                                results[student_id].append(GradeInfo(
                                    total_scores[student_id][child.question_id],
                                    questions[child.question_id].max_grade,
                                    format_tree(build_tree(comments[student_id][child.question_id])),
                                    questions[child.question_id].title,
                                    child.question_id,
                                    grader_by_question_submission[child.question_id][student_to_question_to_question_submission[student_id][child.question_id]] if child.question_id in student_to_question_to_question_submission[student_id] else None,
                                    question_id, 
                                    questions[question_id].title
                                ))
        results[student_id] = sorted(results[student_id], key=lambda g: questions_order.index(g.parent_item_id) if g.parent_item_id in questions_order else len(results[student_id]))
    return results

############################### Format extracted data into reports ###############################
def get_grade_summary(assignment_title, assignment_due_date, assignment_max_grade, selected_students, questions, student_mapping, grade_breakdowns, grades_metadata, student_to_assignment_submissions):
    cols = [f"{assignment_title}\nDue {assignment_due_date}\n\nName", "Email", "Submitted", "Submission ID"]
    cols_set = set(cols)
    rows = []
    for student_id, grade_breakdown in grade_breakdowns.items(): 
        student = student_mapping[student_id]
        if student_id in set(s.identifier for s in selected_students): 
            if student.role == 'Student' or grades_metadata[student.email_address]['submitted']:
                if not grades_metadata[student.email_address]['submitted']: 
                    rows.append({f"{assignment_title}\nDue {assignment_due_date}\n\nName": student.first_name + " " + student.last_name, "Email": student.email_address, "Notes": "No submission"})
                    continue
                row = {f"{assignment_title}\nDue {assignment_due_date}\n\nName": student.first_name + " " + student.last_name, "Email": student.email_address}
                row['Submitted'] = grades_metadata[student.email_address]['submitted_at'].isoformat(sep=' ')
                row['Submission ID'] = student_to_assignment_submissions[student_id]
                for parent_question_id, parent_question_title in list(dict.fromkeys([(grade_info.parent_item_id, grade_info.parent_item_title) for grade_info in grade_breakdown])):
                    score = 0
                    for grade_info in [g for g in grade_breakdown if g.parent_item_id == parent_question_id]:
                        row[f'{grade_info.question_title}:\nGrade\n/{grade_info.max_grade}'] = grade_info.total_score
                        row[f'{grade_info.question_title}:\nComments'] = grade_info.comments_blurb
                        row[f'{grade_info.question_title}:\nGrader'] = grade_info.grader
                        score += (grade_info.total_score or 0.0)
                    row[f'{parent_question_title} TOTAL\n/{questions[parent_question_id].max_grade}'] = score
                row[f'Assignment TOTAL\n/{assignment_max_grade}'] = grades_metadata[student.email_address]["score"]
                for k in row: 
                    if k not in cols_set: 
                        cols_set.add(k)
                        cols.append(k) 
                rows.append(row)
    cols.append('Notes')
    return pd.DataFrame(rows, columns=cols)

def build_feedback_files(assignment_title, assignment_max_grade, selected_students, questions, student_mapping, grade_breakdowns, grades_metadata): 
    feedback_file_strs = {}
    for student_id, grade_breakdown in grade_breakdowns.items(): 
        student = student_mapping[student_id]
        if student_id in set(s.identifier for s in selected_students):
            if grades_metadata[student.email_address]['submitted']:
                feedback = f"{assignment_title} Grade Feedback\nStudent Name: {student.first_name} {student.last_name}\nStudent Email: {student.email_address}\n\n"""
                for parent_question_id, parent_question_title in list(dict.fromkeys([(grade_info.parent_item_id, grade_info.parent_item_title) for grade_info in grade_breakdown])):
                    score = 0
                    for grade_info in [g for g in grade_breakdown if g.parent_item_id == parent_question_id]:
                        feedback += f"{grade_info.question_title}:      {grade_info.total_score} / {grade_info.max_grade}      (graded by: {grade_info.grader})\n"
                        for line in grade_info.comments_blurb.split('\n'):
                            feedback += f"\t{line}\n"
                        feedback += '\n'
                        score += (grade_info.total_score or 0.0)
                    feedback += f'{parent_question_title} TOTAL:      {score} / {questions[parent_question_id].max_grade}\n'
                    feedback += '\n'
                feedback += f'Assignment TOTAL:      {grades_metadata[student.email_address]["score"]} / {assignment_max_grade}'
                feedback_file_strs[student.identifier] = feedback
    return feedback_file_strs

def get_assignment_outline_and_stats(questions, questions_order, grade_breakdowns, users_with_grades): 
    def outline(question):
        path = [question.title]
        p = question.parent
        while p is not None:
            path = [p.title] + path
            p = p.parent
        return '\n'.join(["--"*i+p for (i,p) in enumerate(path)])
    def rubric_string(question):
        if not question.rubric_items:
            return ""
        items = list(question.rubric_items.values())
        groups = {}
        ungrouped = []
        for item in items:
            if item.rubric_group_id is None:
                ungrouped.append(item)
            else:
                groups.setdefault(
                    item.rubric_group_id,
                    {
                        "description": item.rubric_group_description,
                        "items": [],
                    },
                )["items"].append(item)
        def has_points(group):
            return group.points != 0 if isinstance(group, RubricItem) else any(item.points != 0 for item in group["items"])
        def format_item(item, indent=0):
            if item.points > 0:
                prefix = f"+{item.points:g} "
            elif item.points < 0:
                prefix = f"{item.points:g} "
            else:
                prefix = "+0 " if question.scoring_type == "positive" else f"{BULLETS[indent % len(BULLETS)]} "
            return "    " * indent + prefix + item.description
        # Stable partition: scored things first
        group_list = list(groups.values())
        all_items = ungrouped + group_list
        all_items.sort(key=lambda g: not has_points(g))
        lines = []
        for item in all_items:
            if isinstance(item, RubricItem):
                lines.append(format_item(item))
            else:
                lines.append(f"{BULLETS[0]} {item['description']}")
                item["items"].sort(key=lambda i: i.points == 0)
                for item in item["items"]:
                    lines.append(format_item(item, indent=1))
        return "\n".join(lines)
    def stats_string(scores):
        scores = [x for x in scores if x is not None]
        if not scores:
            return ""
        return f"Count: {len(scores)}\nMean: {mean(scores):.2f}\nMedian: {median(scores):.2f}"
    def grader_stats_string(grader_scores):
        pieces = []
        for grader in sorted(grader_scores):
            scores = [x for x in grader_scores[grader] if x is not None]
            if not scores:
                continue
            pieces.append(f"{grader}\n  Count: {len(scores)}\n  Mean: {mean(scores):.2f}\n  Median: {median(scores):.2f}")
        return "\n\n".join(pieces)
    rows = []
    for question in questions_order:
        scores = []
        grader_scores = defaultdict(list)
        for user in users_with_grades:
            if not any(g.question_id == question for g in grade_breakdowns[user.identifier]):
                continue
            breakdown = [g for g in grade_breakdowns[user.identifier] if g.question_id == question][0]
            score = breakdown.total_score
            grader = breakdown.grader
            scores.append(score)
            grader_name = (
                grader.full_name
                if hasattr(grader, "full_name")
                else getattr(grader, "email_address", str(grader))
            )
            grader_scores[grader_name].append(score)
        rows.append(
            {
                "Question": questions[question].title,
                "Outline": outline(questions[question]),
                "Max Points": questions[question].max_grade,
                "Rubric": rubric_string(questions[question]),
                "Stats": stats_string(scores),
                "Stats by grader": grader_stats_string(grader_scores),
            }
        )
    return pd.DataFrame(rows)

def get_submission_summary(selected_students, grades_metadata, downloaded_pdf_students):
    grades = []
    pdfs_available = 0
    for student in selected_students:
        meta = grades_metadata[student.email_address]
        if meta["score"] is not None:
            grades.append(meta["score"])
        if (student.first_name+'_'+student.last_name) in downloaded_pdf_students:
            pdfs_available += 1
    n = len(selected_students)
    def fmt(x):
        return f"{x:.2f}" if isinstance(x, float) else x
    rows = [
        ("Students selected", n),
        ("Submission PDFs available", f"{pdfs_available} / {n}"),
        ("Mean grade", fmt(mean(grades)) if grades else "—"),
        ("Median grade", fmt(median(grades)) if grades else "—"),
        ("Minimum grade", fmt(min(grades)) if grades else "—"),
        ("Maximum grade", fmt(max(grades)) if grades else "—"),
    ]
    return pd.DataFrame(rows, columns=["Statistic", "Value"])

############################### Streamlit styling utils ###########################################
multiline_renderer = JsCode("""
    class MultilineRenderer {
        init(params) {                
            this.eGui = document.createElement("div");
            this.eGui.style.whiteSpace = "pre";
            this.eGui.style.overflow = "auto";
            this.eGui.style.height = "100%";
            this.eGui.style.maxHeight = "100%";
            this.eGui.style.lineHeight = "18px";
            this.eGui.style.padding = "4px";
            this.eGui.style.boxSizing = "border-box";
            this.eGui.textContent = params.value ?? "";
        }

        getGui() {
            return this.eGui;
        }
    }
""")

assignment_total_style = JsCode("""
function(params) {
    return {
        'fontWeight': 'bold',
        'backgroundColor': '#e8edff'
    };
}
""")

total_style = JsCode("""
function(params) {
    return {
        'fontWeight': 'bold',
        'backgroundColor': '#f0f0f0'
    };
}
""")

metadata_style = JsCode("""
function(params) {
    return {
        'backgroundColor': '#f1e8ff'
    };
}
""")

def make_aggrid_safe(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    def safe_cell(x):
        if pd.isna(x):
            return None
        if isinstance(x, (np.integer, np.floating, np.bool_)):
            return x.item()
        if isinstance(x, (pd.Timestamp, datetime.datetime, datetime.date)):
            return x.isoformat(sep=' ')
        if isinstance(x, (dict, list, tuple, set)):
            return str(x)
        return x
    return df.map(safe_cell)

def format_grade_summary_df(df):
    grade_summary_styled = df.copy()

    for col in grade_summary_styled.columns:
        s = grade_summary_styled[col]
        non_null = s.dropna()
        if non_null.empty:
            continue
        if non_null.map(lambda x: isinstance(x, Number)).all():
            grade_summary_styled[col] = pd.to_numeric(s, errors="coerce")
        else:
            grade_summary_styled[col] = s.fillna("").astype("string")

    grade_summary_styled["_rowHeight"] = grade_summary_styled.astype(str).map(lambda s: min(5, str(s).count("\n")+1)).max(axis=1)*30
    grade_summary_styled = make_aggrid_safe(grade_summary_styled)
    
    for col in grade_summary_styled.columns:
        grade_summary_styled[col] = grade_summary_styled[col].astype(object)
        grade_summary_styled.loc[grade_summary_styled[col].isna(), col] = None

    gb = GridOptionsBuilder.from_dataframe(grade_summary_styled)
    gb.configure_default_column(resizable=True, sortable=True, filter=True,
                                cellRenderer=multiline_renderer,
                                wrapText=False,
        wrapHeaderText=True,
        autoHeaderHeight=True,
    )
    for col in grade_summary_styled.columns:
        name = col.lower()
        if "assignment total" in name:
            gb.configure_column(col, cellStyle=assignment_total_style)
        elif "total" in name:
            gb.configure_column(col, cellStyle=total_style)
        elif any(x in name for x in ("email","due","submitted","submission id")):
            gb.configure_column(col, cellStyle=metadata_style)
        header_width = max(len(line) for line in str(col).split("\n"))+3
        if any(x in name for x in ("email","due","submitted","submission id")):
            cell_width = max([0 if x is None else max([len(line) for line in str(x).split("\n")], default=0) for x in grade_summary_styled[col]], default=0)
            width = max(header_width, cell_width)
        elif "comments" in name: 
            cell_width = max([0 if x is None else max([len(line) for line in str(x).split("\n")], default=0) for x in grade_summary_styled[col]], default=0)
            width = min(30, max(header_width, cell_width))
        else:
            width = header_width
        gb.configure_column(col, width=width*8+24)
    grid_options = gb.build()
    grid_options["getRowHeight"] = JsCode("""function(params) {return params.data._rowHeight;}""")
    grid_options["suppressColumnVirtualisation"] = True
    grid_options["domLayout"] = "normal"
    grid_options["suppressHorizontalScroll"] = False
    grid_options["suppressAutoSize"] = False
    grid_options["suppressSizeToFit"] = True
    grid_options["defaultColDef"] = grid_options["defaultColDef"] | {"suppressSizeToFit": True}
    for col in grid_options["columnDefs"]:
        if col["field"] == "_rowHeight":
            col["hide"] = True
    preview_height = int((grade_summary_styled.iloc[:5].astype(str).map(lambda s: min(5, str(s).count("\n")+1)).max(axis=1)*22).sum())+5*22
    custom_css = {".ag-header-cell-label": {"justify-content": "flex-start",}, ".ag-header-cell-text": {"white-space": "pre-line","text-align": "left",}}
    return grade_summary_styled, grid_options, preview_height, custom_css

def is_arrow_compatible(df):
    try:
        pa.Table.from_pandas(df)
        for col in df.columns:
            json.dumps(df[col].tolist(), allow_nan=False)[:100]
        st.session_state.df_pa_compatible_count = st.session_state.get('df_pa_compatible_count',0)+1 
        return True, None
    except Exception:
        return False, traceback.format_exc()
    
