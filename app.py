import streamlit as st

st.set_page_config(page_icon="🎓")
pg = st.navigation([
    st.Page("home.py", title="Gradescope API Tool", default=True),
    st.Page(
        "pages/Privacy_Policy.py",
        title="Privacy Policy",
        url_path="privacy_policy",
    ),
])

pg.run()