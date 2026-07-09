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
from typing import Any, Callable, Literal, TypeVar

import numpy as np
import pandas as pd
import pyarrow as pa  # type: ignore[import-untyped]
import requests
import streamlit as st
from bs4 import BeautifulSoup, Tag
from cachetools import TTLCache, cached
from gradescopeapi.classes.member import Member
from gradescopeapi.classes.assignments import Assignment
from gradescope_auth import SAMPLE_PLACEHOLDER_GS_CONN, GSConnectionFromSession as Conn
from st_aggrid import GridOptionsBuilder, JsCode  # type: ignore[import-untyped]


BULLETS = ['•', '◦', '▪']

@dataclass
class RubricItem:
    rubric_item_id: str
    question_id: str
    points: float
    description: str
    rubric_group_id: str | None
    rubric_group_description: str | None

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

    def __eq__(self: Question, other: object) -> bool:
        if not isinstance(other, Question):
            return NotImplemented
        return self.course_id == other.course_id and self.assignment_id == other.assignment_id and self.question_id == other.question_id

    def __hash__(self: Question) -> int:
        return hash((self.course_id,self.assignment_id,self.question_id))

@dataclass
class Student:
    email_address: str
    first_name: str
    last_name: str
    student_id: str
    user_id: str | None
    role: str
    @property
    def identifier(self: Student) -> str:
        return self.user_id or self.email_address

@dataclass
class RawCommentData:
    item_id: str | None
    description: str | None
    points: float | None
    child_id: str | None
    child_description: str | None
    child_points: float | None
    linked: bool

@dataclass
class CommentNode:
    id: str
    description: str | None
    points: float | None
    linked: bool = False
    children: dict[str, "CommentNode"] = field(default_factory=dict)

@dataclass
class GradeInfo:
    total_score: float
    max_grade: float
    comments_blurb: str
    question_title: str
    question_id: str
    grader: str | None
    parent_item_id: str
    parent_item_title: str

F = TypeVar("F", bound=Callable[..., Any])

def sample_report_available(func: F) -> F:
    @wraps(func)
    def wrapper(_conn: Conn, *args: Any, **kwargs: Any) -> Any:
        if _conn != SAMPLE_PLACEHOLDER_GS_CONN:
            return func(_conn, *args, **kwargs)
        path = Path("sample_reports_data") / f"{func.__name__}.pkl"
        with open(path, "rb") as f:
            return pickle.load(f)
    return wrapper # type: ignore

@dataclass
class PlaceholderAssignment:
    assignment_id: str | None
    name: str = 'Assignment 1'
    release_date: datetime.datetime = datetime.datetime.strptime("2026-01-28 00:00:00", "%Y-%m-%d %H:%M:%S")
    due_date: datetime.datetime = datetime.datetime.strptime("2026-02-10 00:00:00", "%Y-%m-%d %H:%M:%S")
    max_grade: str = '9.0'

placeholder_assignment_object = PlaceholderAssignment(assignment_id=None)

class Endpoint(StrEnum):
    MEMBERSHIP =               "{base_url}/courses/{course_id}/memberships"
    RUBRIC =                   "{base_url}/courses/{course_id}/assignments/{assignment_id}/rubric/edit"
    REVIEW_GRADES =            "{base_url}/courses/{course_id}/assignments/{assignment_id}/review_grades"
    SUBMISSIONS =              "{base_url}/courses/{course_id}/assignments/{assignment_id}/submissions"
    SUBMISSION =               "{base_url}/courses/{course_id}/assignments/{assignment_id}/submissions/{submission_id}"
    QUESTION_SUBMISSIONS =     "{base_url}/courses/{course_id}/questions/{question_id}/submissions"
    QUESTION_SUBMISSION =      "{base_url}/courses/{course_id}/questions/{question_id}/submissions/{question_submission_id}/grade"
    EXPORT =                   "{base_url}/courses/{course_id}/assignments/{assignment_id}/export"
    GRADED_SUBMISSIONS =       "{base_url}/courses/{course_id}/generated_files/{file_id}/"
    ZIP_FILE =                 "{base_url}/courses/{course_id}/assignments/{assignment_id}/export.zip"

def query_endpoint(endpoint: Endpoint, conn: Conn, **path_params: str) -> requests.Response:
    url = endpoint.value.format(base_url=conn.account.gradescope_base_url, **path_params)
    resp: requests.Response = conn.account.session.get(url)
    resp.raise_for_status()
    return resp

############################### Format info for Streamlit ######################################
def format_course_names(courses_dict: dict[str, dict[str, Any]]) -> dict[str, str]:
    course_roles = ['instructor', 'student']
    max_course_id_length = max(set.union(*[{len(k) for k in courses_dict[role].keys()} for role in course_roles]))
    max_course_name_length = max(set.union(*[{len(v.name) for v in courses_dict[role].values()} for role in course_roles]))
    max_course_full_name_length = max(set.union(*[{len(v.full_name) for v in courses_dict[role].values()} for role in course_roles]))
    return reduce(lambda d1, d2: d1 | d2, [{
        f"{'['+course_id+']':<{max_course_id_length+3}}{course.name:<{max_course_name_length+1}}– ".replace(' ','\u00a0') +\
        f"{course.full_name:<{max_course_full_name_length+1}}[{role}]".replace(' ','\u00a0'): course_id
        for (course_id, course) in courses_dict[role].items()
    } for role in course_roles])

def format_assignment_names(assignments_list: list[Assignment]) -> dict[str, str]:
    if not [a for a in assignments_list if a.assignment_id]:
        return {}
    max_assignment_id_length = max(len(a.assignment_id) for a in assignments_list if a.assignment_id)
    return {f"{('['+(a.assignment_id or '<nan>')+']'):<{max_assignment_id_length+4}}{a.name}".replace(' ','\u00a0'):
             (a.assignment_id or '<nan>') for a in assignments_list}

def get_user_mapping(users: list[Student]) -> dict[str, Student]:
    return {u.identifier: u for u in users}

@st.cache_data(ttl=3600)
def filter_submission_zip(zip_bytes: bytes, submission_id_to_student_name_mapping: dict[str, str], assignment_name: str, zip_file_name: str, submission_ids: set[str] | None=None) -> bytes:
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

def ignore_some_args(conn: Any, course_id: str, assignment_id: str, progress_callback: Any) -> int:
    return hash((course_id, assignment_id))

def format_name(s: Student | Member) -> tuple[str, str]:
    if isinstance(s, Student):
        return s.first_name, s.last_name
    else:
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
def get_submission_original_pdf_bytes(_conn: Conn, course_id: str, assignment_id: str, submission_id: str) -> bytes | None:
    resp = query_endpoint(Endpoint.SUBMISSION, _conn, course_id=course_id, assignment_id=assignment_id, submission_id=submission_id)
    resp_json = resp.json()
    if 'pdf_attachment' in resp_json and resp_json['pdf_attachment'] is not None:
        pdf_url = resp_json['pdf_attachment']['url']
        pdf_resp = requests.get(pdf_url)
        return pdf_resp.content
    return None

@st.cache_data(ttl=3600)
def get_original_submissions_zip_bytes(_conn: Conn, course_id: str, assignment_id: str, assignment_name: str, submission_ids_and_student_names: list[tuple[str, str]]) -> tuple[bytes, set[str]]:
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
def get_graded_submission_zip_bytes_helper(_conn: Conn, course_id: str, assignment_id: str, progress_callback: Callable[[float], Any] | None=None) -> bytes:
    review_grades_url = Endpoint.REVIEW_GRADES.format(base_url=_conn.account.gradescope_base_url, course_id=course_id, assignment_id=assignment_id)
    review_grades_resp = query_endpoint(Endpoint.REVIEW_GRADES, _conn, course_id=course_id, assignment_id=assignment_id)
    soup = BeautifulSoup(review_grades_resp.text, "html.parser").find("meta", {"name": "csrf-token"})
    assert soup is not None, "could not find CSRF token in the review grades page; Gradescope must have disconnected"
    csrf = soup["content"]
    headers = {"X-CSRF-Token": csrf, "X-Requested-With": "XMLHttpRequest", "Referer": review_grades_url, "Origin": "https://www.gradescope.com"}
    export_endpoint = Endpoint.EXPORT.format(base_url=_conn.account.gradescope_base_url, course_id=course_id, assignment_id=assignment_id)
    resp1 = _conn.account.session.post(export_endpoint, headers=headers)
    file_id = resp1.json()["generated_file_id"]
    # poll
    while True:
        resp2 = query_endpoint(Endpoint.GRADED_SUBMISSIONS, _conn, course_id=course_id, file_id=file_id)
        file_status_data = resp2.json()
        if progress_callback is not None:
            progress_callback(file_status_data["progress"])
        if file_status_data["status"] == "completed":
            if progress_callback is not None:
                progress_callback(1.0)
            break
        time.sleep(1)
    # download full .zip with all students
    for _ in range(10):
        try:
            resp3 = query_endpoint(Endpoint.ZIP_FILE, _conn, course_id=course_id, assignment_id=assignment_id)
            zipfile.ZipFile(io.BytesIO(resp3.content), "r")
            return resp3.content
        except Exception:
            time.sleep(1)
    return b''

def get_graded_submissions_zip_bytes(_conn: Conn, course_id: str, assignment_id: str, submission_id_to_student_name_mapping: dict[str, str], assignment_name: str, zip_file_name: str, submission_ids: set[str] | None =None, _progress_callback: Callable[[float], Any] | None =None) -> bytes:
    zip_bytes = get_graded_submission_zip_bytes_helper(_conn, course_id, assignment_id, _progress_callback)
    return filter_submission_zip(zip_bytes, submission_id_to_student_name_mapping, assignment_name, zip_file_name, submission_ids)

############################### Extract raw data from Gradescope ################################
@sample_report_available
@st.cache_data(ttl=3600)
def get_raw_submissions_metadata(_conn: Conn, course_id: str, assignment_id: str) -> Any:
    # submissions metadata incl. IDs, time submitted, grading progress
    resp = query_endpoint(Endpoint.SUBMISSIONS, _conn, course_id=course_id, assignment_id=assignment_id)
    return resp.json()

@sample_report_available
@st.cache_data(ttl=3600)
def get_grades_metadata(_conn: Conn, course_id: str, assignment_id: str, instructors: list[Student], users: list[Student]) -> dict[str, dict[str, Any]]:
    # submissions grades metadata incl. total grade, submitted or not, and timestamp
    resp = query_endpoint(Endpoint.REVIEW_GRADES, _conn, course_id=course_id, assignment_id=assignment_id)
    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table", class_="js-reviewGradesTable")
    assert table is not None, "Could not find review grades table in the review grades page"
    thead = table.find("thead")
    assert thead is not None, "Could not find thead in the review grades table"
    headers = {th.get_text(" ", strip=True).lower(): i for i, th in enumerate(thead.find_all("th"))}
    def header_substr_to_cell(cells: list[Tag], header_substr: str) -> Tag | None:
        for h in headers:
            if header_substr.lower() in h.lower():
                return cells[headers[h]]
        return None

    tbody = table.find("tbody")
    assert tbody is not None, "Could not find tbody in the review grades table"
    default_instructor_results = {i.email_address: {"submission_id": None, "student_name": format_name(i), "score": None, "submitted": False, "submitted_at": None} for i in instructors}
    results: dict[str, dict[str, Any]] = {}
    for row in tbody.find_all("tr"):
        cells = row.find_all("td")
        name_cell = header_substr_to_cell(cells, 'name')
        time_cell = header_substr_to_cell(cells, 'time')
        assert name_cell is not None, "Could not find name cell in the review grades table row"
        assert time_cell is not None, "Could not find time cell in the review grades table row"
        link = name_cell.find("a")
        student_name = name_cell.get_text(strip=True)
        email_cell = header_substr_to_cell(cells, 'email')
        assert email_cell is not None, "Could not find email cell in the review grades table row"
        email = email_cell.get_text(strip=True)
        if link is None:
            if any(u.email_address == email for u in users):
                results[email] = {"submission_id": None, "student_name": student_name, "score": None, "submitted": False, "submitted_at": None,}
            continue
        href = str(link["href"])
        submission_id_match = re.search(r"/submissions/(\d+)", href)
        assert submission_id_match is not None, f"Could not find submission ID in the review grades table row link: {href}"
        submission_id = submission_id_match.group(1)
        student_name = link.get_text(strip=True)
        score_cell = header_substr_to_cell(cells, 'score')
        assert score_cell is not None, "Could not find score cell in the review grades table row"
        score = float(score_cell.get_text(strip=True)) if score_cell.get_text(strip=True) else None
        time = time_cell.find("time")
        assert time is not None, "Could not find time element in the review grades table row"
        submitted_at = datetime.datetime.strptime(str(time["datetime"]), "%Y-%m-%d %H:%M:%S %z")
        if any(u.email_address == email for u in users):
            results[email] = {"submission_id": submission_id, "student_name": student_name, "score": score, "submitted": True, "submitted_at": submitted_at}
    return default_instructor_results | results

@sample_report_available
@st.cache_data(ttl=3600)
def get_student_info(_conn: Conn, course_id: str) -> tuple[list[Student], int]:
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
def get_instructor_info(_conn: Conn, course_id: str) -> list[Student]:
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
def get_assignment_questions(_conn: Conn, course_id: str, assignment_id: str) -> tuple[dict[str, Question], list[str]]:
    # question info incl. outline, max grade, available rubric items, scoring type, etc.
    resp = query_endpoint(Endpoint.RUBRIC, _conn, course_id=course_id, assignment_id=assignment_id)
    if resp.status_code != 200:
        raise NotImplementedError("Grade breakdown analysis is currently only implemented for courses for which you are an instructor.")
    soup = BeautifulSoup(resp.text, "html.parser")
    element = soup.find("div", {"data-react-class": "AssignmentRubric"})
    assert element is not None, "Could not find AssignmentRubric element in the rubric page; Gradescope must've disconnected"
    props = json.loads(str(element["data-react-props"]))
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
            rubric_group_description=str([g for g in props['rubric_item_groups'] if g['id']==r['group_id']][0]['description']) if r['group_id'] else None
        )
    return qs, qs_order

@st.cache_data(ttl=3600)
def load_single_question_submission_data(_conn: Conn, course_id: str, question_id: str, question_submission_id: str) -> tuple[str, str, str, dict[str, Any]]:
    # helper function (used for parallelization) for scraping data for a single question submission
    resp = query_endpoint(Endpoint.QUESTION_SUBMISSION, _conn, course_id=course_id, question_id=question_id, question_submission_id=question_submission_id)
    soup = BeautifulSoup(resp.text, "html.parser")
    div = soup.find("div", attrs={"data-react-props": True})
    assert div is not None, f"Could not find div with data-react-props for question submission {question_submission_id} in course {course_id}, question {question_id}"
    data = json.loads(html.unescape(str(div["data-react-props"])))
    assignment_submission_id = str(data["submission"]["assignment_submission_id"])
    return assignment_submission_id, question_id, question_submission_id, data

@sample_report_available
@st.cache_data(ttl=3600, hash_funcs={Question: lambda q: (q.course_id, q.assignment_id, q.question_id)})
def get_grader_by_question_submission(_conn: Conn, course_id: str, questions: dict[str, Question]) -> dict[str, dict[str, str]]:
    # get (most recent) grader for each question submission (one per question per student)
    grader_by_question_submission: dict[str, dict[str, str]] = {}
    for question_id in questions:
        resp = query_endpoint(Endpoint.QUESTION_SUBMISSIONS, _conn, course_id=course_id, question_id=question_id)
        soup = BeautifulSoup(resp.text, "html.parser")
        table = soup.find("table", id="question_submissions")
        if not table:
            continue
        grader_by_question_submission[question_id] = {}
        for row in table.find_all("tr")[1:]:   # skip header
            cells = row.find_all("td")
            link_a = cells[1].find("a")
            assert link_a is not None, f"Could not find link for question submission in course {course_id}, question {question_id}"
            link = str(link_a["href"])
            question_submission_id_match = re.search(r"/submissions/(\d+)/grade", link)
            assert question_submission_id_match is not None, f"Could not find question submission ID in link {link} for course {course_id}, question {question_id}"
            question_submission_id = str(question_submission_id_match.group(1))
            grader = cells[2].get_text(strip=True)
            grader_by_question_submission[question_id][question_submission_id] = grader
    return grader_by_question_submission

@sample_report_available
@st.cache_data(ttl=3600, hash_funcs={Question: lambda q: (q.course_id, q.assignment_id, q.question_id)})
def get_question_to_question_submissions(_conn: Conn, course_id: str, questions: dict[str, Question]) -> dict[str, list[str]]:
    question_to_submissions = defaultdict(list)
    for question_id in questions:
        resp = query_endpoint(Endpoint.QUESTION_SUBMISSIONS, _conn, course_id=course_id, question_id=question_id)
        soup = BeautifulSoup(resp.text, "html.parser")
        table = soup.find("table", id="question_submissions")
        if table is None:
            continue
        for a in table.select("td.table--primaryLink a"):
            submission_id = str(a["href"]).split("/submissions/")[1].split("/")[0]
            question_to_submissions[question_id].append(submission_id)
    return question_to_submissions

@sample_report_available
@st.cache_data(ttl=3600, hash_funcs={Question: lambda q: (q.course_id, q.assignment_id, q.question_id)})
def get_raw_data_by_question_submission(_conn: Conn, course_id: str, students: list[Student], questions: dict[str, Question], question_to_submissions: dict[str, list[str]], student_to_assignment_submissions: dict[str, str | None]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, str]]]:
    # given question metadata and mapping of IDs, extracts all grade and comment info
    assignment_submission_to_question_submissions: dict[str, dict[str, str]] = {s: {} for s in set(student_to_assignment_submissions.values()) if s is not None}
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

    comments: dict[str, dict[str, list[RawCommentData]]] = {}
    total_scores: dict[str, dict[str, float | None]] = {}
    student_to_question_to_question_submission: dict[str, dict[str, str]] = {}
    for student in students:
        student_id = student.identifier
        comments[student_id], total_scores[student_id], student_to_question_to_question_submission[student_id] = {}, {}, {}
        for question_id in questions:
            comments[student_id][question_id] = []
            total_scores[student_id][question_id] = None
            assignment_submission_id2 = student_to_assignment_submissions[student_id]
            if assignment_submission_id2 is not None:
                if not questions[question_id].children:
                    question_submission_id = assignment_submission_to_question_submissions[assignment_submission_id2][question_id]
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
def get_student_to_assignment_submissions(students: list[Student], submission_metadata: dict[str, dict[str, Any]], grades_metadata: dict[str, dict[str, Any]]) -> dict[str, str | None]:
    student_to_assignment_submissions: dict[str, str | None] = {student.identifier: None for student in students}
    for submission_id, submission_info in submission_metadata['detailed_submissions'].items():
        for user in submission_info['active_user_ids']:
            if str(user) in student_to_assignment_submissions:
                student_to_assignment_submissions[str(user)] = str(submission_id)
            elif any(data['submission_id']==str(submission_id) for (_id, data) in grades_metadata.items()):
                matching_grade_info = [_id for (_id, data) in grades_metadata.items() if data['submission_id']==str(submission_id)][0]
                student_to_assignment_submissions[matching_grade_info] = str(submission_id)
    return student_to_assignment_submissions

def build_tree(rows: list[RawCommentData]) -> list[CommentNode]:
    # helper function to build reduced tree of comments with full info from unstructured parsed partial info data
    def make_id(id_: str | None, description: str | None) -> str:
        return id_ if id_ is not None else f"{hash(description)}"
    nodes = {}
    def get_node(id_: str | None, description: str | None, points: float | None, linked: bool) -> CommentNode:
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

def format_tree(roots: list[CommentNode]) -> str:
    # format tree of comments & points for a question into a blurb of ordered comments by line
    def subtree_has_points(node: CommentNode) -> bool:
        return (node.points is not None and node.points != 0) or any(subtree_has_points(child) for child in node.children.values())
    def format_node(node: CommentNode, indent: int=0, extra_indent: int=0) -> list[str]:
        prefix = ""
        if node.linked:
            prefix += "[^] "
        if node.points is not None and node.points > 0:
            prefix += f"+{node.points} "
        elif node.points is not None and node.points < 0:
            prefix += f"{node.points} "
        else:
            prefix += f"{BULLETS[indent % len(BULLETS)]} "
        line = "    " * indent + " " * extra_indent + prefix + str(node.description)
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

def get_grade_breakdowns(students: list[Student], questions: dict[str, Question], comments: dict[str, dict[str, list[RawCommentData]]], total_scores: dict[str, dict[str, float]], student_to_question_to_question_submission: dict[str, dict[str, str]], grader_by_question_submission: dict[str, dict[str, str]], questions_order: list[str]) -> dict[str, list[GradeInfo]]:
    # gets list of GradeInfo objects for all questions for each student
    results: dict[str, list[GradeInfo]] = {}
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
def get_grade_summary(assignment_title: str, assignment_due_date: str, assignment_max_grade: float, selected_students: list[Student], questions: dict[str, Question], student_mapping: dict[str, Student], grade_breakdowns: dict[str, list[GradeInfo]], grades_metadata: dict[str, dict[str, Any]], student_to_assignment_submissions: dict[str, str | None]) -> pd.DataFrame:
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
                row: dict[str, Any]= {f"{assignment_title}\nDue {assignment_due_date}\n\nName": student.first_name + " " + student.last_name, "Email": student.email_address}
                row['Submitted'] = grades_metadata[student.email_address]['submitted_at'].isoformat(sep=' ')
                row['Submission ID'] = student_to_assignment_submissions[student_id]
                for parent_question_id, parent_question_title in list(dict.fromkeys([(grade_info.parent_item_id, grade_info.parent_item_title) for grade_info in grade_breakdown])):
                    score = 0.0
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

def build_feedback_files(assignment_title: str, assignment_max_grade: float, selected_students: list[Student], questions: dict[str, Question], student_mapping: dict[str, Student], grade_breakdowns: dict[str, list[GradeInfo]], grades_metadata: dict[str, dict[str, Any]]) -> dict[str, str]:
    feedback_file_strs = {}
    for student_id, grade_breakdown in grade_breakdowns.items():
        student = student_mapping[student_id]
        if student_id in set(s.identifier for s in selected_students):
            if grades_metadata[student.email_address]['submitted']:
                feedback = f"{assignment_title} Grade Feedback\nStudent Name: {student.first_name} {student.last_name}\nStudent Email: {student.email_address}\n\n"""
                for parent_question_id, parent_question_title in list(dict.fromkeys([(grade_info.parent_item_id, grade_info.parent_item_title) for grade_info in grade_breakdown])):
                    score = 0.0
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

def get_assignment_outline_and_stats(questions: dict[str, Question], questions_order: list[str], grade_breakdowns: dict[str, list[GradeInfo]], users_with_grades: list[Student]) -> pd.DataFrame:
    def outline(question: Question) -> str:
        path = [question.title]
        p = question.parent
        while p is not None:
            path = [p.title] + path
            p = p.parent
        return '\n'.join(["--"*i+p for (i,p) in enumerate(path)])
    def rubric_string(question: Question) -> str:
        if not question.rubric_items:
            return ""
        items = list(question.rubric_items.values())
        groups: dict[str, dict[str, Any]] = {}
        ungrouped: list[RubricItem] = []
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
        def has_points(group: RubricItem | dict[str, Any]) -> bool:
            return group.points != 0 if isinstance(group, RubricItem) else any(item.points != 0 for item in group["items"])
        def format_item(item: RubricItem, indent: int=0) -> str:
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
        for entry in all_items:
            if isinstance(entry, RubricItem):
                lines.append(format_item(entry))
            else:
                lines.append(f"{BULLETS[0]} {entry['description']}")
                entry["items"].sort(key=lambda i: i.points == 0)
                for item2 in entry["items"]:
                    lines.append(format_item(item2, indent=1))
        return "\n".join(lines)
    def stats_string(scores: list[float | None]) -> str:
        scores_non_null: list[float] = [x for x in scores if x is not None]
        if not scores_non_null:
            return ""
        return f"Count: {len(scores_non_null)}\nMean: {mean(scores_non_null):.2f}\nMedian: {median(scores_non_null):.2f}"
    def grader_stats_string(grader_scores: dict[str, list[float | None]]) -> str:
        pieces = []
        for grader in sorted(grader_scores):
            scores = [x for x in grader_scores[grader] if x is not None]
            if not scores:
                continue
            pieces.append(f"{grader}\n  Count: {len(scores)}\n  Mean: {mean(scores):.2f}\n  Median: {median(scores):.2f}")
        return "\n\n".join(pieces)
    rows = []
    for question in questions_order:
        scores: list[float | None] = []
        grader_scores: dict[str, list[float | None]] = defaultdict(list)
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
            ) if grader else "None"
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

def get_submission_summary(selected_students: list[Student], grades_metadata: dict[str, dict[str, float | None]], downloaded_pdf_students: set[str]) -> pd.DataFrame:
    grades = []
    pdfs_available = 0
    for student in selected_students:
        meta = grades_metadata[student.email_address]
        if meta["score"] is not None:
            grades.append(meta["score"])
        if (f"{student.first_name.replace(' ', '_')}_{student.last_name.replace(' ', '_')}") in downloaded_pdf_students:
            pdfs_available += 1
    n = len(selected_students)
    def fmt(x: Any) -> Any:
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
    def safe_cell(x: Any) -> Any:
        if pd.isna(x):
            return None
        if isinstance(x, (np.integer, np.floating, np.bool_)):
            return x.item()
        if isinstance(x, (pd.Timestamp, datetime.datetime)):
            return x.isoformat(sep=' ')
        if isinstance(x, (dict, list, tuple, set)):
            return str(x)
        return x
    return df.map(safe_cell)

def format_grade_summary_df(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any], int, dict[str, dict[str, str]]]:
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
    for col2 in grid_options["columnDefs"]:
        if col2["field"] == "_rowHeight":
            col2["hide"] = True
    preview_height = int((grade_summary_styled.iloc[:5].astype(str).map(lambda s: min(5, str(s).count("\n")+1)).max(axis=1)*22).sum())+5*22
    custom_css = {".ag-header-cell-label": {"justify-content": "flex-start",}, ".ag-header-cell-text": {"white-space": "pre-line","text-align": "left",}}
    return grade_summary_styled, grid_options, preview_height, custom_css

def is_arrow_compatible(df: pd.DataFrame) -> tuple[bool, str | None]:
    try:
        pa.Table.from_pandas(df)
        for col in df.columns:
            json.dumps(df[col].tolist(), allow_nan=False)[:100]
        st.session_state.df_pa_compatible_count = st.session_state.get('df_pa_compatible_count',0)+1
        return True, None
    except Exception:
        return False, traceback.format_exc()

