
from fastapi import FastAPI, Query, HTTPException
from pydantic import BaseModel
from enum import Enum
from collections import defaultdict
from datetime import date, datetime, timedelta


app = FastAPI()


# =====================================================
# 1. ENUM MODELS
# =====================================================

# TimeLine defines how the user wants to view the graph.
# Based on the selected timeline, we decide how to group the data.
#
# daily   -> hourly data
# weekly  -> daily data
# monthly -> weekly data
# yearly  -> monthly data

class TimeLine(str, Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    YEARLY = "yearly"


# Interval represents the actual grouping of data
# used to generate points in the graph.

class IntervalTime(str, Enum):
    HOURS = "hours"
    DAYS = "days"
    WEEKS = "weeks"
    MONTHS = "months"


# =====================================================
# 2. RESPONSE MODELS
# =====================================================

# Defines the structure of one record in the final insights.
# Every record contains the date bucket, theme, enrollment count
# and total revenue for that particular theme and time period.

class ThemeInsightData(BaseModel):

    date: str
    theme_name: str
    enrollment_count: int
    revenue: float


# Defines the complete API response structure.
# FastAPI uses this model to validate the returned response.

class ThemeInsightResponse(BaseModel):

    success: bool
    message: str
    timeline: TimeLine
    interval: IntervalTime
    start_date: date | None
    end_date: date | None
    data: list[ThemeInsightData]



# =====================================================
# 5. EFFECTIVE DATE VALIDATION
# =====================================================

def is_valid_booking(booking, camp):

    # Convert the booking creation timestamp into a date.
    # Example: 2026-10-12T10:42:14 becomes 2026-10-12.

    booking_date = datetime.fromisoformat(
        booking["created_at"]
    ).date()

    # Get the date from which this offering becomes active.

    effective_from = date.fromisoformat(
        camp["effective_from"]
    )

    # Get the offering's ending date.
    # It can be None when there is no ending date.

    effective_to = camp["effective_to"]

    # A booking created before the offering becomes active
    # should not be included in the insights.

    if booking_date < effective_from:
        return False

    # If an ending date exists, check whether the booking
    # was created after that date.

    if effective_to is not None:

        effective_to = date.fromisoformat(effective_to)

        if booking_date > effective_to:
            return False

    # The booking falls within the offering's effective period.
    return True


# =====================================================
# 6. TIMELINE AND INTERVAL MAPPING
# =====================================================

def get_interval(timeline):

    # Each timeline has a corresponding data interval.
    # Daily timeline   -> Hourly records
    # Weekly timeline  -> Daily records
    # Monthly timeline -> Weekly records
    # Yearly timeline  -> Monthly records

    interval_mapping = {

        TimeLine.DAILY: IntervalTime.HOURS,

        TimeLine.WEEKLY: IntervalTime.DAYS,

        TimeLine.MONTHLY: IntervalTime.WEEKS,

        TimeLine.YEARLY: IntervalTime.MONTHS
    }

    # Return the interval associated with the selected timeline.
    return interval_mapping[timeline]


# =====================================================
# 7. CREATE DATE BUCKETS
# =====================================================

def get_bucket_date(booking_datetime, timeline):

    # Extract only the date when date-wise grouping is required.
    booking_date = booking_datetime.date()

    # DAILY TIMELINE:
    # Group bookings by hour.
    # Example: 10:00, 11:00, 12:00.

    if timeline == TimeLine.DAILY:

        return booking_datetime.strftime("%Y-%m-%d %H:00")

    # WEEKLY TIMELINE:
    # Group bookings by individual calendar days.
    # Example: 2026-10-12, 2026-10-13.

    elif timeline == TimeLine.WEEKLY:

        return booking_date.strftime("%Y-%m-%d")

    # MONTHLY TIMELINE:
    # Group bookings into Monday-to-Sunday weeks.
    # First calculate the Monday of the booking's week.

    elif timeline == TimeLine.MONTHLY:

        week_start = booking_date - timedelta(
            days=booking_date.weekday()
        )

        # Calculate the Sunday of the same week.

        week_end = week_start + timedelta(days=6)

        # Return the complete week range as one bucket.

        return (
            f"{week_start:%Y-%m-%d} "
            f"to {week_end:%Y-%m-%d}"
        )

    # YEARLY TIMELINE:
    # Group bookings by calendar month.
    # Example: 2026-01, 2026-02.

    elif timeline == TimeLine.YEARLY:

        return booking_date.strftime("%Y-%m")

    # Use daily format as the default bucket.
    return booking_date.strftime("%Y-%m-%d")


# =====================================================
# 8. INSIGHTS SERVICE
# =====================================================

async def get_daily_theme_insights(

    timeline: TimeLine = TimeLine.DAILY,

    custom_start: date | None = None,

    custom_end: date | None = None

):

    # -------------------------------------------------
    # STEP 1: VALIDATE CUSTOM DATE RANGE
    # -------------------------------------------------

    # Both dates are optional.
    # If both are provided, the start date should not
    # be greater than the end date.

    if custom_start and custom_end:

        if custom_start > custom_end:

            raise HTTPException(
                status_code=400,
                detail="Start date cannot be greater than end date."
            )


    # -------------------------------------------------
    # STEP 2: FETCH DATA FROM BOTH SERVICES
    # -------------------------------------------------

    # Fetch booking records from Ledger Service.

    bookings = await get_booking_data()

    # Fetch camp and theme information from Core Service.

    core_response = await get_camp_data()

    # Core Service returns a nested dictionary.
    #
    # core_response
    #     |
    #     |-- success
    #     |-- message
    #     |-- data
    #           |
    #           |-- camps_data
    #
    # Extract only the list of camp records because
    # we need to process each camp individually.

    camps = core_response["data"]["camps_data"]


    # -------------------------------------------------
    # STEP 3: MATCH BOOKING AND CAMP DATA
    # -------------------------------------------------

    # Create a dictionary where offering_id is the key
    # and the complete camp record is the value.
    #
    # This makes it easy to find camp details for a booking
    # without looping through all camps for every booking.

    camp_dict = {}

    for camp in camps:

        camp_dict[camp["offering_id"]] = camp


    # -------------------------------------------------
    # STEP 4: INITIALIZE RESULT STORAGE
    # -------------------------------------------------

    # defaultdict automatically creates a new record
    # whenever a new date and theme combination is found.
    #
    # Example:
    # ("2026-10-12 10:00", "Creative DIY")
    #
    # enrollment_count starts at 0.
    # revenue starts at 0.0.

    result = defaultdict(lambda: {

        "enrollment_count": 0,

        "revenue": 0.0

    })


    # -------------------------------------------------
    # STEP 5: PROCESS EACH BOOKING
    # -------------------------------------------------

    for booking in bookings:

        # Only confirmed bookings should contribute
        # to enrollment count and revenue.

        if booking["status"] != "confirmed":
            continue

        # Get the offering ID from the booking.
        # This ID is used to find the matching camp
        # from the Core Service.

        offering_id = booking["offering_id"]

        camp = camp_dict.get(offering_id)

        # If no matching camp exists, skip this booking.

        if not camp:
            continue

        # Check whether the booking was created within
        # the effective date range of the offering.

        if not is_valid_booking(booking, camp):
            continue

        # Convert the booking timestamp into datetime.
        # We preserve the time because daily timeline
        # requires hourly grouping.

        booking_datetime = datetime.fromisoformat(
            booking["created_at"]
        )

        booking_date = booking_datetime.date()

        # Apply the optional custom start date.
        # Skip bookings created before the selected date.

        if custom_start and booking_date < custom_start:
            continue

        # Apply the optional custom end date.
        # Skip bookings created after the selected date.

        if custom_end and booking_date > custom_end:
            continue

        # Get the theme name from the matching camp record.

        theme_name = camp["theme_name"]

        # Convert the booking date and time into the
        # required bucket based on the selected timeline.

        bucket = get_bucket_date(
            booking_datetime,
            timeline
        )

        # Combine date bucket and theme name.
        # This ensures each theme is calculated separately
        # for each time period.

        key = (bucket, theme_name)

        # Increase enrollment count for this theme and bucket.

        result[key]["enrollment_count"] += 1

        # Add the offering's weekly price to the revenue.
        # This assumes one weekly charge per confirmed booking.

        result[key]["revenue"] += float(
            camp["weekly_price"]
        )


    # -------------------------------------------------
    # STEP 6: PREPARE FINAL RESPONSE DATA
    # -------------------------------------------------

    response = []

    # Each dictionary entry represents one theme
    # for one date or time bucket.

    for (bucket, theme_name), values in sorted(result.items()):

        response.append({

            "date": bucket,

            "theme_name": theme_name,

            "enrollment_count": values["enrollment_count"],

            "revenue": round(values["revenue"], 2)

        })


    # -------------------------------------------------
    # STEP 7: RETURN FINAL INSIGHTS
    # -------------------------------------------------

    # Return the calculated data along with the selected
    # timeline, interval and custom date filters.

    return {

        "success": True,

        "message": "Theme insights fetched successfully.",

        "timeline": timeline,

        "interval": get_interval(timeline),

        "start_date": custom_start,

        "end_date": custom_end,

        "data": response

    }


# =====================================================
# 9. FASTAPI ENDPOINT
# =====================================================

@app.get(

    "/theme-insights",

    response_model=ThemeInsightResponse

)

async def get_call(

    # User can select daily, weekly, monthly or yearly.
    # Daily is selected automatically when no timeline is given.

    timeline: TimeLine = Query(
        default=TimeLine.DAILY
    ),

    # Custom start date is optional.
    # If not provided, no custom start-date filtering is applied.

    custom_start: date | None = Query(
        default=None
    ),

    # Custom end date is optional.
    # If not provided, no custom end-date filtering is applied.

    custom_end: date | None = Query(
        default=None
    )

):

    # Pass the user-selected filters to the Insights Service
    # and return its calculated response.

    return await get_daily_theme_insights(

        timeline=timeline,

        custom_start=custom_start,

        custom_end=custom_end

    )
