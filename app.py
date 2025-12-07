import streamlit as st
import pandas as pd
import plotly.express as px
from io import BytesIO
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import cm

st.set_page_config(
    page_title="Muthambi HPV Household Mapping Dashboard",
    layout="wide"
)

# ===========================================================
# LOAD FUNCTIONS
# ===========================================================

@st.cache_data
def load_household_and_members(uploaded_file):
    """
    Load Kobo Excel export: assume first sheet = households, second = household_members.
    """
    xls = pd.ExcelFile(uploaded_file)
    if len(xls.sheet_names) < 2:
        raise ValueError("Expected at least two sheets (household + household_members).")

    main_df = pd.read_excel(xls, sheet_name=xls.sheet_names[0])
    members_df = pd.read_excel(xls, sheet_name=xls.sheet_names[1])

    main_df.columns = [c.strip() for c in main_df.columns]
    members_df.columns = [c.strip() for c in members_df.columns]

    return main_df, members_df


def merge_members_with_households(main_df, members_df):
    """
    Attach household-level info to each member using submission UUID.
    """
    if "_uuid" in main_df.columns and "_submission__uuid" in members_df.columns:
        return members_df.merge(
            main_df,
            left_on="_submission__uuid",
            right_on="_uuid",
            how="left",
            suffixes=("", "_hh")
        )
    return members_df.copy()


# ===========================================================
# INDICATOR HELPERS
# ===========================================================

def compute_hpv1_coverage_for_group(members, age_min, age_max):
    """
    HPV1 coverage among girls whose age is in [age_min, age_max].
    Returns (n_girls, coverage_percent or None).
    """
    df = members.copy()
    if "age" not in df.columns or "Member sex" not in df.columns:
        return 0, None

    df = df[(df["age"].notna()) & (df["age"] >= 0)]

    girls = df[(df["Member sex"] == "Female") &
               (df["age"].between(age_min, age_max))]
    n_girls = len(girls)

    if n_girls == 0:
        return 0, None

    if "Have you received HPV 1st Dose ?" not in girls.columns:
        return n_girls, None

    cov = 100 * (girls["Have you received HPV 1st Dose ?"] == "Yes").mean()
    return n_girls, cov


def compute_child_antigen_coverages(members):
    """
    Compute:
      - number of under 5s
      - number of under 1s
      - coverage among under 1s for child antigens (Yes/no fields)
      (age based on 'age' column only)
    """
    df = members.copy()
    if "age" not in df.columns:
        return 0, 0, {}

    df = df[(df["age"].notna()) & (df["age"] >= 0)]

    u5 = df[df["age"] < 5]
    u1 = df[df["age"] < 1]

    n_u5 = len(u5)
    n_u1 = len(u1)

    coverages_u1 = {}
    if n_u1 == 0:
        return n_u5, n_u1, coverages_u1

    antigen_cols = [
        "Was Penta 1 Administered",
        "Was Penta 2 Administered",
        "Was Penta 3 Administered",
        "Was Measles Administered",
        "Was Measles 2 Administered",
        "Fully Immunized",
    ]

    for col in antigen_cols:
        if col in u1.columns:
            cov = 100 * (u1[col] == "Yes").mean()
            coverages_u1[col] = cov

    return n_u5, n_u1, coverages_u1


# ===========================================================
# DATACHECK FUNCTION
# ===========================================================

def build_datacheck_issues(df):
    """
    Flag member records with age/DOB issues:
    - Age > 18
    - DOB after interview date
    - Age & DOB differ by >2 years
    """
    df = df.copy()
    df["age_num"] = pd.to_numeric(df.get("age"), errors="coerce")

    issues = []

    # Issue 1: Age > 18
    if "age_num" in df.columns:
        over18 = df[df["age_num"] > 18].copy()
        over18["Issue"] = "Age > 18 years"
        issues.append(over18)

    # DOB & interview date
    dob_col = "Member date of birth" if "Member date of birth" in df.columns else None
    date_col = "Enter a date" if "Enter a date" in df.columns else None

    if dob_col:
        df["dob"] = pd.to_datetime(df[dob_col], errors="coerce")
    else:
        df["dob"] = pd.NaT

    if date_col:
        df["interview"] = pd.to_datetime(df[date_col], errors="coerce")
    else:
        df["interview"] = pd.NaT

    if dob_col and date_col:
        # Issue 2: DOB after interview date
        future = df[df["dob"] > df["interview"]].copy()
        future["Issue"] = "DOB after interview date"
        issues.append(future)

        # Issue 3: Age vs DOB mismatch (>2 years)
        df["calc_age"] = (df["interview"] - df["dob"]).dt.days / 365.25
        mismatch = df[
            (df["calc_age"].notna()) &
            (df["age_num"].notna()) &
            ((df["calc_age"] - df["age_num"]).abs() > 2)
        ].copy()
        mismatch["Issue"] = "Age & DOB differ by >2 years"
        issues.append(mismatch)

    if not issues:
        return pd.DataFrame()

    out = pd.concat(issues, ignore_index=True)

    cols = []
    for c in [
        "Household Code",
        "Community Health Unit (CHU)",
        "Enumerator name",
        "Enter a date",
        "Member name",
        "age",
        "Member date of birth",
        "Issue",
    ]:
        if c in out.columns:
            cols.append(c)

    return out[cols].drop_duplicates()


# ===========================================================
# PDF GENERATOR
# ===========================================================

def build_pdf_report(filtered_main,
                     members_geo,
                     col_subcounty,
                     col_ward,
                     col_facility,
                     n_households,
                     hh_children,
                     n_members,
                     n_u5,
                     n_u1,
                     cov_u1_dict,
                     n_girls10,
                     hpv1_10,
                     n_girls10_14,
                     hpv1_10_14):
    """
    Build a simple narrative PDF report with key summary stats.
    """
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    y = height - 2 * cm

    def line(txt, size=10, bold=False):
        nonlocal y
        if bold:
            c.setFont("Helvetica-Bold", size)
        else:
            c.setFont("Helvetica", size)
        c.drawString(2 * cm, y, txt)
        y -= 0.5 * cm
        if y < 2 * cm:
            c.showPage()
            y = height - 2 * cm

    # Title
    line("Muthambi HPV Household Mapping Report", 14, True)
    line("Filtered dashboard summary", 11)
    y -= 0.5 * cm

    # Overview
    line("1. Household and member summary", 12, True)
    line(f"• Households visited: {n_households}")
    line(f"• Members recorded: {n_members}")
    if hh_children is not None:
        line(f"• Households with children <18y: {hh_children}")

    # Children summary
    line("2. Child statistics (from household members)", 12, True)
    line(f"• Children under 5 years: {n_u5}")
    line(f"• Children under 1 year: {n_u1}")

    if n_u1 > 0 and cov_u1_dict:
        line("• Antigen coverage among under 1s:")
        for k, v in cov_u1_dict.items():
            line(f"   - {k}: {v:.1f}%")

    # HPV summary
    line("3. HPV vaccination summary", 12, True)
    if n_girls10 > 0:
        line(f"• 10-year-old girls: {n_girls10}")
        if hpv1_10 is not None:
            line(f"   - HPV1 coverage (10-year-old girls): {hpv1_10:.1f}%")
    else:
        line("• 10-year-old girls: 0")

    if n_girls10_14 > 0:
        line(f"• Girls 10–14 years: {n_girls10_14}")
        if hpv1_10_14 is not None:
            line(f"   - HPV1 coverage (10–14-year-old girls): {hpv1_10_14:.1f}%")
    else:
        line("• Girls 10–14 years: 0")

    # Geography note
    line("4. Coverage area", 12, True)
    if col_subcounty and col_subcounty in filtered_main.columns:
        subs = sorted(filtered_main[col_subcounty].dropna().unique().tolist())
        if subs:
            line(f"• Sub-counties: {', '.join(map(str, subs))}")
    if col_ward and col_ward in filtered_main.columns:
        wards = sorted(filtered_main[col_ward].dropna().unique().tolist())
        if wards:
            line(f"• Wards (sample): {', '.join(map(str, wards[:10]))}"
                 + (" ..." if len(wards) > 10 else ""))

    c.showPage()
    c.save()
    return buffer.getvalue()


# ===========================================================
# STREAMLIT APP START
# ===========================================================

st.title("Muthambi Sub-County – HPV Household Mapping Dashboard")

# -------------------- Upload --------------------
with st.sidebar:
    st.header("Upload Data File")
    data_file = st.file_uploader("Upload Kobo Excel Export", type=["xlsx"])

if not data_file:
    st.info("Please upload the Kobo export Excel file to begin.")
    st.stop()

main_df, members_df = load_household_and_members(data_file)
members_full = merge_members_with_households(main_df, members_df)

# Ensure age numeric
if "age" in members_full.columns:
    members_full["age"] = pd.to_numeric(members_full["age"], errors="coerce")

# -------------------- Field Mapping --------------------
col_subcounty = "Sub-county" if "Sub-county" in main_df.columns else None
col_ward = "Ward" if "Ward" in main_df.columns else None
col_facility = "Linked / Nearest Health Facility" if "Linked / Nearest Health Facility" in main_df.columns else None
col_cu = "Community Health Unit (CHU)" if "Community Health Unit (CHU)" in main_df.columns else None
col_enum = "Enumerator name" if "Enumerator name" in main_df.columns else None

# -------------------- Filters --------------------
with st.sidebar:
    st.header("Filters")

    df_filt = main_df.copy()

    # Geography filters
    if col_subcounty:
        opts = sorted(main_df[col_subcounty].dropna().unique())
        sel = st.multiselect("Sub-county", opts, default=opts)
        df_filt = df_filt[df_filt[col_subcounty].isin(sel)]

    if col_ward:
        opts = sorted(df_filt[col_ward].dropna().unique())
        sel = st.multiselect("Ward", opts)
        if sel:
            df_filt = df_filt[df_filt[col_ward].isin(sel)]

    if col_facility:
        opts = sorted(df_filt[col_facility].dropna().unique())
        sel = st.multiselect("Facility", opts)
        if sel:
            df_filt = df_filt[df_filt[col_facility].isin(sel)]

    # Date range filter based on "Enter a date"
    if "Enter a date" in df_filt.columns:
        df_filt["_visit_date"] = pd.to_datetime(df_filt["Enter a date"], errors="coerce")
        min_date = df_filt["_visit_date"].min()
        max_date = df_filt["_visit_date"].max()
        if pd.notna(min_date) and pd.notna(max_date):
            start_date, end_date = st.date_input(
                "Visit date range",
                value=(min_date.date(), max_date.date())
            )
            mask = (df_filt["_visit_date"].dt.date >= start_date) & (df_filt["_visit_date"].dt.date <= end_date)
            df_filt = df_filt[mask]
        df_filt = df_filt.drop(columns=["_visit_date"], errors="ignore")

# Filter members by households (geography + date)
if "_uuid" in df_filt.columns and "_uuid" in members_full.columns:
    members_geo = members_full[members_full["_uuid"].isin(df_filt["_uuid"])]
else:
    members_geo = members_full.copy()

# ===========================================================
# DERIVED COUNTS FOR NEW METRICS
# ===========================================================

# Under-1 with Penta 1 = "No" (age-based)
if "age" in members_geo.columns and "Was Penta 1 Administered" in members_geo.columns:
    u1_no_penta1 = members_geo[
        (members_geo["age"].notna()) &
        (members_geo["age"] < 1) &
        (members_geo["Was Penta 1 Administered"] == "No")
    ]
    n_u1_no_penta1 = len(u1_no_penta1)
else:
    n_u1_no_penta1 = 0

# Children <5 with Measles = Yes and Measles 2 != Yes (age-based)
if "age" in members_geo.columns and \
   "Was Measles Administered" in members_geo.columns and \
   "Was Measles 2 Administered" in members_geo.columns:
    meas_gap = members_geo[
        (members_geo["age"].notna()) &
        (members_geo["age"] < 5) &
        (members_geo["Was Measles Administered"] == "Yes") &
        (members_geo["Was Measles 2 Administered"] != "Yes")
    ]
    n_meas_yes_meas2_no = len(meas_gap)
else:
    n_meas_yes_meas2_no = 0

# ===========================================================
# SUMMARY CARDS
# ===========================================================

st.subheader("Summary (based on geography + date filters)")

n_households = len(df_filt)
n_members = len(members_geo)

if "Does this household has children under the age of 18 years?" in df_filt.columns:
    hh_children = (df_filt["Does this household has children under the age of 18 years?"] == "Yes").sum()
else:
    hh_children = None

# Child stats
n_u5, n_u1, cov_u1_dict = compute_child_antigen_coverages(members_geo)
penta1_u1 = cov_u1_dict.get("Was Penta 1 Administered", None)

# HPV1 coverage 10-year-olds & 10–14
n_girls10, hpv1_10 = compute_hpv1_coverage_for_group(members_geo, 10, 10)
n_girls10_14, hpv1_10_14 = compute_hpv1_coverage_for_group(members_geo, 10, 14)

# Row 1
c1, c2, c3, c4 = st.columns(4)
c1.metric("Households visited", n_households)
c2.metric("Members recorded", n_members)
c3.metric("HHs with children <18y", hh_children if hh_children is not None else "—")
c4.metric("Children <5 years", n_u5)

# Row 2
c5, c6, c7, c8 = st.columns(4)
c5.metric("Children <1 year", n_u1)
c6.metric(
    "Penta 1 coverage (under 1s)",
    f"{penta1_u1:.1f}%" if penta1_u1 is not None else "—"
)
# Show one more key antigen if available
other_antigen_label = None
other_antigen_cov = None
for k in ["Fully Immunized", "Was Measles Administered", "Was Penta 3 Administered"]:
    if k in cov_u1_dict:
        other_antigen_label = k
        other_antigen_cov = cov_u1_dict[k]
        break
if other_antigen_label:
    c7.metric(
        f"{other_antigen_label} (under 1s)",
        f"{other_antigen_cov:.1f}%"
    )
else:
    c7.metric("Other child antigen (under 1s)", "—")

c8.metric(
    "HPV1 – 10-year-old girls",
    f"{hpv1_10:.1f}% (n={n_girls10})" if hpv1_10 is not None else f"n={n_girls10}"
)

# Row 3 – new cards + HPV1 10–14
c9, c10, c11 = st.columns(3)
c9.metric(
    "HPV1 – 10–14-year-old girls",
    f"{hpv1_10_14:.1f}% (n={n_girls10_14})" if hpv1_10_14 is not None else f"n={n_girls10_14}"
)
c10.metric(
    "Children <5: Measles 1 Yes, Measles 2 No",
    n_meas_yes_meas2_no
)
c11.metric(
    "Under-1: Penta 1 = No",
    n_u1_no_penta1
)

# Summary table for under-1 coverage
st.markdown("#### Coverage among under-1 children (all antigens captured)")
if n_u1 > 0 and cov_u1_dict:
    cov_df = pd.DataFrame({
        "Antigen": list(cov_u1_dict.keys()),
        "Coverage_under1(%)": [round(v, 1) for v in cov_u1_dict.values()]
    })
    st.dataframe(cov_df, use_container_width=True, height=200)
else:
    st.info("No under-1 children or no antigen coverage data available in the current filters.")

# ===========================================================
# TABS
# ===========================================================

tab_overview, tab_members, tab_datacheck = st.tabs(
    ["Overview (Households)", "Household Members", "Datacheck"]
)

# ===========================================================
# TAB 1: OVERVIEW
# ===========================================================

with tab_overview:
    st.markdown("### Household Distribution & Penta 1 Gaps")

    col_left, col_right = st.columns(2)

    with col_left:
        if col_ward and col_ward in df_filt.columns:
            ward_counts = df_filt[col_ward].value_counts().reset_index()
            ward_counts.columns = ["Ward", "Households"]
            fig = px.bar(
                ward_counts,
                x="Ward",
                y="Households",
                title="Households by Ward",
            )
            fig.update_layout(bargap=0.2)
            st.plotly_chart(fig, use_container_width=True)

        if col_facility and col_facility in df_filt.columns:
            fac_counts = df_filt[col_facility].value_counts().reset_index()
            fac_counts.columns = ["Facility", "Households"]
            fig_fac = px.bar(
                fac_counts,
                x="Households",
                y="Facility",
                orientation="h",
                title="Households by Linked Facility",
            )
            fig_fac.update_layout(bargap=0.2)
            st.plotly_chart(fig_fac, use_container_width=True)

    with col_right:
        # Penta 1 gap map
        st.markdown("#### Map – children <5 who have NOT received Penta 1")
        required_cols = ["age", "Was Penta 1 Administered",
                         "_Location_latitude", "_Location_longitude"]
        if all(col in members_geo.columns for col in required_cols):
            df_p1 = members_geo.copy()
            df_p1 = df_p1[
                (df_p1["age"].notna()) &
                (df_p1["age"] < 5) &
                (df_p1["Was Penta 1 Administered"] == "No")
            ].dropna(subset=["_Location_latitude", "_Location_longitude"])

            if not df_p1.empty:
                df_p1["Age (yrs)"] = df_p1["age"].astype(int).astype(str)

                fig_p1_map = px.scatter_mapbox(
                    df_p1,
                    lat="_Location_latitude",
                    lon="_Location_longitude",
                    color="Age (yrs)",
                    hover_name="Member name" if "Member name" in df_p1.columns else None,
                    hover_data=[col_facility] if col_facility in df_p1.columns else None,
                    zoom=10,
                    height=350,
                )
                fig_p1_map.update_layout(mapbox_style="open-street-map")
                fig_p1_map.update_layout(
                    title="Children <5 who have NOT received Penta 1 (coloured by age in years)"
                )
                st.plotly_chart(fig_p1_map, use_container_width=True)
            else:
                st.info("No children <5 with 'No' for Penta 1 found in the current filters.")
        else:
            st.info(
                "Missing columns needed for Penta 1 gap map "
                "(age, 'Was Penta 1 Administered', GPS)."
            )

        # Household distribution heat map
        st.markdown("#### Household distribution heatmap")
        if "_Location_latitude" in df_filt.columns and "_Location_longitude" in df_filt.columns:
            df_hh_map = df_filt.dropna(
                subset=["_Location_latitude", "_Location_longitude"]
            ).copy()

            if not df_hh_map.empty:
                fig_hh_heat = px.density_mapbox(
                    df_hh_map,
                    lat="_Location_latitude",
                    lon="_Location_longitude",
                    radius=15,
                    zoom=10,
                    height=350,
                )
                fig_hh_heat.update_layout(mapbox_style="open-street-map")
                fig_hh_heat.update_layout(
                    title="Household distribution heatmap (filtered households)"
                )
                st.plotly_chart(fig_hh_heat, use_container_width=True)
            else:
                st.info("No household GPS coordinates available for the current filters.")
        else:
            st.info("No GPS columns on the household sheet for the heatmap (`_Location_latitude` / `_Location_longitude`).")

# ===========================================================
# TAB 2: HOUSEHOLD MEMBERS
# ===========================================================

with tab_members:
    st.markdown("### Member-level distributions")

    col_left2, col_right2 = st.columns(2)

    # Age distribution
    with col_left2:
        if "age" in members_geo.columns:
            age_df = members_geo[(members_geo["age"].notna())]
            if not age_df.empty:
                fig_age = px.histogram(
                    age_df,
                    x="age",
                    nbins=15,
                    title="Age distribution (all members in filters)",
                )
                fig_age.update_layout(bargap=0.2)
                st.plotly_chart(fig_age, use_container_width=True)
            else:
                st.info("No members in current filters.")
        else:
            st.info("Age column not found in member data.")

        # School status – girls 10–14
        if all(col in members_geo.columns for col in ["age", "Member sex", "Member school status"]):
            df_school = members_geo.copy()
            girls_10_14 = df_school[
                (df_school["Member sex"] == "Female") &
                (df_school["age"].between(10, 14))
            ]
            if not girls_10_14.empty:
                school_counts = (
                    girls_10_14["Member school status"]
                    .value_counts(dropna=False)
                    .reset_index()
                )
                school_counts.columns = ["School status", "Count"]
                fig_school = px.pie(
                    school_counts,
                    names="School status",
                    values="Count",
                    title="School status – girls 10–14 yrs",
                )
                st.plotly_chart(fig_school, use_container_width=True)
            else:
                st.info("No girls 10–14 yrs in current filters.")
        else:
            st.info("Missing columns to compute school status for girls 10–14.")

        # NEW: where girls were immunized (HPV1 place)
        st.markdown("#### Where girls 10–14 received HPV dose 1")
        # Try to detect the HPV1 place-of-vaccination column
        possible_hpv_place_cols = [
            "Where did you receive the HPV 1st Dose?",
            "Where did you receive HPV 1st dose ?",
            "Where did you receive HPV dose 1?",
            "Where did you receive HPV Dose 1 ?",
            "Where were you immunized with HPV 1st Dose ?",
        ]
        hpv_place_col = None
        for col in possible_hpv_place_cols:
            if col in members_geo.columns:
                hpv_place_col = col
                break

        if hpv_place_col and all(c in members_geo.columns for c in ["age", "Member sex", "Have you received HPV 1st Dose ?"]):
            df_place = members_geo.copy()
            girls_hpv1 = df_place[
                (df_place["Member sex"] == "Female") &
                (df_place["age"].between(10, 14)) &
                (df_place["Have you received HPV 1st Dose ?"] == "Yes")
            ].copy()
            girls_hpv1 = girls_hpv1[girls_hpv1[hpv_place_col].notna()]

            if not girls_hpv1.empty:
                place_counts = (
                    girls_hpv1[hpv_place_col]
                    .value_counts()
                    .reset_index()
                )
                place_counts.columns = ["Place of HPV1", "Girls"]
                fig_place = px.bar(
                    place_counts,
                    x="Place of HPV1",
                    y="Girls",
                    title="Place of HPV1 vaccination – girls 10–14 yrs",
                )
                fig_place.update_layout(bargap=0.2)
                st.plotly_chart(fig_place, use_container_width=True)
            else:
                st.info("No HPV1 'Yes' records with place information for girls 10–14 in the current filters.")
        else:
            st.info("HPV1 place-of-vaccination column not found, or HPV1 status/age/sex missing.")

    # HPV & map of non-vaccinated girls
    with col_right2:
        st.markdown("#### HPV coverage by ward – girls 10–14 yrs")

        if all(col in members_geo.columns for col in [
            "age", "Member sex", "Have you received HPV 1st Dose ?"
        ]) and col_ward and col_ward in members_geo.columns:

            df_hpv = members_geo.copy()
            girls_10_14 = df_hpv[
                (df_hpv["Member sex"] == "Female") &
                (df_hpv["age"].between(10, 14))
            ].copy()

            if not girls_10_14.empty:
                girls_10_14["hpv1_yes"] = girls_10_14["Have you received HPV 1st Dose ?"] == "Yes"
                cov_by_ward = (
                    girls_10_14.groupby(col_ward)["hpv1_yes"]
                    .mean()
                    .reset_index()
                )
                cov_by_ward["HPV1 coverage (%)"] = cov_by_ward["hpv1_yes"] * 100
                fig_cov = px.bar(
                    cov_by_ward,
                    x=col_ward,
                    y="HPV1 coverage (%)",
                    title="HPV1 coverage by ward – girls 10–14 yrs",
                    labels={"HPV1 coverage (%)": "Coverage (%)"},
                )
                fig_cov.update_layout(bargap=0.2, yaxis=dict(ticksuffix="%"))
                st.plotly_chart(fig_cov, use_container_width=True)
            else:
                st.info("No girls 10–14 yrs in current filters.")
        else:
            st.info("Missing columns for HPV1 coverage by ward (check age, sex, HPV1, ward).")

        st.markdown("#### Map – girls 10–14 who have NOT received HPV1")
        if all(col in members_geo.columns for col in ["age", "Member sex", "Have you received HPV 1st Dose ?"]) and \
           "_Location_latitude" in members_geo.columns and "_Location_longitude" in members_geo.columns:

            df_map = members_geo.copy()
            girls_10_14_all = df_map[
                (df_map["Member sex"] == "Female") &
                (df_map["age"].between(10, 14))
            ].copy()
            if not girls_10_14_all.empty:
                girls_10_14_all["hpv1_yes"] = girls_10_14_all["Have you received HPV 1st Dose ?"] == "Yes"
                non_vacc = girls_10_14_all[~girls_10_14_all["hpv1_yes"]].dropna(
                    subset=["_Location_latitude", "_Location_longitude"]
                )
                if not non_vacc.empty:
                    # Colour by age in years
                    non_vacc["Age (yrs)"] = non_vacc["age"].astype(int).astype(str)

                    fig_nv = px.scatter_mapbox(
                        non_vacc,
                        lat="_Location_latitude",
                        lon="_Location_longitude",
                        color="Age (yrs)",
                        hover_name="Member name" if "Member name" in non_vacc.columns else None,
                        hover_data=[col_facility] if col_facility in non_vacc.columns else None,
                        zoom=10,
                        height=400,
                    )
                    fig_nv.update_layout(mapbox_style="open-street-map")
                    fig_nv.update_layout(
                        title="Girls 10–14 who have NOT received HPV1 (coloured by age in years)"
                    )
                    st.plotly_chart(fig_nv, use_container_width=True)
                else:
                    st.info("All mapped girls 10–14 appear to have received HPV1, or no GPS data.")
            else:
                st.info("No girls 10–14 yrs in geography-filtered data.")
        else:
            st.info("Missing columns for non-vaccinated girls map (age, sex, HPV1, GPS).")

    # ---------- TABLE + CHARTS: GIRLS 10–14 WHO HAVE NOT RECEIVED HPV1 ----------
    st.markdown("### Girls 10–14 years who have NOT received HPV1")
    girls_no_hpv1 = pd.DataFrame()
    if all(col in members_geo.columns for col in ["age", "Member sex", "Have you received HPV 1st Dose ?"]):
        girls_no_hpv1 = members_geo[
            (members_geo["Member sex"] == "Female") &
            (members_geo["age"].between(10, 14)) &
            (members_geo["Have you received HPV 1st Dose ?"] != "Yes")
        ].copy()

        if not girls_no_hpv1.empty:
            cols_to_show = []
            for col in [
                "Household Code",
                "Member name",
                "age",
                "Member school status",
                "Have you received HPV 1st Dose ?",
                col_cu,
                col_facility,
            ]:
                if col and col in girls_no_hpv1.columns:
                    cols_to_show.append(col)

            if not cols_to_show:
                cols_to_show = girls_no_hpv1.columns.tolist()

            st.dataframe(
                girls_no_hpv1[cols_to_show].sort_values("age"),
                use_container_width=True,
                height=350,
            )

            st.markdown("#### Age distribution – girls 10–14 without HPV1")
            if "age" in girls_no_hpv1.columns:
                fig_g_age = px.histogram(
                    girls_no_hpv1,
                    x="age",
                    nbins=5,
                    title="Age distribution (girls 10–14 without HPV1)",
                )
                fig_g_age.update_layout(bargap=0.2)
                st.plotly_chart(fig_g_age, use_container_width=True)

            st.markdown("#### Numbers per Community Health Unit (CHU)")
            if col_cu and col_cu in girls_no_hpv1.columns:
                cu_counts = (
                    girls_no_hpv1[col_cu]
                    .value_counts()
                    .reset_index()
                )
                cu_counts.columns = ["CHU", "Girls without HPV1"]
                fig_g_cu = px.bar(
                    cu_counts,
                    x="CHU",
                    y="Girls without HPV1",
                    title="Girls 10–14 without HPV1 by CHU",
                )
                fig_g_cu.update_layout(bargap=0.2)
                st.plotly_chart(fig_g_cu, use_container_width=True)
            else:
                st.info("Community Health Unit (CHU) column not found for this group.")

            st.markdown("#### Numbers per Facility")
            if col_facility and col_facility in girls_no_hpv1.columns:
                fac_counts = (
                    girls_no_hpv1[col_facility]
                    .value_counts()
                    .reset_index()
                )
                fac_counts.columns = ["Facility", "Girls without HPV1"]
                fig_g_fac = px.bar(
                    fac_counts,
                    x="Facility",
                    y="Girls without HPV1",
                    title="Girls 10–14 without HPV1 by facility",
                )
                fig_g_fac.update_layout(bargap=0.2)
                st.plotly_chart(fig_g_fac, use_container_width=True)
            else:
                st.info("Facility column not found for this group.")
        else:
            st.info("No girls 10–14 in the current filters who are missing HPV1.")
    else:
        st.info("Required columns for HPV1 status table (age, sex, HPV1) not found.")

    # -------------------------------------------------------
    # ANTIGEN STACKED CHARTS PER CU (CHILD ANTIGENS + HPV)
    # -------------------------------------------------------
    st.markdown("### Antigen uptake by Community Health Unit (stacked by age)")

    if col_cu and col_cu in members_geo.columns:
        cu_opts = sorted(members_geo[col_cu].dropna().unique())
        selected_cu = st.selectbox("Select Community Health Unit (CHU)", cu_opts)

        df_cu = members_geo[members_geo[col_cu] == selected_cu].copy()

        # --- Child antigens: use children <5
        st.markdown("#### Childhood antigens – children <5 years (Yes counts, stacked by age)")
        if "age" in df_cu.columns:
            df_children = df_cu[(df_cu["age"].notna()) & (df_cu["age"] < 5)].copy()
        else:
            df_children = pd.DataFrame()

        child_antigen_cols = [
            "Was Penta 1 Administered",
            "Was Penta 2 Administered",
            "Was Penta 3 Administered",
            "Was Measles Administered",
            "Was Measles 2 Administered",
            "Fully Immunized",
        ]

        records = []
        if not df_children.empty:
            for antigen in child_antigen_cols:
                if antigen in df_children.columns:
                    yes_df = df_children[df_children[antigen] == "Yes"]
                    if not yes_df.empty:
                        counts = (
                            yes_df.groupby("age")
                            .size()
                            .reset_index(name="Count")
                        )
                        for _, row in counts.iterrows():
                            records.append({
                                "Antigen": antigen,
                                "Age": int(row["age"]),
                                "Count": int(row["Count"]),
                            })

        if records:
            df_chart = pd.DataFrame(records)
            fig_child = px.bar(
                df_chart,
                x="Antigen",
                y="Count",
                color="Age",
                barmode="stack",
                title=f"Number of children <5 with 'Yes' for each antigen – {selected_cu}",
                labels={"Count": "Number of children"},
            )
            fig_child.update_layout(bargap=0.2)
            st.plotly_chart(fig_child, use_container_width=True)
        else:
            st.info("No 'Yes' responses for childhood antigens among children <5 in this CU.")

        # --- HPV antigens: girls 10–14
        st.markdown("#### HPV doses – girls 10–14 years (Yes counts, stacked by age)")
        hpv_cols = [
            "Have you received HPV 1st Dose ?",
            "Have you received HPV dose 2?",
        ]

        df_hpv_cu = df_cu.copy()
        if all(col in df_hpv_cu.columns for col in ["age", "Member sex"]):
            df_girls_hpv = df_hpv_cu[
                (df_hpv_cu["Member sex"] == "Female") &
                (df_hpv_cu["age"].between(10, 14))
            ].copy()
        else:
            df_girls_hpv = pd.DataFrame()

        hpv_records = []
        if not df_girls_hpv.empty:
            for antigen in hpv_cols:
                if antigen in df_girls_hpv.columns:
                    yes_df = df_girls_hpv[df_girls_hpv[antigen] == "Yes"]
                    if not yes_df.empty:
                        counts = (
                            yes_df.groupby("age")
                            .size()
                            .reset_index(name="Count")
                        )
                        for _, row in counts.iterrows():
                            hpv_records.append({
                                "Antigen": antigen,
                                "Age": int(row["age"]),
                                "Count": int(row["Count"]),
                            })

        if hpv_records:
            df_hpv_chart = pd.DataFrame(hpv_records)
            fig_hpv = px.bar(
                df_hpv_chart,
                x="Antigen",
                y="Count",
                color="Age",
                barmode="stack",
                title=f"Number of girls 10–14 with 'Yes' for HPV doses – {selected_cu}",
                labels={"Count": "Number of girls"},
            )
            fig_hpv.update_layout(bargap=0.2)
            st.plotly_chart(fig_hpv, use_container_width=True)
        else:
            st.info("No 'Yes' HPV responses among girls 10–14 in this CU.")
    else:
        st.info("Community Health Unit (CHU) column not found in the data, so CU-level charts cannot be drawn.")

# ===========================================================
# TAB 3: DATACHECK
# ===========================================================

with tab_datacheck:
    st.markdown("## Datacheck – age & date-of-birth issues")

    df_dc = df_filt.copy()

    col1_dc, col2_dc = st.columns(2)
    with col1_dc:
        if col_enum and col_enum in df_dc.columns:
            enum_opts = sorted(df_dc[col_enum].dropna().unique())
            sel_enum = st.multiselect("Enumerator", enum_opts, default=enum_opts)
            df_dc = df_dc[df_dc[col_enum].isin(sel_enum)]
    with col2_dc:
        if col_cu and col_cu in df_dc.columns:
            cu_opts = sorted(df_dc[col_cu].dropna().unique())
            sel_cu = st.multiselect("Community Health Unit (CHU)", cu_opts)
            if sel_cu:
                df_dc = df_dc[df_dc[col_cu].isin(sel_cu)]

    # Members for datacheck – geography + date + enumerator/CU filters
    if "_uuid" in df_dc.columns and "_uuid" in members_geo.columns:
        members_dc = members_geo[members_geo["_uuid"].isin(df_dc["_uuid"])]
    else:
        members_dc = members_geo.copy()

    issues = build_datacheck_issues(members_dc)

    st.markdown("### Records requiring action (age / DOB issues)")
    if not issues.empty:
        st.dataframe(issues, use_container_width=True, height=350)
        st.caption(f"{len(issues)} member records flagged for follow-up.")
    else:
        st.success("No age / DOB issues detected for the current filters.")

    st.markdown("### Households reached per enumerator per day")

    if "Enter a date" in df_dc.columns and col_enum and col_enum in df_dc.columns and "Household Code" in df_dc.columns:
        df_enum = df_dc.copy()
        df_enum["Enter a date"] = pd.to_datetime(df_enum["Enter a date"], errors="coerce")
        df_enum = df_enum.dropna(subset=["Enter a date"])

        counts = (
            df_enum.groupby(["Enter a date", col_enum])["Household Code"]
            .nunique()
            .reset_index(name="Households")
        )

        if not counts.empty:
            fig_enum = px.bar(
                counts,
                x="Enter a date",
                y="Households",
                color=col_enum,
                title="Households reached per enumerator per day",
                labels={"Enter a date": "Date", "Households": "Households visited"},
            )
            fig_enum.update_layout(bargap=0.2)
            st.plotly_chart(fig_enum, use_container_width=True)
        else:
            st.info("No data for the enumerator/day chart with the current filters.")
    else:
        st.info("Date, enumerator, or household code columns not found for productivity chart.")

# ===========================================================
# EXPORT PDF
# ===========================================================

st.markdown("---")
st.subheader("Export PDF Report")

pdf_bytes = build_pdf_report(
    filtered_main=df_filt,
    members_geo=members_geo,
    col_subcounty=col_subcounty,
    col_ward=col_ward,
    col_facility=col_facility,
    n_households=n_households,
    hh_children=hh_children,
    n_members=n_members,
    n_u5=n_u5,
    n_u1=n_u1,
    cov_u1_dict=cov_u1_dict,
    n_girls10=n_girls10,
    hpv1_10=hpv1_10,
    n_girls10_14=n_girls10_14,
    hpv1_10_14=hpv1_10_14,
)

st.download_button(
    "Download PDF Report",
    data=pdf_bytes,
    file_name="HPV_Muthambi_Report.pdf",
    mime="application/pdf",
)
