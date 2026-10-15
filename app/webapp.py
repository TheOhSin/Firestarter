import hashlib
import hmac
import logging
import os
import re
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import aiosqlite
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from fireshare import FireshareClient, FireshareError


logger = logging.getLogger("firestarter.web")

BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

APP_NAME = os.environ.get("APP_NAME", "Firestarter")
APP_VERSION = os.environ.get("APP_VERSION", "dev")
PUBLIC_SERVICE_NAME = os.environ.get(
    "PUBLIC_SERVICE_NAME",
    "Fireshare",
)

DISCORD_GUILD_ID = int(
    os.environ.get("DISCORD_GUILD_ID", "0")
)

ONBOARDING_ROLE_ID = int(
    os.environ.get("ONBOARDING_ROLE_ID", "0")
)

PUBLIC_FIRESHARE_URL = os.environ.get(
    "PUBLIC_FIRESHARE_URL",
    os.environ.get("FIRESHARE_URL", ""),
).rstrip("/")

SESSION_SECRET_PATH = Path(
    os.environ.get(
        "WEB_SESSION_SECRET_PATH",
        "/data/.web-session-secret",
    )
)

SECURE_COOKIES = (
    os.environ.get(
        "WEB_SECURE_COOKIES",
        "false",
    ).lower()
    == "true"
)

USERNAME_PATTERN = re.compile(
    r"^[A-Za-z0-9_.-]{3,32}$"
)


class AttemptLimiter:
    def __init__(
        self,
        max_attempts=5,
        window_seconds=300,
    ):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.failures = {}

    def _prune(self, key):
        now = time.monotonic()

        values = [
            ts
            for ts in self.failures.get(key, [])
            if now - ts < self.window_seconds
        ]

        if values:
            self.failures[key] = values
        else:
            self.failures.pop(key, None)

        return values

    def blocked(self, key):
        return (
            len(self._prune(key))
            >= self.max_attempts
        )

    def fail(self, key):
        values = self._prune(key)
        values.append(time.monotonic())
        self.failures[key] = values

    def clear(self, key):
        self.failures.pop(key, None)


limiter = AttemptLimiter()


def generate_claim_code():
    alphabet = (
        "ABCDEFGHJKLMNPQRSTUVWXYZ"
        "23456789"
    )

    raw = "".join(
        secrets.choice(alphabet)
        for _ in range(12)
    )

    return "-".join(
        raw[i:i + 4]
        for i in range(0, 12, 4)
    )


def normalize_claim_code(value):
    return (
        value.replace("-", "")
        .replace(" ", "")
        .upper()
    )


def load_or_create_session_secret():
    SESSION_SECRET_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    try:
        fd = os.open(
            SESSION_SECRET_PATH,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL,
            0o600,
        )

        secret = secrets.token_hex(32)

        with os.fdopen(fd, "w") as handle:
            handle.write(secret)

        return secret

    except FileExistsError:
        return SESSION_SECRET_PATH.read_text().strip()


def hash_password(password, salt=None):
    if salt is None:
        salt_bytes = os.urandom(16)
    else:
        salt_bytes = bytes.fromhex(salt)

    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt_bytes,
        n=2**14,
        r=8,
        p=1,
        dklen=32,
    )

    return (
        salt_bytes.hex(),
        digest.hex(),
    )


def verify_password(
    password,
    salt,
    expected_hash,
):
    _, calculated = hash_password(
        password,
        salt=salt,
    )

    return hmac.compare_digest(
        calculated,
        expected_hash,
    )


async def ensure_web_schema(db_path):
    async with aiosqlite.connect(
        db_path
    ) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS web_admin (
                id INTEGER PRIMARY KEY
                    CHECK (id = 1),

                username TEXT NOT NULL UNIQUE,

                password_hash TEXT NOT NULL,

                password_salt TEXT NOT NULL,

                created_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.commit()


async def get_web_admin(db_path):
    async with aiosqlite.connect(
        db_path
    ) as db:
        cursor = await db.execute("""
            SELECT
                username,
                password_hash,
                password_salt,
                created_at
            FROM web_admin
            WHERE id = 1
        """)

        return await cursor.fetchone()



async def update_web_admin_password(
    db_path,
    password_hash,
    password_salt,
):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            UPDATE web_admin
            SET
                password_hash = ?,
                password_salt = ?
            WHERE id = 1
        """, (
            password_hash,
            password_salt,
        ))

        await db.commit()


async def create_web_admin(
    db_path,
    username,
    password_hash,
    password_salt,
):
    try:
        async with aiosqlite.connect(
            db_path
        ) as db:
            await db.execute("""
                INSERT INTO web_admin (
                    id,
                    username,
                    password_hash,
                    password_salt
                )
                VALUES (
                    1,
                    ?,
                    ?,
                    ?
                )
            """, (
                username,
                password_hash,
                password_salt,
            ))

            await db.commit()

        return True

    except sqlite3.IntegrityError:
        return False


async def local_stats(db_path):
    async with aiosqlite.connect(
        db_path
    ) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM users"
        )

        linked = (
            await cursor.fetchone()
        )[0]

        cursor = await db.execute(
            """
            SELECT COUNT(*)
            FROM onboarding
            """
        )

        tracked = (
            await cursor.fetchone()
        )[0]

        cursor = await db.execute(
            """
            SELECT COUNT(*)
            FROM onboarding
            WHERE last_prompt_status = 'sent'
            """
        )

        prompted = (
            await cursor.fetchone()
        )[0]

    return {
        "linked": linked,
        "tracked": tracked,
        "prompted": prompted,
    }


def create_web_app(
    discord_client,
    database,
    fireshare,
    config_store,
):
    state = {
        "claim_code": None,
    }

    @asynccontextmanager
    async def lifespan(app):
        await ensure_web_schema(
            database.path
        )

        admin = await get_web_admin(
            database.path
        )

        if admin is None:
            state["claim_code"] = (
                generate_claim_code()
            )

            logger.warning(
                "=================================================="
            )
            logger.warning(
                "FIRESTARTER FIRST-RUN CLAIM CODE: %s",
                state["claim_code"],
            )
            logger.warning(
                "Use this code once in the Firestarter WebUI."
            )
            logger.warning(
                "Do not share this code."
            )
            logger.warning(
                "=================================================="
            )

        yield

    app = FastAPI(
        title="Firestarter",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    session_secret = (
        load_or_create_session_secret()
    )

    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret,
        session_cookie=(
            "firestarter_session"
        ),
        max_age=43200,
        same_site="lax",
        https_only=SECURE_COOKIES,
    )

    app.mount(
        "/static",
        StaticFiles(
            directory=str(STATIC_DIR)
        ),
        name="static",
    )

    templates = Jinja2Templates(
        directory=str(TEMPLATE_DIR)
    )

    @app.middleware("http")
    async def security_headers(
        request,
        call_next,
    ):
        response = await call_next(
            request
        )

        response.headers[
            "X-Content-Type-Options"
        ] = "nosniff"

        response.headers[
            "X-Frame-Options"
        ] = "DENY"

        response.headers[
            "Referrer-Policy"
        ] = "no-referrer"

        response.headers[
            "Permissions-Policy"
        ] = (
            "camera=(), microphone=(), "
            "geolocation=()"
        )

        response.headers[
            "Content-Security-Policy"
        ] = (
            "default-src 'self'; "
            "style-src 'self'; "
            "img-src 'self' data:; "
            "script-src 'self'; "
            "frame-ancestors 'none'; "
            "form-action 'self'"
        )

        if request.url.path != "/static":
            response.headers[
                "Cache-Control"
            ] = "no-store"

        return response

    def client_address(request):
        if request.client:
            return request.client.host

        return "unknown"

    def authenticated(request):
        return (
            request.session.get(
                "authenticated"
            )
            is True
        )

    def csrf_token(request):
        token = request.session.get(
            "csrf_token"
        )

        if not token:
            token = secrets.token_urlsafe(
                32
            )

            request.session[
                "csrf_token"
            ] = token

        return token

    def valid_csrf(
        request,
        supplied,
    ):
        expected = request.session.get(
            "csrf_token",
            "",
        )

        return (
            bool(expected)
            and bool(supplied)
            and hmac.compare_digest(
                expected,
                supplied,
            )
        )

    def template_context(
        request,
        **extra,
    ):
        context = {
            "request": request,
            "app_name": APP_NAME,
            "app_version": APP_VERSION,
            "service_name": (
                PUBLIC_SERVICE_NAME
            ),
            "authenticated": (
                authenticated(request)
            ),
            "csrf_token": (
                csrf_token(request)
            ),
        }

        context.update(extra)
        return context

    async def require_auth(request):
        if not authenticated(request):
            return RedirectResponse(
                "/login",
                status_code=303,
            )

        return None

    @app.get(
        "/healthz",
        response_class=JSONResponse,
    )
    async def healthz():
        admin = await get_web_admin(
            database.path
        )

        return {
            "status": "ok",
            "claimed": admin is not None,
            "discord_ready": (
                discord_client.is_ready()
            ),
        }

    @app.get("/")
    async def index(
        request: Request,
    ):
        admin = await get_web_admin(
            database.path
        )

        if admin is None:
            return RedirectResponse(
                "/claim",
                status_code=303,
            )

        if authenticated(request):
            return RedirectResponse(
                "/dashboard",
                status_code=303,
            )

        return RedirectResponse(
            "/login",
            status_code=303,
        )

    @app.get(
        "/claim",
        response_class=HTMLResponse,
    )
    async def claim_page(
        request: Request,
    ):
        admin = await get_web_admin(
            database.path
        )

        if admin is not None:
            return RedirectResponse(
                "/login",
                status_code=303,
            )

        return templates.TemplateResponse(
            request=request,
            name="claim.html",
            context=template_context(
                request,
                error=None,
            ),
        )

    @app.post(
        "/claim",
        response_class=HTMLResponse,
    )
    async def claim_submit(
        request: Request,
    ):
        admin = await get_web_admin(
            database.path
        )

        if admin is not None:
            return RedirectResponse(
                "/login",
                status_code=303,
            )

        form = await request.form()

        csrf = str(
            form.get(
                "csrf_token",
                "",
            )
        )

        claim_code = str(
            form.get(
                "claim_code",
                "",
            )
        )

        username = str(
            form.get(
                "username",
                "",
            )
        ).strip()

        password = str(
            form.get(
                "password",
                "",
            )
        )

        confirm = str(
            form.get(
                "confirm_password",
                "",
            )
        )

        key = (
            "claim:"
            + client_address(request)
        )

        error = None

        if limiter.blocked(key):
            error = (
                "Too many failed attempts. "
                "Wait a few minutes and try again."
            )

        elif not valid_csrf(
            request,
            csrf,
        ):
            error = (
                "Your session expired. "
                "Reload the page and try again."
            )

        elif not state["claim_code"]:
            error = (
                "No active claim code exists. "
                "Restart Firestarter to generate one."
            )

        elif not hmac.compare_digest(
            normalize_claim_code(
                claim_code
            ),
            normalize_claim_code(
                state["claim_code"]
            ),
        ):
            limiter.fail(key)
            error = "Invalid claim code."

        elif not USERNAME_PATTERN.fullmatch(
            username
        ):
            error = (
                "Admin username must be "
                "3-32 characters and use only "
                "letters, numbers, _, -, or ."
            )

        elif len(password) < 8:
            error = (
                "Admin password must be "
                "at least 8 characters."
            )

        elif password != confirm:
            error = (
                "The passwords do not match."
            )

        if error:
            return templates.TemplateResponse(
                request=request,
                name="claim.html",
                context=template_context(
                    request,
                    error=error,
                ),
                status_code=400,
            )

        salt, password_hash = (
            hash_password(password)
        )

        created = await create_web_admin(
            database.path,
            username,
            password_hash,
            salt,
        )

        if not created:
            return RedirectResponse(
                "/login",
                status_code=303,
            )

        state["claim_code"] = None
        limiter.clear(key)

        request.session.clear()

        request.session[
            "authenticated"
        ] = True

        request.session[
            "username"
        ] = username

        request.session[
            "csrf_token"
        ] = secrets.token_urlsafe(32)

        logger.info(
            "WebUI claimed username=%s source=%s",
            username,
            client_address(request),
        )

        return RedirectResponse(
            "/dashboard",
            status_code=303,
        )

    @app.get(
        "/login",
        response_class=HTMLResponse,
    )
    async def login_page(
        request: Request,
    ):
        admin = await get_web_admin(
            database.path
        )

        if admin is None:
            return RedirectResponse(
                "/claim",
                status_code=303,
            )

        if authenticated(request):
            return RedirectResponse(
                "/dashboard",
                status_code=303,
            )

        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context=template_context(
                request,
                error=None,
            ),
        )

    @app.post(
        "/login",
        response_class=HTMLResponse,
    )
    async def login_submit(
        request: Request,
    ):
        form = await request.form()

        csrf = str(
            form.get(
                "csrf_token",
                "",
            )
        )

        username = str(
            form.get(
                "username",
                "",
            )
        ).strip()

        password = str(
            form.get(
                "password",
                "",
            )
        )

        key = (
            "login:"
            + client_address(request)
        )

        error = None

        if limiter.blocked(key):
            error = (
                "Too many failed attempts. "
                "Wait a few minutes and try again."
            )

        elif not valid_csrf(
            request,
            csrf,
        ):
            error = (
                "Your session expired. "
                "Reload the page and try again."
            )

        else:
            admin = await get_web_admin(
                database.path
            )

            valid = False

            if admin:
                valid = (
                    hmac.compare_digest(
                        username,
                        admin[0],
                    )
                    and verify_password(
                        password,
                        admin[2],
                        admin[1],
                    )
                )

            if not valid:
                limiter.fail(key)
                error = (
                    "Invalid username or password."
                )

        if error:
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context=template_context(
                    request,
                    error=error,
                ),
                status_code=400,
            )

        limiter.clear(key)

        request.session.clear()

        request.session[
            "authenticated"
        ] = True

        request.session[
            "username"
        ] = username

        request.session[
            "csrf_token"
        ] = secrets.token_urlsafe(32)

        logger.info(
            "WebUI login username=%s source=%s",
            username,
            client_address(request),
        )

        return RedirectResponse(
            "/dashboard",
            status_code=303,
        )

    @app.post("/logout")
    async def logout(
        request: Request,
    ):
        form = await request.form()

        if valid_csrf(
            request,
            str(
                form.get(
                    "csrf_token",
                    "",
                )
            ),
        ):
            request.session.clear()

        return RedirectResponse(
            "/login",
            status_code=303,
        )

    @app.get(
        "/dashboard",
        response_class=HTMLResponse,
    )
    async def dashboard(
        request: Request,
    ):
        redirect = await require_auth(
            request
        )

        if redirect:
            return redirect

        stats = await local_stats(
            database.path
        )

        guild = discord_client.get_guild(
            DISCORD_GUILD_ID
        )

        guild_name = (
            guild.name
            if guild
            else "Unavailable"
        )

        role_name = "Unavailable"

        if guild:
            role = guild.get_role(
                ONBOARDING_ROLE_ID
            )

            if role:
                role_name = role.name

        discord_ready = (
            discord_client.is_ready()
        )

        discord_bot_name = (
            str(discord_client.user)
            if discord_client.user
            else "Connecting"
        )

        fireshare_ready = False
        fireshare_users = None
        fireshare_disabled = None

        try:
            remote_users = (
                await fireshare.list_users()
            )

            fireshare_ready = True

            fireshare_users = len(
                remote_users
            )

            fireshare_disabled = sum(
                1
                for user in remote_users
                if user.get(
                    "disabled",
                    False,
                )
            )

        except Exception as exc:
            logger.warning(
                "Dashboard Fireshare check failed: %s",
                type(exc).__name__,
            )

        recent_rows = await database.list_onboarding()

        recent_activity = []

        for row in recent_rows[:5]:
            (
                discord_id,
                discord_username,
                fireshare_username,
                generation,
                last_role_added_at,
                last_prompt_sent_at,
                last_prompt_status,
                registration_state,
            ) = row

            display_name = (
                discord_username
                or f"Discord {discord_id}"
            )

            if registration_state == "registered":
                activity_label = "Registered"
                activity_kind = "good"

            elif last_prompt_status == "dm_blocked":
                activity_label = "DM blocked"
                activity_kind = "bad"

            elif registration_state == "prompt_pending":
                activity_label = "Prompt pending"
                activity_kind = "neutral"

            elif registration_state == "prompted":
                activity_label = "Prompt sent"
                activity_kind = "neutral"

            else:
                activity_label = (
                    registration_state
                    or "Tracked"
                )
                activity_kind = "neutral"

            activity_time = (
                last_prompt_sent_at
                or last_role_added_at
                or ""
            )

            recent_activity.append({
                "discord_id": discord_id,
                "discord_username": display_name,
                "fireshare_username": (
                    fireshare_username or None
                ),
                "label": activity_label,
                "kind": activity_kind,
                "time": activity_time,
                "generation": generation,
            })


        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context=template_context(
                request,
                recent_activity=recent_activity,
                username=request.session.get(
                    "username",
                    "",
                ),
                stats=stats,
                discord_ready=discord_ready,
                discord_bot_name=(
                    discord_bot_name
                ),
                guild_name=guild_name,
                role_name=role_name,
                fireshare_ready=(
                    fireshare_ready
                ),
                fireshare_users=(
                    fireshare_users
                ),
                fireshare_disabled=(
                    fireshare_disabled
                ),
                fireshare_url=(
                    PUBLIC_FIRESHARE_URL
                ),
            ),
        )


    @app.get(
        "/settings/discord",
        response_class=HTMLResponse,
    )
    async def discord_settings(
        request: Request,
    ):
        redirect = await require_auth(
            request
        )

        if redirect:
            return redirect

        saved_guild_id = await config_store.get(
            "discord_guild_id",
            str(DISCORD_GUILD_ID),
        )

        saved_role_id = await config_store.get(
            "onboarding_role_id",
            str(ONBOARDING_ROLE_ID),
        )

        requested_guild_id = (
            request.query_params.get(
                "guild_id"
            )
        )

        selected_guild_id = (
            requested_guild_id
            or saved_guild_id
            or ""
        )

        guilds = sorted(
            discord_client.guilds,
            key=lambda guild: guild.name.casefold(),
        )

        selected_guild = None

        try:
            selected_id_int = int(
                selected_guild_id
            )

            selected_guild = (
                discord_client.get_guild(
                    selected_id_int
                )
            )

        except (TypeError, ValueError):
            selected_guild = None

        roles = []

        if selected_guild:
            roles = sorted(
                [
                    role
                    for role
                    in selected_guild.roles
                    if not role.is_default()
                ],
                key=lambda role: (
                    -role.position,
                    role.name.casefold(),
                ),
            )

        settings = (
            await config_store.get_public_settings()
        )

        token_configured = (
            settings.get(
                "discord_token",
                {}
            ).get(
                "configured",
                False,
            )
        )

        saved = (
            request.query_params.get(
                "saved"
            )
            == "1"
        )

        test_result = (
            request.query_params.get(
                "test"
            )
        )

        return templates.TemplateResponse(
            request=request,
            name="discord_settings.html",
            context=template_context(
                request,
                guilds=guilds,
                selected_guild=selected_guild,
                selected_guild_id=str(
                    selected_guild_id
                ),
                selected_role_id=str(
                    saved_role_id or ""
                ),
                roles=roles,
                token_configured=(
                    token_configured
                ),
                discord_ready=(
                    discord_client.is_ready()
                ),
                discord_bot_name=(
                    str(discord_client.user)
                    if discord_client.user
                    else "Not connected"
                ),
                saved=saved,
                test_result=test_result,
            ),
        )


    @app.post(
        "/settings/discord/save",
    )
    async def discord_settings_save(
        request: Request,
    ):
        redirect = await require_auth(
            request
        )

        if redirect:
            return redirect

        form = await request.form()

        if not valid_csrf(
            request,
            str(
                form.get(
                    "csrf_token",
                    "",
                )
            ),
        ):
            return HTMLResponse(
                "Invalid or expired CSRF token.",
                status_code=403,
            )

        guild_id_raw = str(
            form.get(
                "guild_id",
                "",
            )
        )

        role_id_raw = str(
            form.get(
                "role_id",
                "",
            )
        )

        try:
            guild_id = int(
                guild_id_raw
            )

            role_id = int(
                role_id_raw
            )

        except ValueError:
            return HTMLResponse(
                "Invalid Discord server or role.",
                status_code=400,
            )

        guild = discord_client.get_guild(
            guild_id
        )

        if guild is None:
            return HTMLResponse(
                "Firestarter cannot access that Discord server.",
                status_code=400,
            )

        role = guild.get_role(
            role_id
        )

        if (
            role is None
            or role.is_default()
        ):
            return HTMLResponse(
                "That Discord role is not valid.",
                status_code=400,
            )

        await config_store.set(
            "discord_guild_id",
            str(guild_id),
            secret=False,
        )

        await config_store.set(
            "onboarding_role_id",
            str(role_id),
            secret=False,
        )

        logger.info(
            "Discord settings saved "
            "guild_id=%s role_id=%s",
            guild_id,
            role_id,
        )

        return RedirectResponse(
            (
                "/settings/discord"
                f"?guild_id={guild_id}"
                "&saved=1"
            ),
            status_code=303,
        )


    @app.post(
        "/settings/discord/test",
    )
    async def discord_settings_test(
        request: Request,
    ):
        redirect = await require_auth(
            request
        )

        if redirect:
            return redirect

        form = await request.form()

        if not valid_csrf(
            request,
            str(
                form.get(
                    "csrf_token",
                    "",
                )
            ),
        ):
            return HTMLResponse(
                "Invalid or expired CSRF token.",
                status_code=403,
            )

        guild_id_raw = str(
            form.get(
                "guild_id",
                "",
            )
        )

        role_id_raw = str(
            form.get(
                "role_id",
                "",
            )
        )

        result = "fail"

        try:
            guild_id = int(
                guild_id_raw
            )

            role_id = int(
                role_id_raw
            )

            guild = discord_client.get_guild(
                guild_id
            )

            role = (
                guild.get_role(role_id)
                if guild
                else None
            )

            if (
                discord_client.is_ready()
                and guild is not None
                and role is not None
                and not role.is_default()
            ):
                result = "ok"

        except ValueError:
            pass

        return RedirectResponse(
            (
                "/settings/discord"
                f"?guild_id={guild_id_raw}"
                f"&test={result}"
            ),
            status_code=303,
        )



    def valid_http_url(value):
        try:
            parsed = urlparse(value)

            return (
                parsed.scheme in ("http", "https")
                and bool(parsed.netloc)
                and parsed.username is None
                and parsed.password is None
            )

        except Exception:
            return False


    async def test_fireshare_connection(
        base_url,
        username,
        password,
    ):
        client = FireshareClient(
            base_url,
            username,
            password,
        )

        try:
            await client.start()

            users = await client.list_users()

            return {
                "ok": True,
                "user_count": len(users),
            }

        except FireshareError as exc:
            logger.warning(
                "Fireshare settings test failed "
                "url=%s username=%s error=%s",
                base_url,
                username,
                type(exc).__name__,
            )

            return {
                "ok": False,
                "error": str(exc),
            }

        except Exception as exc:
            logger.warning(
                "Unexpected Fireshare settings "
                "test failure url=%s username=%s error=%s",
                base_url,
                username,
                type(exc).__name__,
            )

            return {
                "ok": False,
                "error": (
                    "Firestarter could not connect "
                    "to that Fireshare instance."
                ),
            }

        finally:
            await client.close()


    @app.get(
        "/settings/fireshare",
        response_class=HTMLResponse,
    )
    async def fireshare_settings(
        request: Request,
    ):
        redirect = await require_auth(
            request
        )

        if redirect:
            return redirect

        settings = (
            await config_store.get_public_settings()
        )

        fireshare_url = await config_store.get(
            "fireshare_url",
            PUBLIC_FIRESHARE_URL,
        )

        public_url = await config_store.get(
            "public_fireshare_url",
            fireshare_url,
        )

        username = await config_store.get(
            "fireshare_username",
            "",
        )

        password_configured = (
            settings.get(
                "fireshare_password",
                {}
            ).get(
                "configured",
                False,
            )
        )

        saved = (
            request.query_params.get("saved")
            == "1"
        )

        test_result = request.query_params.get(
            "test"
        )

        test_users = request.query_params.get(
            "users"
        )

        return templates.TemplateResponse(
            request=request,
            name="fireshare_settings.html",
            context=template_context(
                request,
                fireshare_url=fireshare_url or "",
                public_url=public_url or "",
                fireshare_username=username or "",
                password_configured=(
                    password_configured
                ),
                saved=saved,
                test_result=test_result,
                test_users=test_users,
            ),
        )


    @app.post(
        "/settings/fireshare/test",
        response_class=HTMLResponse,
    )
    async def fireshare_settings_test(
        request: Request,
    ):
        redirect = await require_auth(
            request
        )

        if redirect:
            return redirect

        form = await request.form()

        if not valid_csrf(
            request,
            str(
                form.get(
                    "csrf_token",
                    "",
                )
            ),
        ):
            return HTMLResponse(
                "Invalid or expired CSRF token.",
                status_code=403,
            )

        base_url = str(
            form.get(
                "fireshare_url",
                "",
            )
        ).strip().rstrip("/")

        public_url = str(
            form.get(
                "public_fireshare_url",
                "",
            )
        ).strip().rstrip("/")

        username = str(
            form.get(
                "fireshare_username",
                "",
            )
        ).strip()

        password = str(
            form.get(
                "fireshare_password",
                "",
            )
        )

        if not valid_http_url(base_url):
            return templates.TemplateResponse(
                request=request,
                name="fireshare_settings.html",
                context=template_context(
                    request,
                    fireshare_url=base_url,
                    public_url=public_url,
                    fireshare_username=username,
                    password_configured=bool(
                        await config_store.get(
                            "fireshare_password",
                            "",
                        )
                    ),
                    saved=False,
                    test_result="invalid_url",
                    test_users=None,
                ),
                status_code=400,
            )

        if public_url and not valid_http_url(
            public_url
        ):
            return templates.TemplateResponse(
                request=request,
                name="fireshare_settings.html",
                context=template_context(
                    request,
                    fireshare_url=base_url,
                    public_url=public_url,
                    fireshare_username=username,
                    password_configured=bool(
                        await config_store.get(
                            "fireshare_password",
                            "",
                        )
                    ),
                    saved=False,
                    test_result="invalid_public_url",
                    test_users=None,
                ),
                status_code=400,
            )

        if not username:
            return templates.TemplateResponse(
                request=request,
                name="fireshare_settings.html",
                context=template_context(
                    request,
                    fireshare_url=base_url,
                    public_url=public_url,
                    fireshare_username=username,
                    password_configured=bool(
                        await config_store.get(
                            "fireshare_password",
                            "",
                        )
                    ),
                    saved=False,
                    test_result="missing_username",
                    test_users=None,
                ),
                status_code=400,
            )

        if not password:
            password = await config_store.get(
                "fireshare_password",
                "",
            )

        if not password:
            return templates.TemplateResponse(
                request=request,
                name="fireshare_settings.html",
                context=template_context(
                    request,
                    fireshare_url=base_url,
                    public_url=public_url,
                    fireshare_username=username,
                    password_configured=False,
                    saved=False,
                    test_result="missing_password",
                    test_users=None,
                ),
                status_code=400,
            )

        result = await test_fireshare_connection(
            base_url,
            username,
            password,
        )

        return templates.TemplateResponse(
            request=request,
            name="fireshare_settings.html",
            context=template_context(
                request,
                fireshare_url=base_url,
                public_url=(
                    public_url
                    or base_url
                ),
                fireshare_username=username,
                password_configured=True,
                saved=False,
                test_result=(
                    "ok"
                    if result["ok"]
                    else "fail"
                ),
                test_users=(
                    result.get(
                        "user_count"
                    )
                ),
            ),
        )


    @app.post(
        "/settings/fireshare/save",
        response_class=HTMLResponse,
    )
    async def fireshare_settings_save(
        request: Request,
    ):
        redirect = await require_auth(
            request
        )

        if redirect:
            return redirect

        form = await request.form()

        if not valid_csrf(
            request,
            str(
                form.get(
                    "csrf_token",
                    "",
                )
            ),
        ):
            return HTMLResponse(
                "Invalid or expired CSRF token.",
                status_code=403,
            )

        base_url = str(
            form.get(
                "fireshare_url",
                "",
            )
        ).strip().rstrip("/")

        public_url = str(
            form.get(
                "public_fireshare_url",
                "",
            )
        ).strip().rstrip("/")

        username = str(
            form.get(
                "fireshare_username",
                "",
            )
        ).strip()

        password = str(
            form.get(
                "fireshare_password",
                "",
            )
        )

        if not valid_http_url(base_url):
            return HTMLResponse(
                "Invalid Fireshare URL.",
                status_code=400,
            )

        if not public_url:
            public_url = base_url

        if not valid_http_url(public_url):
            return HTMLResponse(
                "Invalid public Fireshare URL.",
                status_code=400,
            )

        if not username:
            return HTMLResponse(
                "Fireshare username is required.",
                status_code=400,
            )

        existing_password = (
            await config_store.get(
                "fireshare_password",
                "",
            )
        )

        effective_password = (
            password
            if password
            else existing_password
        )

        if not effective_password:
            return HTMLResponse(
                "Fireshare password is required.",
                status_code=400,
            )

        # Do not save unverified credentials.
        result = await test_fireshare_connection(
            base_url,
            username,
            effective_password,
        )

        if not result["ok"]:
            return templates.TemplateResponse(
                request=request,
                name="fireshare_settings.html",
                context=template_context(
                    request,
                    fireshare_url=base_url,
                    public_url=public_url,
                    fireshare_username=username,
                    password_configured=bool(
                        existing_password
                    ),
                    saved=False,
                    test_result="fail",
                    test_users=None,
                ),
                status_code=400,
            )

        await config_store.set(
            "fireshare_url",
            base_url,
            secret=False,
        )

        await config_store.set(
            "public_fireshare_url",
            public_url,
            secret=False,
        )

        await config_store.set(
            "fireshare_username",
            username,
            secret=False,
        )

        if password:
            await config_store.set(
                "fireshare_password",
                password,
                secret=True,
            )

        logger.info(
            "Fireshare settings saved "
            "url=%s username=%s",
            base_url,
            username,
        )

        return RedirectResponse(
            "/settings/fireshare?saved=1&test=ok"
            f"&users={result['user_count']}",
            status_code=303,
        )




    async def build_accounts_view():
        rows = await database.list_linked_users()

        live_users = []
        fireshare_error = None

        try:
            live_users = await fireshare.list_users()

        except FireshareError as exc:
            fireshare_error = str(exc)

        users_by_id = {}
        users_by_name = {}

        for user in live_users:
            user_id = user.get("id")

            if user_id is not None:
                users_by_id[str(user_id)] = user

            username = str(
                user.get("username", "")
            ).strip()

            if username:
                users_by_name[
                    username.casefold()
                ] = user

        accounts = []

        for row in rows:
            (
                discord_id,
                discord_username,
                fireshare_username,
                fireshare_user_id,
                created_at,
                registration_state,
            ) = row

            live_user = None

            if fireshare_user_id is not None:
                live_user = users_by_id.get(
                    str(fireshare_user_id)
                )

            if live_user is None:
                live_user = users_by_name.get(
                    str(
                        fireshare_username
                    ).casefold()
                )

            if fireshare_error:
                status = "unavailable"

            elif live_user is None:
                status = "missing"

            elif bool(
                live_user.get(
                    "disabled",
                    False,
                )
            ):
                status = "disabled"

            else:
                status = "active"

            accounts.append({
                "discord_id": discord_id,
                "discord_username": discord_username,
                "fireshare_username": (
                    fireshare_username
                ),
                "fireshare_user_id": (
                    fireshare_user_id
                ),
                "created_at": created_at,
                "registration_state": (
                    registration_state
                ),
                "status": status,
                "live_user": live_user,
            })

        return accounts, fireshare_error


    async def render_accounts_page(
        request,
        *,
        action_message=None,
        action_error=None,
        invite_url=None,
        status_code=200,
    ):
        accounts, fireshare_error = (
            await build_accounts_view()
        )

        return templates.TemplateResponse(
            request=request,
            name="accounts.html",
            context=template_context(
                request,
                accounts=accounts,
                account_count=len(accounts),
                fireshare_error=fireshare_error,
                action_message=action_message,
                action_error=action_error,
                invite_url=invite_url,
            ),
            status_code=status_code,
        )



    @app.get(
        "/onboarding",
        response_class=HTMLResponse,
    )
    async def onboarding_page(
        request: Request,
    ):
        redirect = await require_auth(
            request
        )

        if redirect:
            return redirect

        rows = await database.list_onboarding()

        records = []

        for row in rows:
            (
                discord_id,
                discord_username,
                fireshare_username,
                generation,
                last_role_added_at,
                last_prompt_sent_at,
                last_prompt_status,
                registration_state,
            ) = row

            records.append({
                "discord_id": discord_id,
                "discord_username": (
                    discord_username
                    or "Unknown member"
                ),
                "fireshare_username": (
                    fireshare_username
                    or None
                ),
                "generation": generation,
                "last_role_added_at": (
                    last_role_added_at
                ),
                "last_prompt_sent_at": (
                    last_prompt_sent_at
                ),
                "last_prompt_status": (
                    last_prompt_status
                    or "none"
                ),
                "registration_state": (
                    registration_state
                    or "none"
                ),
            })

        counts = {
            "total": len(records),
            "registered": sum(
                1 for item in records
                if item["registration_state"]
                == "registered"
            ),
            "prompted": sum(
                1 for item in records
                if item["registration_state"]
                in (
                    "prompted",
                    "prompt_pending",
                )
            ),
            "blocked": sum(
                1 for item in records
                if item["last_prompt_status"]
                == "dm_blocked"
            ),
        }

        return templates.TemplateResponse(
            request=request,
            name="onboarding.html",
            context=template_context(
                request,
                records=records,
                counts=counts,
            ),
        )


    @app.get(
        "/accounts",
        response_class=HTMLResponse,
    )
    async def accounts_page(
        request: Request,
    ):
        redirect = await require_auth(
            request
        )

        if redirect:
            return redirect

        return await render_accounts_page(
            request,
            action_message=(
                request.query_params.get(
                    "message"
                )
            ),
        )


    @app.post(
        "/accounts/action",
        response_class=HTMLResponse,
    )
    async def accounts_action(
        request: Request,
    ):
        redirect = await require_auth(
            request
        )

        if redirect:
            return redirect

        form = await request.form()

        csrf = str(
            form.get(
                "csrf_token",
                "",
            )
        )

        if not valid_csrf(
            request,
            csrf,
        ):
            return await render_accounts_page(
                request,
                action_error=(
                    "Your session expired. "
                    "Reload the page and try again."
                ),
                status_code=400,
            )

        try:
            discord_id = int(
                str(
                    form.get(
                        "discord_id",
                        "",
                    )
                )
            )

        except ValueError:
            return await render_accounts_page(
                request,
                action_error=(
                    "Invalid Discord account ID."
                ),
                status_code=400,
            )

        action = str(
            form.get(
                "action",
                "",
            )
        ).strip().lower()

        mapping = (
            await database.get_by_discord_id(
                discord_id
            )
        )

        if not mapping:
            return await render_accounts_page(
                request,
                action_error=(
                    "That Firestarter mapping "
                    "no longer exists."
                ),
                status_code=404,
            )

        fireshare_username = mapping[2]
        fireshare_user_id = mapping[3]

        # Unlink is intentionally local-only.
        # It does not delete the Fireshare account.
        if action == "unlink":
            await database.delete_by_discord_id(
                discord_id
            )

            logger.info(
                "WebUI account mapping unlinked "
                "discord_id=%s fireshare_username=%s",
                discord_id,
                fireshare_username,
            )

            return RedirectResponse(
                "/accounts?message=Account+mapping+unlinked.",
                status_code=303,
            )

        try:
            live_user = await fireshare.get_user(
                user_id=fireshare_user_id,
                username=fireshare_username,
            )

            if live_user is None:
                return await render_accounts_page(
                    request,
                    action_error=(
                        "The mapped Fireshare "
                        "account no longer exists."
                    ),
                    status_code=404,
                )

            live_user_id = live_user.get("id")

            if action == "disable":
                await fireshare.set_disabled(
                    live_user_id,
                    True,
                )

                logger.info(
                    "WebUI disabled Fireshare "
                    "account user_id=%s",
                    live_user_id,
                )

                return RedirectResponse(
                    "/accounts?message=Account+disabled.",
                    status_code=303,
                )

            if action == "enable":
                await fireshare.set_disabled(
                    live_user_id,
                    False,
                )

                logger.info(
                    "WebUI enabled Fireshare "
                    "account user_id=%s",
                    live_user_id,
                )

                return RedirectResponse(
                    "/accounts?message=Account+enabled.",
                    status_code=303,
                )

            if action == "reset":
                invite = (
                    await fireshare.create_invite(
                        live_user_id
                    )
                )

                if not invite:
                    return await render_accounts_page(
                        request,
                        action_error=(
                            "Fireshare could not "
                            "generate a setup link."
                        ),
                        status_code=500,
                    )

                invite_url = invite.get(
                    "invite_url"
                )

                if not invite_url:
                    return await render_accounts_page(
                        request,
                        action_error=(
                            "Fireshare returned an "
                            "unexpected setup-link response."
                        ),
                        status_code=500,
                    )

                logger.info(
                    "WebUI generated fresh setup "
                    "link user_id=%s",
                    live_user_id,
                )

                return await render_accounts_page(
                    request,
                    action_message=(
                        "Fresh password setup link "
                        "generated."
                    ),
                    invite_url=invite_url,
                )

            return await render_accounts_page(
                request,
                action_error=(
                    "Unknown account action."
                ),
                status_code=400,
            )

        except FireshareError as exc:
            return await render_accounts_page(
                request,
                action_error=str(exc),
                status_code=502,
            )



    @app.get(
        "/system",
        response_class=HTMLResponse,
    )
    async def system_page(
        request: Request,
    ):
        redirect = await require_auth(
            request
        )

        if redirect:
            return redirect

        # Discord health
        discord_ready = bool(
            discord_client
            and discord_client.is_ready()
        )

        discord_bot_name = "Unavailable"

        if (
            discord_client
            and discord_client.user
        ):
            discord_bot_name = str(
                discord_client.user
            )

        # Fireshare health
        fireshare_ready = False
        fireshare_users = None
        fireshare_error = None

        try:
            users = await fireshare.list_users()

            fireshare_users = len(users)
            fireshare_ready = True

        except FireshareError as exc:
            fireshare_error = str(exc)

        # Web administrator
        admin = await get_web_admin(
            database.path
        )

        admin_username = (
            admin[0]
            if admin
            else "Unavailable"
        )

        secure_cookies = (
            os.environ.get(
                "WEB_SECURE_COOKIES",
                "false",
            ).strip().lower()
            in (
                "1",
                "true",
                "yes",
                "on",
            )
        )

        web_port = os.environ.get(
            "WEB_PORT",
            "8090",
        )

        system_info = {
            "version": APP_VERSION,
            "runtime_mode": "Full runtime",
            "database_path": database.path,
            "web_port": web_port,
            "secure_cookies": secure_cookies,
            "admin_username": admin_username,
            "discord_ready": discord_ready,
            "discord_bot_name": discord_bot_name,
            "fireshare_ready": fireshare_ready,
            "fireshare_users": fireshare_users,
            "fireshare_error": fireshare_error,
        }

        return templates.TemplateResponse(
            request=request,
            name="system.html",
            context=template_context(
                request,
                system_info=system_info,
            ),
        )


    @app.get(
        "/settings/account",
        response_class=HTMLResponse,
    )
    async def account_settings(
        request: Request,
    ):
        redirect = await require_auth(request)

        if redirect:
            return redirect

        admin = await get_web_admin(
            database.path
        )

        return templates.TemplateResponse(
            request=request,
            name="account_settings.html",
            context=template_context(
                request,
                username=admin[0] if admin else "",
                error=None,
            ),
        )


    @app.post(
        "/settings/account/password",
        response_class=HTMLResponse,
    )
    async def account_password_change(
        request: Request,
    ):
        redirect = await require_auth(request)

        if redirect:
            return redirect

        form = await request.form()

        csrf = str(
            form.get("csrf_token", "")
        )

        current_password = str(
            form.get("current_password", "")
        )

        new_password = str(
            form.get("new_password", "")
        )

        confirm_password = str(
            form.get("confirm_password", "")
        )

        admin = await get_web_admin(
            database.path
        )

        error = None

        if not valid_csrf(request, csrf):
            error = (
                "Your session expired. "
                "Reload the page and try again."
            )

        elif not admin:
            error = (
                "The Firestarter administrator "
                "account could not be found."
            )

        elif not verify_password(
            current_password,
            admin[2],
            admin[1],
        ):
            error = (
                "Your current password is incorrect."
            )

        elif len(new_password) < 8:
            error = (
                "The new password must be "
                "at least 8 characters."
            )

        elif new_password != confirm_password:
            error = (
                "The new passwords do not match."
            )

        elif verify_password(
            new_password,
            admin[2],
            admin[1],
        ):
            error = (
                "Your new password must be "
                "different from your current password."
            )

        if error:
            return templates.TemplateResponse(
                request=request,
                name="account_settings.html",
                context=template_context(
                    request,
                    username=admin[0] if admin else "",
                    error=error,
                ),
                status_code=400,
            )

        salt, password_hash = hash_password(
            new_password
        )

        await update_web_admin_password(
            database.path,
            password_hash,
            salt,
        )

        logger.info(
            "WebUI administrator password changed "
            "username=%s source=%s",
            admin[0],
            client_address(request),
        )

        request.session.clear()

        return RedirectResponse(
            "/login?password_changed=1",
            status_code=303,
        )


    return app
