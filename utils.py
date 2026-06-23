from __future__ import annotations
from functools import reduce
import pandas as pd
from bs4 import BeautifulSoup
import json
from dataclasses import dataclass
from typing import Literal

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
    max_assignment_id_length = max(len(a.assignment_id) for a in assignments_list if a.assignment_id)
    return {f"{('['+(a.assignment_id or '<nan>')+']'):<{max_assignment_id_length+4}}{a.name}".replace(' ','\u00a0'):
             (a.assignment_id or '<nan>') for a in assignments_list}

def get_full_assignment_info(conn, course_id, assignment_id): 
    assn_endpoint = f"{conn.account.gradescope_base_url}/courses/{course_id}/assignments/{assignment_id}"
    url = f"{assn_endpoint}/submissions/"
    resp = conn.account.session.get(url)
    return resp.json()

def get_student_info(conn, course_id): 
    members_list = conn.account.get_course_users(course_id)
    def format_name(s): 
        if s.first_name and s.last_name:
            return f'{s.first_name}', f'{s.last_name}'
        name_parts = s.full_name.split(' ')
        if len(name_parts) <= 1:
            return '', s.full_name
        elif len(name_parts) == 2: 
            return f'{name_parts[0]}', f'{name_parts[1]}'
        else: 
            return f'{" ".join(name_parts[0:-1])}', f'{name_parts[-1]}'
    max_student_name_len = max([len(format_name(s)[0])+len(format_name(s)[1])+1 for s in members_list if s.role=='Student'])
    return (
        sorted([(s.email, format_name(s)[0], format_name(s)[1], s.sid, s.user_id) for s in members_list if s.role=='Student'], key=lambda x: (x[2],x[1])), 
        max_student_name_len
    )

def get_assignment_questions(conn, course_id, assignment_id): 
    endpoint = f"{conn.account.gradescope_base_url}/courses/{course_id}/assignments/{assignment_id}/rubric/edit"
    response = conn.account.session.get(endpoint)
    if response.status_code != 200:
        raise RuntimeError(
            f"Failed to access account page on Gradescope. Status code: {response.status_code}"
        )
    soup = BeautifulSoup(response.text, "html.parser")
    element = soup.find("div", {"data-react-class": "AssignmentRubric"})
    props = json.loads(element["data-react-props"])
    qs = dict()
    for q in props['questions']: 
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
    return qs

# def get_student_grade_breakdown(conn, course_id, assignment_id, studentemailoruseridorstudentididk): 


def get_grade_summary(conn, course_id, assignment_id): 
    # TODO
    df = pd.DataFrame([[1,'aaa',2,3,4,1,2,3,4,1,2],[5,'bbb',6,7,8,5,6,7,8,5,6]], columns=[
        'Student Name', 'Email', 'Q1.1 grade', 'Q1.1 comments', 'Q1.2 grade', 'Q1.2 comments', 'Q1 TOTAL', 'Q2 grade', 'Q2 comments', 'Q2 TOTAL', 'Assignment TOTAL'
    ])
    return df