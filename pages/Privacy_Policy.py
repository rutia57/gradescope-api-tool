from pathlib import Path 
import streamlit as st

st.set_page_config(page_title="Privacy Policy")
st.set_page_config(layout='wide')

st.title("Privacy Policy")

html = Path("privacy_policy.html").read_text()

st.components.v1.html(html, height=2000, scrolling=True)