from typing import List, Optional
from fastapi import FastAPI, HTTPException, status, Query, Header, Request
from pydantic import BaseModel, Field, EmailStr
from datetime import datetime, timedelta
import logging
import json
from pathlib import Path
import httpx
import os
import uuid
from jose import jwt, jwk
from jose.exceptions import JWTError, ExpiredSignatureError

logger = logging.getLogger('uvicorn.error')
logger.setLevel(logging.DEBUG)

app = FastAPI(title="Hotel Booking API", version="1.0")

@app.middleware("http")
async def log_request_body(request: Request, call_next):
    """Middleware to log raw request body for debugging"""
    if request.method == "POST" and "/bookings" in request.url.path:
        body = await request.body()
        logger.info(f"RAW REQUEST BODY for {request.url.path}: {body}")
        logger.info(f"Content-Type: {request.headers.get('content-type')}")
        logger.info(f"Content-Length: {request.headers.get('content-length')}")
    response = await call_next(request)
    return response

# --- Configuration ---
# OIDC Discovery URL for getting JWKS endpoint
OIDC_DISCOVERY_URL = os.getenv(
    "OIDC_DISCOVERY_URL",
    "http://am-gateway:8092/gravitee/oidc/.well-known/openid-configuration"
)
# Expected audience (resource identifier for MCP Server)
EXPECTED_AUDIENCE = os.getenv(
    "EXPECTED_AUDIENCE",
    "gravitee-hotels"
)
AUTHZEN_PDP_URL = os.getenv(
    "AUTHZEN_PDP_URL",
    "http://am-gateway:8092/gravitee/access/v1/evaluation"
)

# HTTP client for fetching JWKS
http_client = httpx.AsyncClient(timeout=10.0)

# Cache for JWKS public keys
jwks_cache = None

# --- Models ---
class Accommodation(BaseModel):
    id: int
    name: str = Field(..., example="The Grand Hotel")
    location: str = Field(..., example="London")
    description: str = Field(..., example="Luxury hotel in the heart of the city")
    price_per_night: float = Field(..., example=250.0)
    available_rooms: int = Field(..., example=10)

class Booking(BaseModel):
    id: int
    user_email: EmailStr = Field(..., example="john.doe@gravitee.io")
    hotel_name: str = Field(..., example="Hotel California")
    room_number: str = Field(..., example="101")
    start_date: datetime = Field(..., example="2025-08-01T14:00:00")
    end_date: datetime = Field(..., example="2025-08-10T11:00:00")
    price: float = Field(..., example=1500.0)

class BookingCreate(BaseModel):
    hotel_name: Optional[str] = Field(None, example="Hotel California")
    location: Optional[str] = Field(None, example="Paris")
    start_date: datetime = Field(..., example="2025-08-01T14:00:00")
    end_date: datetime = Field(..., example="2025-08-10T11:00:00")

# --- Helper Functions ---
def load_accommodations() -> List[Accommodation]:
    """Load accommodations from external JSON file"""
    json_path = Path(__file__).parent / "accommodations.json"
    with open(json_path, 'r') as f:
        data = json.load(f)
    return [Accommodation(**item) for item in data]

def load_bookings() -> List[Booking]:
    """Load bookings from external JSON file with simple date format (YYYY-MM-DD)."""
    json_path = Path(__file__).parent / "bookings.json"
    try:
        with open(json_path, 'r') as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []  # Return empty list if file doesn't exist or is invalid

    bookings = []

    for item in data:
        try:
            # Parse simple date strings (YYYY-MM-DD)
            start_date_str = item['start_date']
            end_date_str = item['end_date']

            # Convert to datetime at midnight for compatibility with existing code
            start_date = datetime.fromisoformat(start_date_str)
            end_date = datetime.fromisoformat(end_date_str)

            booking = Booking(
                id=item['id'],
                user_email=item['user_email'],
                hotel_name=item['hotel_name'],
                room_number=item.get('room_number', 'N/A'),
                start_date=start_date,
                end_date=end_date,
                price=item['price']
            )
            bookings.append(booking)
        except (KeyError, TypeError, ValueError) as e:
            logger.error(f"Skipping invalid booking item due to error: {e}. Item: {item}")
            continue

    return bookings

def save_bookings(bookings: List[Booking]):
    """Save bookings to external JSON file with simple date format (YYYY-MM-DD)."""
    logger.info(f"--- Attempting to save {len(bookings)} bookings ---")
    json_path = Path(__file__).parent / "bookings.json"
    logger.debug(f"Saving bookings to file: {json_path}")

    bookings_to_save = []

    for booking in bookings:
        booking_dict = {
            "id": booking.id,
            "user_email": booking.user_email,
            "hotel_name": booking.hotel_name,
            "room_number": booking.room_number,
            "start_date": booking.start_date.date().isoformat(),  # YYYY-MM-DD
            "end_date": booking.end_date.date().isoformat(),      # YYYY-MM-DD
            "price": booking.price,
        }
        bookings_to_save.append(booking_dict)

    if bookings_to_save:
        logger.debug(f"Sample data to be saved: {bookings_to_save[0]}")

    try:
        with open(json_path, 'w') as f:
            json.dump(bookings_to_save, f, indent=4)
        logger.info(f"--- Successfully saved {len(bookings_to_save)} bookings to {json_path} ---")
    except IOError as e:
        logger.error(f"!!! FAILED to write to {json_path}: {e} !!!")
    except TypeError as e:
        logger.error(f"!!! FAILED to serialize bookings to JSON: {e} !!!")

# --- In-memory data ---
accommodations_db = load_accommodations()
bookings_db = load_bookings()

# --- Token Validation ---
async def get_jwks() -> dict:
    """Fetch JWKS (JSON Web Key Set) from OIDC discovery endpoint"""
    global jwks_cache

    if jwks_cache is not None:
        return jwks_cache

    try:
        # Get OIDC configuration
        oidc_response = await http_client.get(OIDC_DISCOVERY_URL)
        oidc_response.raise_for_status()
        oidc_config = oidc_response.json()

        # Get JWKS endpoint
        jwks_uri = oidc_config.get("jwks_uri")
        if not jwks_uri:
            raise ValueError("No jwks_uri in OIDC configuration")

        # Fetch JWKS
        jwks_response = await http_client.get(jwks_uri)
        jwks_response.raise_for_status()
        jwks_cache = jwks_response.json()

        logger.info(f"Fetched JWKS from {jwks_uri}")
        return jwks_cache

    except Exception as e:
        logger.error(f"Failed to fetch JWKS: {e}")
        raise

async def validate_token(
    token: str,
    required_audience: str,
    required_scopes: List[str]
) -> dict:
    """
    Validate JWT token by decoding and verifying signature with JWKS.

    Args:
        token: The JWT access token to validate
        required_audience: Expected audience (resource identifier)
        required_scopes: List of required scopes for this operation

    Returns:
        Decoded token payload (dict with claims like 'aud', 'scope', 'sub', etc.)

    Raises:
        HTTPException: If token is invalid, expired, or lacks required audience/scopes
    """
    try:
        # Get JWKS for signature verification
        jwks = await get_jwks()

        # Decode token header to get key ID (kid)
        unverified_header = jwt.get_unverified_header(token)
        kid = unverified_header.get("kid")

        # Find the matching public key
        rsa_key = None
        for key in jwks.get("keys", []):
            if key.get("kid") == kid:
                rsa_key = key
                break

        if not rsa_key:
            logger.warning(f"No matching key found for kid: {kid}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unable to find appropriate key for token verification"
            )

        # Decode and verify the token
        payload = jwt.decode(
            token,
            rsa_key,
            algorithms=["RS256"],
            options={"verify_aud": False}  # We'll verify audience manually
        )

        # Check audience (aud)
        token_audience = payload.get("aud")
        if isinstance(token_audience, list):
            token_audiences = token_audience
        elif isinstance(token_audience, str):
            token_audiences = [token_audience]
        else:
            token_audiences = []

        if required_audience not in token_audiences:
            logger.warning(f"Token audience mismatch. Expected: {required_audience}, Got: {token_audiences}")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Token does not have required audience: {required_audience}"
            )

        # Check scopes
        token_scope = payload.get("scope", "")
        token_scopes = token_scope.split() if isinstance(token_scope, str) else []

        missing_scopes = [scope for scope in required_scopes if scope not in token_scopes]
        if missing_scopes:
            logger.warning(f"Token missing required scopes: {missing_scopes}")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Token does not have required scopes: {', '.join(missing_scopes)}"
            )

        logger.info(f"Token validated successfully. Scopes: {token_scopes}, Audience: {token_audiences}")
        return payload

    except ExpiredSignatureError:
        logger.warning("Token has expired")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired"
        )
    except JWTError as e:
        logger.error(f"JWT validation error: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {str(e)}"
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Token validation error: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Token validation failed: {str(e)}"
        )

# --- API Endpoints ---
@app.get("/health")
async def health_check():
    """Health check endpoint for container orchestration"""
    return {"status": "healthy"}

@app.get("/accommodations", response_model=List[Accommodation], tags=["Accommodations"])
async def list_accommodations(
    location: Optional[str] = Query(None, description="Filter by city name"),
    authorization: Optional[str] = Header(None, description="OAuth2 Bearer token with user information")
):
    """
    Get all accommodations, optionally filtered by location.

    This endpoint validates the JWT token by verifying signature and checks:
    - Token signature is valid (using JWKS)
    - Token is not expired
    - Token has required audience: http://localhost:8082/hotels/mcp
    - Token has required scope: hotels:read

    Additionally, it checks with AuthZen whether the user
    is allowed to access the `getAccommodations` resource.
    """
    # Check if Authorization header is present
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header is required"
        )

    # Extract token from Authorization header
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization header format. Expected 'Bearer <token>'"
        )

    token = authorization.replace("Bearer ", "").strip()

    # Validate JWT token
    # Checks: signature, expiration, audience, and scope
    token_payload = await validate_token(
        token=token,
        required_audience=EXPECTED_AUDIENCE,
        required_scopes=["accommodations"]
    )

    # Extract user email from token payload
    token_email = (
        token_payload.get("user_email") or           # Custom claim from AM
        token_payload.get("preferred_username") or   # Standard OIDC claim
        token_payload.get("email") or                # Standard OIDC claim
        token_payload.get("username") or             # Fallback
        token_payload.get("sub")                     # UUID fallback
    )

    if not token_email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token does not contain user identifier"
        )

    logger.info(f"User {token_email} authenticated successfully")
    logger.debug(f"Token payload: {token_payload}")

    # 🔐 Check authorization with AuthZen
    await check_authzen_access(
        subject_id=token_email,
        token=token,
        resource_type="tool",
        resource_id="getAccommodations",
        action_name="can_access",
    )

    # Return accommodations (optionally filtered by location)
    if location:
        filtered = [acc for acc in accommodations_db if acc.location.lower() == location.lower()]
        return filtered
    return accommodations_db

@app.get("/bookings", response_model=List[Booking], tags=["Bookings"])
async def get_bookings(
    authorization: Optional[str] = Header(None, description="OAuth2 Bearer token with user information")
):
    """
    Get all bookings for the authenticated user.

    This endpoint validates the JWT token by verifying signature and checks:
    - Token signature is valid (using JWKS)
    - Token is not expired
    - Token has required audience: http://localhost:8082/hotels/mcp
    - Token has required scope: bookings:read

    Additionally, it checks with AuthZen whether the user
    is allowed to access the `get_bookings` resource.
    """
    # Check if Authorization header is present
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header is required"
        )

    # Extract token from Authorization header
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization header format. Expected 'Bearer <token>'"
        )

    token = authorization.replace("Bearer ", "").strip()

    # Validate JWT token
    # Checks: signature, expiration, audience, and scope
    token_payload = await validate_token(
        token=token,
        required_audience=EXPECTED_AUDIENCE,
        required_scopes=["bookings"]
    )

    # Extract user email from token payload
    token_email = (
        token_payload.get("user_email") or           # Custom claim from AM
        token_payload.get("preferred_username") or   # Standard OIDC claim
        token_payload.get("email") or                # Standard OIDC claim
        token_payload.get("username") or             # Fallback
        token_payload.get("sub")                     # UUID fallback
    )

    if not token_email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token does not contain user identifier"
        )

    logger.info(f"User {token_email} authenticated successfully")
    logger.debug(f"Token payload: {token_payload}")

    # 🔐 NEW: Check authorization with AuthZen
    await check_authzen_access(
        subject_id=token_email,
        token=token,
        resource_type="tool",
        resource_id="getBookings",
        action_name="can_access",
    )

    # Filter bookings by user email from token
    user_bookings = [b for b in bookings_db if b.user_email == token_email]
    return user_bookings

@app.post("/bookings", response_model=Booking, tags=["Bookings"], status_code=status.HTTP_201_CREATED)
async def create_booking(
        hotel_name: Optional[str] = Query(None),
        location: Optional[str] = Query(None),
        start_date: datetime = Query(...),
        end_date: datetime = Query(...),
        authorization: Optional[str] = Header(None),
):
    booking_data = BookingCreate(
        hotel_name=hotel_name,
        location=location,
        start_date=start_date,
        end_date=end_date,
    )
    """
    Create a new booking for the authenticated user.

    This endpoint validates the JWT token by verifying signature and checks:
    - Token signature is valid (using JWKS)
    - Token is not expired
    - Token has required audience: http://localhost:8082/hotels/mcp
    - Token has required scope: bookings

    Additionally, it checks with AuthZen whether the user
    is allowed to access the `makeBooking` resource.
    """
    logger.info("--- Starting booking creation process ---")
    # Check if Authorization header is present
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header is required"
        )

    # Extract token from Authorization header
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization header format. Expected 'Bearer <token>'"
        )

    token = authorization.replace("Bearer ", "").strip()

    # Validate JWT token
    # Checks: signature, expiration, audience, and scope
    token_payload = await validate_token(
        token=token,
        required_audience=EXPECTED_AUDIENCE,
        required_scopes=["bookings"]
    )

    # Extract user email from token payload
    token_email = (
        token_payload.get("user_email") or           # Custom claim from AM
        token_payload.get("preferred_username") or   # Standard OIDC claim
        token_payload.get("email") or                # Standard OIDC claim
        token_payload.get("username") or             # Fallback
        token_payload.get("sub")                     # UUID fallback
    )

    if not token_email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token does not contain user identifier"
        )

    logger.info(f"User '{token_email}' creating booking.")
    logger.debug(f"Token payload: {token_payload}")

    # 🔐 Check authorization with AuthZen
    await check_authzen_access(
        subject_id=token_email,
        token=token,
        resource_type="tool",
        resource_id="makeBooking",
        action_name="can_access",
    )

    # Determine hotel - either by name or by location
    if booking_data.hotel_name:
        hotel = next((h for h in accommodations_db if h.name.lower() == booking_data.hotel_name.lower()), None)
        if not hotel:
            logger.error(f"Hotel '{booking_data.hotel_name}' not found.")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Hotel '{booking_data.hotel_name}' not found"
            )
    elif booking_data.location:
        hotels_in_location = [h for h in accommodations_db if h.location.lower() == booking_data.location.lower()]
        if not hotels_in_location:
            logger.error(f"No hotels found in '{booking_data.location}'.")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No hotels found in '{booking_data.location}'"
            )
        hotel = hotels_in_location[0]
        logger.info(f"Auto-selected hotel '{hotel.name}' in {booking_data.location}")
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Either 'hotel_name' or 'location' must be provided"
        )
    
    logger.debug(f"Selected hotel: {hotel.dict()}")

    # Validate dates
    if booking_data.end_date <= booking_data.start_date:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="End date must be after start date"
        )

    # Calculate price
    nights = (booking_data.end_date - booking_data.start_date).days
    total_price = nights * hotel.price_per_night
    logger.debug(f"Calculated price: {total_price} for {nights} nights.")

    # Generate new booking ID
    new_id = max([b.id for b in bookings_db], default=0) + 1
    logger.debug(f"Generated new booking ID: {new_id}")

    # Assign room number (simple increment)
    existing_rooms = [b.room_number for b in bookings_db if b.hotel_name.lower() == hotel.name.lower()]
    room_num = len(existing_rooms) + 1
    room_number = f"{room_num:03d}"
    logger.debug(f"Assigned room number: {room_number}")

    # Create new booking
    new_booking = Booking(
        id=new_id,
        user_email=token_email,
        hotel_name=hotel.name,
        room_number=room_number,
        start_date=booking_data.start_date,
        end_date=booking_data.end_date,
        price=total_price
    )

    bookings_db.append(new_booking)
    logger.info(f"Appended new booking to in-memory list. New DB size: {len(bookings_db)}")

    save_bookings(bookings_db)

    logger.info(f"--- ✓ Booking creation process finished successfully for ID: {new_id} ---")

    return new_booking


async def check_authzen_access(
    subject_id: str,
    token: str,
    resource_type: str,
    resource_id: str,
    action_name: str,
) -> None:
    """
    Calls AuthZen PDP to check if `subject_id` is allowed to perform `action_name`
    on `resource_type`/`resource_id`.

    Raises HTTPException(403) if denied, 503 if PDP is unavailable.
    """
    request_id = str(uuid.uuid4())

    payload = {
        "subject": {
            "type": "user",
            "id": subject_id,
        },
        "resource": {
            "type": resource_type,
            "id": resource_id,
        },
        "action": {
            "name": action_name,
        },
    }

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Request-ID": request_id,
    }
    logger.info(payload)

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(AUTHZEN_PDP_URL, json=payload, headers=headers)
            logger.info(resp)
    except httpx.RequestError as exc:
        logger.error(f"AuthZen PDP request error: {exc}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authorization service unavailable",
        )

    if resp.status_code == 401:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication failed at authorization service",
        )

    if resp.status_code == 400:
        logger.error(f"AuthZen PDP returned 400: {resp.text}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Invalid authorization request",
        )

    if resp.status_code >= 500:
        logger.error(f"AuthZen PDP error {resp.status_code}: {resp.text}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authorization service error",
        )

    data = resp.json()
    decision = data.get("decision")

    logger.info(f"AuthZen decision for {subject_id}: {decision}, context={data.get('context')}")

    if decision is not True:
        # You can also pass data["context"] in detail if you want more info
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User doesn't has access to this resource.",
        )




def main():
    """Main entry point for Hotel Booking API."""
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

if __name__ == "__main__":
    main()
