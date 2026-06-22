from functools import reduce


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
    