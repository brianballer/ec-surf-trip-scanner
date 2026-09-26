import streamlit as st
import pandas as pd
import requests
import plotly.graph_objects as go
from datetime import datetime, timedelta

# --- CONFIGURATION ---
st.set_page_config(page_title="Surf Scout: Mid-Atlantic", layout="wide")

LOCATIONS = {
    # tide_station: nearest NOAA CO-OPS station that actually serves harmonic
    # tide predictions (several closer inlet/subordinate stations don't).
    "Crystal Pier, NC": {"lat": 34.21, "lon": -77.70, "face": 110, "tide_station": "8658163"},  # Wrightsville Beach, NC
    "Bogue Inlet Pier, NC": {"lat": 34.60, "lon": -77.03, "face": 160, "tide_station": "8656613"},  # Swansboro, NC
    "Croatan Beach, VA": {"lat": 36.83, "lon": -75.90, "face": 95, "tide_station": "8638863"},  # Chesapeake Bay Bridge Tunnel, VA
}

MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
NOAA_TIDE_URL = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
REQUEST_TIMEOUT = 10  # seconds

M_TO_FT = 3.28084
KMH_TO_MPH = 0.621371
TIMEZONE = "America/New_York"
FORECAST_DAYS = 14

# Color Palette matching the requested image
COLOR_MAP = {
    2: "#72cc9b",  # Clean / Green
    1: "#f6eb14",  # Fair / Yellow
    0: "#e85151",  # Choppy / Red
}
WAVE_GREEN_FT = 1.4   # at/above this: green
WAVE_YELLOW_FT = 1.25  # at/above this (but below green): yellow; below this: red
MIN_PERIOD_SEC = 4    # below this, wind chop dominates regardless of height

# Dark theme palette — charts render as dark cards that blend into the app chrome
# instead of white boxes (Streamlit's auto-theming only re-themes named templates
# like "plotly_white"/"plotly_dark"; explicit colors + theme=None keep this exact).
CARD_BG = "#161b22"
GRID_COLOR = "rgba(255,255,255,0.08)"
TEXT_COLOR = "#c9d1d9"
MUTED_TEXT = "#8b949e"


def dark_layout(**overrides):
    base = dict(paper_bgcolor=CARD_BG, plot_bgcolor=CARD_BG, font=dict(color=TEXT_COLOR))
    base.update(overrides)
    return base

MIN_DATE = datetime.now().date()
MAX_DATE = MIN_DATE + timedelta(days=FORECAST_DAYS - 1)

if "sel_spot" not in st.session_state:
    st.session_state.sel_spot = list(LOCATIONS.keys())[0]
if "sel_date" not in st.session_state:
    st.session_state.sel_date = MIN_DATE


def clamp_date(d):
    return min(max(d, MIN_DATE), MAX_DATE)


def prev_day():
    st.session_state.sel_date = clamp_date(st.session_state.sel_date - timedelta(days=1))


def next_day():
    st.session_state.sel_date = clamp_date(st.session_state.sel_date + timedelta(days=1))


@st.cache_data(ttl=3600)
def fetch_combined_data(lat, lon):
    """Fetch and merge marine + weather hourly data. Raises on any failure
    so the caller can surface a per-location error instead of failing silently."""
    params = {"latitude": lat, "longitude": lon, "timezone": TIMEZONE, "forecast_days": FORECAST_DAYS}

    m_resp = requests.get(MARINE_URL, params={**params, "hourly": ["wave_height", "wave_period"]},
                           timeout=REQUEST_TIMEOUT)
    m_resp.raise_for_status()
    w_resp = requests.get(FORECAST_URL, params={**params, "hourly": ["wind_speed_10m", "wind_direction_10m"]},
                           timeout=REQUEST_TIMEOUT)
    w_resp.raise_for_status()

    m_json, w_json = m_resp.json(), w_resp.json()
    if "hourly" not in m_json or "hourly" not in w_json:
        raise ValueError("Unexpected API response (missing 'hourly' data)")

    df = pd.merge(pd.DataFrame(m_json["hourly"]), pd.DataFrame(w_json["hourly"]), on="time", how="inner")
    if df.empty:
        raise ValueError("Marine and weather forecasts had no overlapping timestamps")

    df["time"] = pd.to_datetime(df["time"])
    df["wave_height_ft"] = df["wave_height"] * M_TO_FT
    df["wind_speed_mph"] = df["wind_speed_10m"] * KMH_TO_MPH
    df.rename(columns={"wave_period": "period_sec", "wind_direction_10m": "wind_deg"}, inplace=True)
    return df.dropna()


@st.cache_data(ttl=3600)
def fetch_tide_data(station_id, date):
    """Hourly tide-height predictions (ft, MLLW) for one station and day."""
    date_str = date.strftime("%Y%m%d")
    params = {
        "product": "predictions",
        "application": "SurfScout",
        "begin_date": date_str,
        "end_date": date_str,
        "datum": "MLLW",
        "station": station_id,
        "time_zone": "lst_ldt",
        "units": "english",
        "interval": "h",
        "format": "json",
    }
    resp = requests.get(NOAA_TIDE_URL, params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    payload = resp.json()
    if "predictions" not in payload:
        message = payload.get("error", {}).get("message", "No tide data returned")
        raise ValueError(message)

    df = pd.DataFrame(payload["predictions"])
    df["time"] = pd.to_datetime(df["t"])
    df["tide_ft"] = df["v"].astype(float)
    return df[["time", "tide_ft"]]


def is_offshore(wind_deg, beach_face):
    offshore_dir = (beach_face + 180) % 360
    diff = abs(wind_deg - offshore_dir)
    if diff > 180:
        diff = 360 - diff
    return diff <= 50


def get_wave_score(height_ft, period_sec):
    if period_sec < MIN_PERIOD_SEC:
        return 0
    if height_ft >= WAVE_GREEN_FT:
        return 2
    if height_ft >= WAVE_YELLOW_FT:
        return 1
    return 0


def get_wind_score(wind_deg, wind_speed_mph, beach_face):
    offshore = is_offshore(wind_deg, beach_face)
    if offshore:
        return 2 if wind_speed_mph <= 15 else 0
    if wind_speed_mph < 5:
        return 2
    if wind_speed_mph < 10:
        return 1
    return 0


def load_all_data():
    all_data = {}
    with st.spinner("Fetching latest forecasts..."):
        for name, info in LOCATIONS.items():
            try:
                df = fetch_combined_data(info["lat"], info["lon"])
            except Exception as e:
                st.warning(f"⚠️ Couldn't load forecast for {name}: {e}")
                continue
            df["wave_score"] = df.apply(
                lambda x: get_wave_score(x["wave_height_ft"], x["period_sec"]), axis=1)
            df["wind_score"] = df.apply(
                lambda x: get_wind_score(x["wind_deg"], x["wind_speed_mph"], info["face"]), axis=1)
            df["score"] = df[["wave_score", "wind_score"]].min(axis=1)
            df["hour"] = df["time"].dt.hour
            df["date"] = df["time"].dt.date
            df["spot"] = name
            df["wave_color"] = df["wave_score"].map(COLOR_MAP)
            df["wind_color"] = df["wind_score"].map(COLOR_MAP)
            df["color"] = df["score"].map(COLOR_MAP)
            all_data[name] = df
    return all_data


def main():
    st.title("🏄‍♂️ The Mid-Atlantic Surf Scout 🏄‍♀️")

    all_data = load_all_data()
    if not all_data:
        st.error("Data unavailable for all locations right now. Please try again shortly.")
        return

    # If a previously selected spot failed to load this run, fall back to one that did.
    if st.session_state.sel_spot not in all_data:
        st.session_state.sel_spot = next(iter(all_data))

    # Single location selector drives both the overview chart and the drill-down below.
    st.pills("Select Location", options=list(all_data.keys()), key="sel_spot")

    # --- SWELL QUALITY OVERVIEW ---
    st.subheader(f"Swell Quality Forecast")

    ov_df = all_data[st.session_state.sel_spot]
    ov_daylight = ov_df[(ov_df["hour"] >= 6) & (ov_df["hour"] <= 19)].copy()

    fig_swell = go.Figure()
    fig_swell.add_trace(go.Bar(
        x=ov_daylight["time"],
        y=ov_daylight["wave_height_ft"],
        marker_color=ov_daylight["color"],
        marker_line_width=0,
        showlegend=False,
        hovertemplate="<b>%{x|%a %b %d %I:%M %p}</b><br>Height: %{y:.2f} ft<extra></extra>",
    ))

    max_height = ov_daylight["wave_height_ft"].max()
    annotation_y = max_height * 1.1
    for d in ov_daylight["date"].unique():
        day_start = datetime.combine(d, datetime.min.time())
        mid_day = day_start + timedelta(hours=12)
        fig_swell.add_vline(x=day_start, line_width=1, line_dash="dash",
                             line_color="rgba(255,255,255,0.15)")
        fig_swell.add_annotation(
            x=mid_day, y=annotation_y,
            text=d.strftime("%a %d").upper(),
            showarrow=False, font=dict(size=12, color=MUTED_TEXT, family="Arial Black"),
        )

    fig_swell.add_hline(y=WAVE_GREEN_FT, line_width=2, line_dash="dash", line_color="#3ddc84")
    fig_swell.add_hline(y=WAVE_YELLOW_FT, line_width=2, line_dash="dash", line_color="#ffe066")

    fig_swell.update_layout(**dark_layout(
        height=350,
        bargap=0,
        margin=dict(l=40, r=10, t=50, b=10),
        yaxis=dict(title="Wave Height (ft)", gridcolor=GRID_COLOR),
        xaxis=dict(showticklabels=False, fixedrange=True),
    ))
    st.plotly_chart(fig_swell, use_container_width=True, theme=None)

    st.divider()

    # --- NAVIGATION & DRILL-DOWN ---
    st.subheader("Break Forecast Drill Down")

    nav_col1, nav_col2, nav_col3, nav_col4, _nav_spacer = st.columns(
        [0.5, 1.4, 0.9, 0.5, 3], gap="small", vertical_alignment="center")
    with nav_col1:
        st.button("◀", on_click=prev_day, disabled=st.session_state.sel_date <= MIN_DATE)
    with nav_col2:
        st.session_state.sel_date = st.date_input(
            "Target Date", value=st.session_state.sel_date,
            min_value=MIN_DATE, max_value=MAX_DATE,
            label_visibility="collapsed",
        )
    with nav_col3:
        st.markdown(f"**{st.session_state.sel_date:%A}**")
    with nav_col4:
        st.button("▶", on_click=next_day, disabled=st.session_state.sel_date >= MAX_DATE)

    detail_df = all_data[st.session_state.sel_spot]
    daylight_df = detail_df[(detail_df["date"] == st.session_state.sel_date) &
                             (detail_df["hour"] >= 6) & (detail_df["hour"] <= 19)].copy()

    if daylight_df.empty:
        st.warning(f"No daylight data found for {st.session_state.sel_date}. "
                   f"The forecast only covers through {MAX_DATE}.")
        return

    tide_station = LOCATIONS[st.session_state.sel_spot]["tide_station"]
    try:
        tide_df = fetch_tide_data(tide_station, st.session_state.sel_date)
        tide_df = tide_df[(tide_df["time"].dt.hour >= 6) & (tide_df["time"].dt.hour <= 19)]
    except Exception as e:
        tide_df = None
        st.caption(f"⚠️ Tide data unavailable: {e}")

    # --- Top: wave height (colored by condition) + wind speed, with direction on the wind markers ---
    fig_top = go.Figure()

    fig_top.add_trace(go.Bar(
        x=daylight_df["time"], y=daylight_df["wave_height_ft"],
        marker_color=daylight_df["wave_color"], marker_line_width=0,
        showlegend=False,
        hovertemplate="%{y:.2f} ft<extra></extra>",
    ))

    fig_top.add_trace(go.Scatter(
        x=daylight_df["time"], y=daylight_df["wind_speed_mph"],
        name="Wind (mph)", mode="lines+markers",
        line=dict(color="#e6edf3", width=2, dash="dot"),
        marker=dict(symbol="arrow", size=13, color=daylight_df["wind_color"], line=dict(width=1, color="#0d1117"),
                    angle=(daylight_df["wind_deg"] + 180) % 360),
        yaxis="y2",
        hovertemplate="%{y:.1f} mph<extra></extra>",
    ))

    fig_top.update_layout(**dark_layout(
        title=f"Hourly Breakdown: {st.session_state.sel_spot} ({st.session_state.sel_date})",
        yaxis=dict(title="Wave Height (ft)", gridcolor=GRID_COLOR,
                   range=[0, max(daylight_df["wave_height_ft"].max() * 1.3, 4)]),
        yaxis2=dict(title="Wind (mph)", overlaying="y", side="right", range=[0, 30], gridcolor=GRID_COLOR),
        hovermode="x unified",
        height=400,
        showlegend=True,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        xaxis=dict(tickformat="%I %p"),
        margin=dict(l=60, r=60, t=60, b=10),
    ))
    st.plotly_chart(fig_top, use_container_width=True, theme=None)
    st.caption("Arrows show wind direction (pointing where the wind blows toward), colored by wind "
               "quality for this break — green favorable, yellow marginal, red unfavorable (direction + speed).")

    # --- Bottom: tide, stacked directly below so the same time window lines up ---
    if tide_df is not None and not tide_df.empty:
        tide_pad = 0.3
        fig_tide = go.Figure()
        fig_tide.add_trace(go.Scatter(
            x=tide_df["time"], y=tide_df["tide_ft"],
            line=dict(color="#d2a679", width=2, dash="dash"),
            fill="tozeroy", fillcolor="rgba(210,166,121,0.2)",
            hovertemplate="%{y:.2f} ft<extra></extra>",
        ))
        fig_tide.update_layout(**dark_layout(
            height=180,
            showlegend=False,
            margin=dict(l=60, r=60, t=10, b=40),
            yaxis=dict(title="Tide (ft, MLLW)", gridcolor=GRID_COLOR,
                       range=[tide_df["tide_ft"].min() - tide_pad, tide_df["tide_ft"].max() + tide_pad]),
            xaxis=dict(tickformat="%I %p"),
        ))
        st.plotly_chart(fig_tide, use_container_width=True, theme=None)


if __name__ == "__main__":
    main()
