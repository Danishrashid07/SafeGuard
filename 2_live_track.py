"""Contact-facing live location page (auto-refreshes)."""
import streamlit as st

from tracking import render_live_track

st.set_page_config(page_title="Live Track", page_icon="📍")
st.title("📍 Live location shared by SafeGuard")


@st.fragment(run_every=3)
def live():
    render_live_track()


live()