"""Streamlit dashboard for ReviewBot AI.

Reads everything through the FastAPI service (``/api/metrics``, ``/api/reviews``)
so the dashboard can run anywhere the API is reachable.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import pandas as pd
import plotly.express as px
import requests
import streamlit as st

DEFAULT_API_URL = os.getenv("REVIEWBOT_API_URL", "http://localhost:8000")
REQUEST_TIMEOUT = 10

CATEGORY_LABELS = {
    "total_bugs": "Bugs",
    "total_security": "Security",
    "total_smells": "Code smells",
    "total_performance": "Performance",
    "total_best_practices": "Best practices",
}

SEVERITY_COLORS = {"high": "#e45756", "medium": "#f2c744", "low": "#54a24b"}


@st.cache_data(ttl=60, show_spinner=False)
def fetch(api_url: str, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    """GET a JSON document from the API. Returns ``None`` on any failure."""
    try:
        response = requests.get(
            f"{api_url.rstrip('/')}{path}", params=params, timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        st.session_state["last_error"] = str(exc)
        return None


def render_header() -> None:
    st.title("🤖 ReviewBot AI")
    st.caption("AI-powered code review for GitHub pull requests")


def render_sidebar() -> Dict[str, Any]:
    """Sidebar controls; returns the selected API URL and repo filter."""
    with st.sidebar:
        st.header("Settings")
        api_url = st.text_input("API URL", value=DEFAULT_API_URL)

        if st.button("🔄 Refresh data", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

        health = fetch(api_url, "/health")
        if health:
            healthy = health.get("status") == "healthy"
            st.success("API healthy") if healthy else st.warning("API degraded")
            st.caption(f"Model: `{health.get('model', 'unknown')}`")
            config = health.get("config", {})
            missing = [name for name, present in config.items() if not present]
            if missing:
                st.warning("Not configured: " + ", ".join(missing))
        else:
            st.error("API unreachable")
            st.caption(st.session_state.get("last_error", ""))

        repos = (fetch(api_url, "/api/repos") or {}).get("repos", [])
        options = ["All repositories"] + [r["repo_name"] for r in repos]
        selected = st.selectbox("Repository", options)

    return {
        "api_url": api_url,
        "repo": None if selected == "All repositories" else selected,
        "online": bool(health),
    }


def render_metrics(metrics: Dict[str, Any]) -> None:
    """Top-line KPI row."""
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Reviews", f"{metrics.get('total_reviews', 0):,}")
    col2.metric("Issues found", f"{metrics.get('total_issues', 0):,}")
    col3.metric("Avg confidence", f"{(metrics.get('average_confidence') or 0) * 100:.0f}%")
    col4.metric("Files reviewed", f"{metrics.get('total_files_reviewed', 0):,}")

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("🐛 Bugs", metrics.get("total_bugs", 0))
    col2.metric("🔒 Security", metrics.get("total_security", 0))
    col3.metric("👃 Smells", metrics.get("total_smells", 0))
    col4.metric("⚡ Performance", metrics.get("total_performance", 0))


def render_charts(metrics: Dict[str, Any], reviews: List[Dict[str, Any]]) -> None:
    """Category, severity and volume charts."""
    left, right = st.columns(2)

    with left:
        st.subheader("Issues by category")
        data = pd.DataFrame(
            {
                "Category": list(CATEGORY_LABELS.values()),
                "Count": [metrics.get(key, 0) for key in CATEGORY_LABELS],
            }
        )
        if data["Count"].sum() == 0:
            st.info("No issues recorded yet.")
        else:
            fig = px.bar(data, x="Category", y="Count", color="Category", text="Count")
            fig.update_layout(showlegend=False, height=340)
            st.plotly_chart(fig, use_container_width=True)

    with right:
        st.subheader("Severity breakdown")
        severity = metrics.get("severity_breakdown") or {}
        data = pd.DataFrame(
            {
                "Severity": [s.title() for s in severity],
                "Count": list(severity.values()),
            }
        )
        if data.empty or data["Count"].sum() == 0:
            st.info("No severities recorded yet.")
        else:
            fig = px.pie(
                data,
                names="Severity",
                values="Count",
                hole=0.45,
                color="Severity",
                color_discrete_map={
                    key.title(): value for key, value in SEVERITY_COLORS.items()
                },
            )
            fig.update_layout(height=340)
            st.plotly_chart(fig, use_container_width=True)

    st.subheader("Review activity")
    if not reviews:
        st.info("No reviews yet.")
        return

    frame = pd.DataFrame(reviews)
    frame["created_at"] = pd.to_datetime(frame["created_at"], errors="coerce")
    daily = (
        frame.dropna(subset=["created_at"])
        .groupby(frame["created_at"].dt.date)
        .agg(reviews=("id", "count"), issues=("total_issues", "sum"))
        .reset_index()
        .rename(columns={"created_at": "date"})
    )
    if daily.empty:
        st.info("No dated reviews yet.")
        return

    fig = px.line(daily, x="date", y=["reviews", "issues"], markers=True)
    fig.update_layout(height=320, yaxis_title="Count", legend_title="")
    st.plotly_chart(fig, use_container_width=True)


def render_reviews_table(reviews: List[Dict[str, Any]]) -> None:
    """Recent reviews as a sortable table."""
    st.subheader("Recent reviews")
    if not reviews:
        st.info("No reviews yet. Open a pull request to get started.")
        return

    frame = pd.DataFrame(
        [
            {
                "ID": r["id"],
                "Repository": r["repo_name"],
                "PR": r["pr_number"],
                "Issues": r["total_issues"],
                "Bugs": r["issue_counts"].get("bugs", 0),
                "Security": r["issue_counts"].get("security", 0),
                "Files": r["files_reviewed"],
                "Confidence": round((r["confidence_score"] or 0) * 100),
                "Created": r["created_at"],
            }
            for r in reviews
        ]
    )
    st.dataframe(
        frame,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Confidence": st.column_config.ProgressColumn(
                "Confidence", format="%.0f%%", min_value=0, max_value=100
            ),
            "Created": st.column_config.DatetimeColumn(
                "Created", format="YYYY-MM-DD HH:mm"
            ),
        },
    )


def render_review_detail(api_url: str, reviews: List[Dict[str, Any]]) -> None:
    """Drill into one review: summary, issues and inline comments."""
    st.subheader("Review detail")
    if not reviews:
        return

    labels = {
        f"#{r['id']} — {r['repo_name']} PR #{r['pr_number']} ({r['total_issues']} issues)": r["id"]
        for r in reviews
    }
    choice = st.selectbox("Select a review", list(labels))
    review = fetch(api_url, f"/api/reviews/{labels[choice]}")
    if not review:
        st.error("Could not load that review.")
        return

    left, right = st.columns([2, 1])
    with left:
        st.markdown(review.get("summary") or "_No summary_")
    with right:
        st.metric("Confidence", f"{(review.get('confidence_score') or 0) * 100:.0f}%")
        st.metric("Files reviewed", review.get("files_reviewed", 0))
        if review.get("commit_sha"):
            st.caption(f"Commit `{review['commit_sha'][:10]}`")

    issues = review.get("issues") or {}
    for issue_type, items in issues.items():
        if not items:
            continue
        label = issue_type.replace("_", " ").title()
        with st.expander(f"{label} ({len(items)})", expanded=issue_type == "security"):
            for issue in items:
                where = issue.get("file", "unknown")
                if issue.get("line"):
                    where = f"{where}:{issue['line']}"
                st.markdown(
                    f"**{where}** — `{issue.get('severity', 'medium')}`\n\n"
                    f"{issue.get('description', '')}"
                )
                if issue.get("suggestion"):
                    st.caption(f"💡 {issue['suggestion']}")
                st.divider()

    comments = review.get("comments") or []
    posted = sum(1 for c in comments if c.get("posted"))
    st.caption(f"{len(comments)} inline comment(s) rendered, {posted} posted to GitHub")


def main() -> None:
    """Streamlit entrypoint."""
    st.set_page_config(
        page_title="ReviewBot AI",
        page_icon="🤖",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    render_header()

    controls = render_sidebar()
    api_url = controls["api_url"]

    if not controls["online"]:
        st.error(
            "Cannot reach the ReviewBot API. Start it with "
            "`uvicorn reviewbot.api.main:app --reload`, then refresh."
        )
        st.stop()

    metrics = fetch(api_url, "/api/metrics") or {}
    params = {"limit": 200}
    if controls["repo"]:
        params["repo"] = controls["repo"]
    reviews = (fetch(api_url, "/api/reviews", params) or {}).get("reviews", [])

    render_metrics(metrics)
    st.divider()
    render_charts(metrics, reviews)
    st.divider()
    render_reviews_table(reviews)
    st.divider()
    render_review_detail(api_url, reviews)


if __name__ == "__main__":
    main()
