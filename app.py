import datetime as dt
import json
import os
from pathlib import Path

import pandas as pd
import requests
import snowflake.connector
import streamlit as st

# ----------------------------------------------------------------------------
# Config (from the Power BI screenshot). Override with environment variables.
# ----------------------------------------------------------------------------
SERVER = os.getenv("SNOWFLAKE_SERVER", "emeaprod01.eu-west-1.snowflakecomputing.com")
ACCOUNT = SERVER.replace(".snowflakecomputing.com", "")
WAREHOUSE = os.getenv("SNOWFLAKE_WAREHOUSE", "PFE_COMMON_WH_M_01")
ROLE = os.getenv("SNOWFLAKE_ROLE", "VAW_EMEA_DESIGN_DSS_COMMERCIAL")
DATABASE = os.getenv("SNOWFLAKE_DATABASE", "VAW_EMEA_DESIGN")
SF_USER = os.getenv("SNOWFLAKE_USER", "")          # your SSO email
FLOW_URL = os.getenv("FLOW_URL", "")               # Teams Workflows webhook URL (free, no premium)
MAX_CARD_ROWS = 15                                 # keep the Teams card small
MAX_CARD_COLS = 6
QUERY_FILE = Path(__file__).parent / "query.sql"
MAX_FILTER_CHOICES = 50

st.set_page_config(page_title="Snowflake Data Explorer", layout="wide")


# ----------------------------------------------------------------------------
# Snowflake
# ----------------------------------------------------------------------------
@st.cache_resource(show_spinner="Connecting to Snowflake (a browser window may open for SSO)...")
def get_connection():
    kwargs = dict(
        account=ACCOUNT,
        authenticator="externalbrowser",  # SSO, same as Power BI
        warehouse=WAREHOUSE,
        role=ROLE,
        database=DATABASE,
    )
    if SF_USER:  # optional: SSO identifies you if this is left empty
        kwargs["user"] = SF_USER
    return snowflake.connector.connect(**kwargs)


@st.cache_data(show_spinner=False)
def current_user() -> str:
    cur = get_connection().cursor()
    try:
        cur.execute("SELECT CURRENT_USER()")
        return cur.fetchone()[0]
    finally:
        cur.close()


@st.cache_data(ttl=600, show_spinner="Running query...")
def load_data(sql: str) -> pd.DataFrame:
    cur = get_connection().cursor()
    try:
        cur.execute(sql)
        df = cur.fetch_pandas_all()
    finally:
        cur.close()
    # Convert python date columns to datetime so they can be filtered
    for c in df.columns:
        if df[c].dtype == "object":
            first = df[c].dropna().head(1)
            if len(first) and isinstance(first.iloc[0], (dt.date, dt.datetime)):
                df[c] = pd.to_datetime(df[c])
    return df


# ----------------------------------------------------------------------------
# Filters (built automatically from the columns)
# ----------------------------------------------------------------------------
def apply_filters(df: pd.DataFrame) -> pd.DataFrame:
    st.sidebar.header("Filters")

    default_cols = [
        c for c in df.columns
        if df[c].dtype == "object" and df[c].nunique() <= MAX_FILTER_CHOICES
    ][:4]
    chosen = st.sidebar.multiselect("Columns to filter by", list(df.columns), default_cols)

    for c in chosen:
        s = df[c]
        if pd.api.types.is_datetime64_any_dtype(s):
            lo, hi = s.min().date(), s.max().date()
            rng = st.sidebar.date_input(c, (lo, hi), min_value=lo, max_value=hi, key=f"f_{c}")
            if isinstance(rng, tuple) and len(rng) == 2:
                df = df[(df[c].dt.date >= rng[0]) & (df[c].dt.date <= rng[1])]
        elif pd.api.types.is_numeric_dtype(s):
            lo, hi = float(s.min()), float(s.max())
            if lo < hi:
                sel = st.sidebar.slider(c, lo, hi, (lo, hi), key=f"f_{c}")
                df = df[df[c].between(*sel)]
        else:
            options = sorted(s.dropna().astype(str).unique())
            sel = st.sidebar.multiselect(c, options, key=f"f_{c}")
            if sel:
                df = df[df[c].astype(str).isin(sel)]
    search = st.sidebar.text_input("🔍 Search all columns")
    if search:
        mask = df.astype(str).apply(
            lambda col: col.str.contains(search, case=False, na=False)
        ).any(axis=1)
        df = df[mask]

    return df


# ----------------------------------------------------------------------------
# Power Automate
# ----------------------------------------------------------------------------
def build_card(rows: pd.DataFrame, scope: str) -> dict:
    cols = list(rows.columns)[:MAX_CARD_COLS]
    ts = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    body = [
        {"type": "TextBlock", "text": "Snowflake Data Explorer", "weight": "Bolder", "size": "Large"},
        {"type": "TextBlock", "text": f"Triggered by {SF_USER or current_user()} at {ts} UTC", "isSubtle": True, "wrap": True},
        {"type": "TextBlock", "text": f"{len(rows)} row(s) sent ({scope})", "wrap": True},
    ]
    for _, r in rows.head(MAX_CARD_ROWS).iterrows():
        line = " | ".join(f"{c}: {r[c]}" for c in cols)
        body.append({"type": "TextBlock", "text": line, "wrap": True, "separator": True, "spacing": "Small"})
    if len(rows) > MAX_CARD_ROWS:
        body.append({"type": "TextBlock", "text": f"...and {len(rows) - MAX_CARD_ROWS} more rows not shown",
                     "isSubtle": True, "wrap": True})
    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": body,
            },
        }],
    }


def run_flow(rows: pd.DataFrame, scope: str):
    r = requests.post(FLOW_URL, json=build_card(rows, scope), timeout=60)
    r.raise_for_status()
    return r


# ----------------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------------
st.title("Snowflake Data Explorer")

sql = QUERY_FILE.read_text(encoding="utf-8-sig").strip().rstrip(";")
try:
    data = load_data(sql)
except Exception as e:
    st.error(f"Query failed: {e}")
    st.stop()

top = st.columns([1, 1, 6])
top[0].metric("Total rows", f"{len(data):,}")
if top[1].button("↻ Refresh"):
    load_data.clear()
    st.rerun()

filtered = apply_filters(data)
top[1].metric("Filtered", f"{len(filtered):,}")

page_size = st.selectbox("Rows displayed in the table", [500, 1000, 2000, 5000], index=1)
shown = filtered.head(page_size)
st.caption(f"Showing the first {len(shown):,} of {len(filtered):,} filtered rows. Use the filters to narrow down.")

event = st.dataframe(
    shown,
    use_container_width=True,
    hide_index=True,
    on_select="rerun",
    selection_mode="multi-row",
    height=500,
)
selected = shown.iloc[event.selection.rows] if event.selection.rows else shown.iloc[0:0]

if len(filtered) <= 50000:
    st.download_button("⬇ Download filtered CSV", filtered.to_csv(index=False), "data.csv", "text/csv")
else:
    st.caption("CSV download is available when the filtered data has 50,000 rows or fewer.")

st.divider()
st.subheader("Send to Teams (Power Automate)")
scope = st.radio(
    "Send to Teams",
    [f"Selected rows ({len(selected)})", f"All filtered rows ({len(filtered)})"],
    horizontal=True,
)
rows_to_send = selected if scope.startswith("Selected") else filtered

if not FLOW_URL:
    st.info("Set the FLOW_URL environment variable to your Teams Workflows webhook URL.")
elif st.button("▶ Send to Teams", type="primary", disabled=rows_to_send.empty):
    try:
        with st.spinner("Calling flow..."):
            resp = run_flow(rows_to_send, "selected" if scope.startswith("Selected") else "filtered")
        st.success(f"Flow triggered (HTTP {resp.status_code}). Check Teams.")
    except Exception as e:
        st.error(f"Flow call failed: {e}")