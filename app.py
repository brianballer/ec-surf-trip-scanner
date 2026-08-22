import streamlit as st
import pandas as pd
import requests
import plotly.graph_objects as go
from datetime import datetime, timedelta

# --- CONFIGURATION ---
st.set_page_config(page_title="Surf Scout: Mid-Atlantic", layout="wide")

LOCATIONS = {
    "Crystal Pier, NC": {"lat": 34.21, "lon": -77.70, "face": 110},
    "Bogue Inlet Pier, NC": {"lat": 34.60, "lon": -77.03, "face": 160},
    "Croatan Beach, VA": {"lat": 36.83, "lon": -75.90, "face": 95},
}

M_TO_FT = 3.28084
KMH_TO_MPH = 0.621371
TIMEZONE = "America/New_York"

# --- SESSION STATE INITIALIZATION ---
# We initialize these BEFORE the widgets to ensure logic flows correctly
if "sel_spot" not in st.session_state:
    st.session_state.sel_spot = list(LOCATIONS.keys())[0]
if "sel_date" not in st.session_state:
    st.session_state.sel_date = datetime.now().date()

# Functions to handle button clicks safely
def prev_day():
    st.session_state.sel_date -= timedelta(days=1)

def next_day():
    st.session_state.sel_date += timedelta(days=1)

# --- DATA FETCHING ---
@st.cache_data(ttl=3600)
def fetch_combined_data(lat, lon):
    params = {"latitude": lat, "longitude": lon, "timezone": TIMEZONE, "forecast_days": 14}
    try:
        m_resp = requests.get("https://marine-api.open-meteo.com/v1/marine", 
                             params={**params, "hourly": ["wave_height", "wave_period"]})
        w_resp = requests.get("https://api.open-meteo.com/v1/forecast", 
                             params={**params, "hourly": ["wind_speed_10m", "wind_direction_10m"]})
        
        df = pd.merge(pd.DataFrame(m_resp.json()["hourly"]), 
                      pd.DataFrame(w_resp.json()["hourly"]), on="time")
        df['time'] = pd.to_datetime(df['time'])
        df['wave_height_ft'] = df['wave_height'] * M_TO_FT
        df['wind_speed_mph'] = df['wind_speed_10m'] * KMH_TO_MPH
        df.rename(columns={'wave_period': 'period_sec', 'wind_direction_10m': 'wind_deg'}, inplace=True)
        return df.dropna()
    except:
        return None

def get_score(row, beach_face):
    # Logic: > 0.75ft height and >= 4s period
    if not (row['wave_height_ft'] > 0.75 and row['period_sec'] >= 4):
        return 0 
    
    offshore_dir = (beach_face + 180) % 360
    diff = abs(row['wind_deg'] - offshore_dir)
    if diff > 180: diff = 360 - diff
    
    is_offshore = diff <= 50
    if is_offshore and row['wind_speed_mph'] <= 15:
        return 2 
    elif not is_offshore:
        if row['wind_speed_mph'] < 5: return 2
        if row['wind_speed_mph'] < 10: return 1
    return 0

# --- UI LOGIC ---
def main():
    st.title("🏄‍♂️ Rina's Surf Scout")
    
    all_data = {}
    heatmap_list = []

    for name, info in LOCATIONS.items():
        df = fetch_combined_data(info['lat'], info['lon'])
        if df is not None:
            df['score'] = df.apply(lambda x: get_score(x, info['face']), axis=1)
            df['hour'] = df['time'].dt.hour
            df['date'] = df['time'].dt.date
            df['spot'] = name
            all_data[name] = df
            heatmap_list.append(df[(df['hour'] >= 6) & (df['hour'] <= 19)])

    if not all_data:
        st.error("API Data currently unavailable.")
        return

    # --- 1. THE HEATMAP (VISUAL ONLY) ---
    st.subheader("14-Day Condition Matrix")
    h_df = pd.concat(heatmap_list)
    fig_matrix = go.Figure(data=go.Heatmap(
        z=h_df['score'], x=h_df['time'], y=h_df['spot'],
        colorscale=[[0, '#ff4b4b'], [0.5, '#ffeb3b'], [1, '#00c853']],
        showscale=False, xgap=1, ygap=4,
        hovertemplate="<b>%{y}</b><br>%{x|%a %b %d, %I %p}<extra></extra>"
    ))
    fig_matrix.update_layout(height=280, margin=dict(l=10, r=10, t=30, b=10), 
                              xaxis=dict(side="top"), yaxis=dict(autorange="reversed"))
    st.plotly_chart(fig_matrix, use_container_width=True)

    st.divider()

    # --- 2. NAVIGATION & DRILL-DOWN CONTROLS ---
    # Location Pills
    st.pills("Select Location", options=list(LOCATIONS.keys()), key="sel_spot")

    # Date Navigation Row
    nav_col1, nav_col2, nav_col3 = st.columns([1, 2, 1])
    with nav_col1:
        st.button("⬅️ Previous Day", on_click=prev_day, use_container_width=True)
    with nav_col2:
        # Use value= to sync with session state, but we don't need key="sel_date" here 
        # to avoid the streamlit state modification error.
        current_date = st.date_input("Target Date", value=st.session_state.sel_date)
        st.session_state.sel_date = current_date
    with nav_col3:
        st.button("Next Day ➡️", on_click=next_day, use_container_width=True)

    # --- 3. DUAL-AXIS HOURLY CHART (6 AM - 7 PM ONLY) ---
    spot_df = all_data[st.session_state.sel_spot]
    daylight_df = spot_df[(spot_df['date'] == st.session_state.sel_date) & 
                          (spot_df['hour'] >= 6) & (spot_df['hour'] <= 19)].copy()

    if daylight_df.empty:
        st.warning(f"No daylight data found for {st.session_state.sel_date}. The forecast might not reach this far out yet.")
    else:
        fig_detail = go.Figure()

        # Left Axis: Wave Height (Area)
        fig_detail.add_trace(go.Scatter(
            x=daylight_df['time'], y=daylight_df['wave_height_ft'],
            fill='tozeroy', name="Surf (ft)", 
            line=dict(color='#0077b6', width=4),
            hovertemplate="%{y:.2f} ft"
        ))

        # Right Axis: Wind Speed (Line)
        fig_detail.add_trace(go.Scatter(
            x=daylight_df['time'], y=daylight_df['wind_speed_mph'],
            name="Wind (mph)", 
            line=dict(color='#ef476f', width=2, dash='dot'),
            yaxis="y2",
            hovertemplate="%{y:.1f} mph"
        ))

        # Wind Arrows (High Contrast / Dark Mode Ready)
        fig_detail.add_trace(go.Scatter(
            x=daylight_df['time'], 
            y=[daylight_df['wind_speed_mph'].max() * 1.1] * len(daylight_df),
            mode="markers",
            yaxis="y2",
            marker=dict(
                symbol="arrow", size=18, color="white",
                line=dict(width=1.5, color="black"),
                angle=(daylight_df['wind_deg'] + 180) % 360 # Points TO
            ),
            name="Wind Direction", hoverinfo="skip"
        ))

        fig_detail.update_layout(
            title=f"Hourly Breakdown: {st.session_state.sel_spot} ({st.session_state.sel_date})",
            yaxis=dict(title="Wave Height (ft)", range=[0, max(daylight_df['wave_height_ft'].max()*1.3, 4)]),
            yaxis2=dict(title="Wind Speed (mph)", overlaying="y", side="right", range=[0, 30]),
            hovermode="x unified", 
            height=450, 
            template="none", 
            showlegend=False,
            xaxis=dict(tickformat="%I %p")
        )
        st.plotly_chart(fig_detail, use_container_width=True)

        # --- 4. CLEAN TABLE ---
        def score_style(val):
            if val == "GREEN": return 'background-color: rgba(0, 200, 83, 0.25)'
            if val == "AMBER": return 'background-color: rgba(255, 235, 59, 0.25)'
            return 'background-color: rgba(255, 75, 75, 0.25)'

        table_df = daylight_df[['time', 'score', 'wave_height_ft', 'period_sec', 'wind_speed_mph', 'wind_deg']].copy()
        table_df['Condition'] = table_df['score'].map({2: "GREEN", 1: "AMBER", 0: "RED"})
        table_df['time'] = table_df['time'].dt.strftime('%I:%M %p')

        st.dataframe(
            table_df[['time', 'Condition', 'wave_height_ft', 'period_sec', 'wind_speed_mph', 'wind_deg']]
            .style.map(score_style, subset=['Condition']),
            column_config={
                "wave_height_ft": st.column_config.NumberColumn("Waves (ft)", format="%.2f"),
                "wind_speed_mph": st.column_config.NumberColumn("Wind (mph)", format="%.0f"),
                "wind_deg": st.column_config.NumberColumn("Dir (From)"),
            },
            use_container_width=True, hide_index=True
        )

if __name__ == "__main__":
    main()
