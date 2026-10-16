import asyncio
import hmac
import logging
import os
import secrets
from contextlib import asynccontextmanager
from urllib.parse import urlencode

import aiohttp
from fastapi import FastAPI, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import (
    SessionMiddleware,
)

from fireshare import (
    FireshareClient,
    FireshareError,
)
from webapp import (
    USERNAME_PATTERN,
    create_web_admin,
    ensure_web_schema,
    get_web_admin,
    hash_password,
    load_or_create_session_secret,
    verify_password,
)


logger = logging.getLogger(
    "firestarter.setup"
)

APP_VERSION = os.environ.get(
    "APP_VERSION",
    "dev",
)

BASE_DIR = os.path.dirname(
    os.path.abspath(__file__)
)

templates = Jinja2Templates(
    directory=os.path.join(
        BASE_DIR,
        "templates",
    )
)



REQUIRED_SETTINGS = [
    "app_name",
    "public_service_name",
    "discord_token",
    "discord_guild_id",
    "onboarding_role_id",
    "fireshare_url",
    "public_fireshare_url",
    "fireshare_username",
    "fireshare_password",
]


async def discord_request(
    token,
    path,
):
    headers = {
        "Authorization": f"Bot {token}",
        "User-Agent": (
            "Firestarter "
            f"{APP_VERSION}"
        ),
    }

    timeout = aiohttp.ClientTimeout(
        total=20
    )

    async with aiohttp.ClientSession(
        timeout=timeout
    ) as session:
        async with session.get(
            "https://discord.com/api/v10"
            + path,
            headers=headers,
        ) as response:

            try:
                data = await response.json(
                    content_type=None
                )
            except Exception:
                data = {}

            return response.status, data


async def get_discord_identity(
    token,
):
    status, data = await discord_request(
        token,
        "/users/@me",
    )

    if status != 200:
        return None

    return data


async def get_discord_guilds(
    token,
):
    status, data = await discord_request(
        token,
        "/users/@me/guilds",
    )

    if status != 200:
        return []

    if not isinstance(data, list):
        return []

    return sorted(
        data,
        key=lambda guild: (
            guild.get(
                "name",
                "",
            ).casefold()
        ),
    )


async def get_discord_roles(
    token,
    guild_id,
):
    status, data = await discord_request(
        token,
        f"/guilds/{guild_id}/roles",
    )

    if status != 200:
        return []

    if not isinstance(data, list):
        return []

    roles = [
        role
        for role in data
        if str(role.get("id"))
        != str(guild_id)
    ]

    return sorted(
        roles,
        key=lambda role: (
            -int(
                role.get(
                    "position",
                    0,
                )
            ),
            role.get(
                "name",
                "",
            ).casefold(),
        ),
    )


def create_setup_app(
    store,
    database_path,
    request_runtime_handoff,
):
    @asynccontextmanager
    async def lifespan(app):
        await ensure_web_schema(
            database_path
        )

        yield

    app = FastAPI(
        title="Firestarter Setup",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    app.mount(
        "/static",
        StaticFiles(
            directory=os.path.join(
                BASE_DIR,
                "static",
            )
        ),
        name="static",
    )

    app.add_middleware(
        SessionMiddleware,
        secret_key=(
            load_or_create_session_secret()
        ),
        session_cookie=(
            "firestarter_session"
        ),
        max_age=43200,
        same_site="lax",
        https_only=False,
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
            "Cache-Control"
        ] = "no-store"

        response.headers[
            "Content-Security-Policy"
        ] = (
            "default-src 'self'; "
            "style-src 'self'; "
            "img-src 'self' data:; "
            "frame-ancestors 'none'; "
            "form-action 'self'"
        )

        return response

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

    def context(
        request,
        **extra,
    ):
        output = {
            "request": request,
            "app_name": "Firestarter",
            "app_version": APP_VERSION,
            "service_name": (
                "First-run setup"
            ),
            "authenticated": (
                authenticated(request)
            ),
            "csrf_token": (
                csrf_token(request)
            ),
        }

        output.update(extra)

        return output

    async def require_auth(request):
        if not authenticated(request):
            return RedirectResponse(
                "/login",
                status_code=303,
            )

        return None

    async def configuration_status():
        status = {}

        for key in REQUIRED_SETTINGS:
            value = await store.get(
                key
            )

            status[key] = bool(value)

        return status

    @app.get("/healthz")
    async def healthz():
        status = (
            await configuration_status()
        )

        return JSONResponse({
            "status": "ok",
            "mode": "setup",
            "configured": all(
                status.values()
            ),
        })

    @app.get("/")
    async def index(
        request: Request,
    ):
        admin = await get_web_admin(
            database_path
        )

        if admin is None:
            return RedirectResponse(
                "/setup/admin",
                status_code=303,
            )

        if not authenticated(request):
            return RedirectResponse(
                "/login",
                status_code=303,
            )

        return RedirectResponse(
            "/setup/instance",
            status_code=303,
        )

    @app.get(
        "/setup/admin",
        response_class=HTMLResponse,
    )
    async def admin_setup_page(
        request: Request,
    ):
        admin = await get_web_admin(
            database_path
        )

        if admin is not None:
            return RedirectResponse(
                "/login",
                status_code=303,
            )

        return templates.TemplateResponse(
            request=request,
            name="setup_admin.html",
            context=context(
                request,
                error=None,
            ),
        )

    @app.post(
        "/setup/admin",
        response_class=HTMLResponse,
    )
    async def admin_setup_submit(
        request: Request,
    ):
        admin = await get_web_admin(
            database_path
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

        error = None

        if not valid_csrf(
            request,
            csrf,
        ):
            error = "Session expired."

        elif not USERNAME_PATTERN.fullmatch(
            username
        ):
            error = (
                "Invalid administrator username."
            )

        elif len(password) < 8:
            error = (
                "Password must be at least "
                "8 characters."
            )

        elif password != confirm:
            error = (
                "Passwords do not match."
            )

        if error:
            return templates.TemplateResponse(
                request=request,
                name="setup_admin.html",
                context=context(
                    request,
                    error=error,
                ),
                status_code=400,
            )

        salt, digest = hash_password(
            password
        )

        created = await create_web_admin(
            database_path,
            username,
            digest,
            salt,
        )

        if not created:
            return RedirectResponse(
                "/login",
                status_code=303,
            )

        request.session.clear()

        request.session[
            "authenticated"
        ] = True

        request.session[
            "username"
        ] = username

        request.session[
            "csrf_token"
        ] = secrets.token_urlsafe(
            32
        )

        return RedirectResponse(
            "/setup/instance",
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
            database_path
        )

        if admin is None:
            return RedirectResponse(
                "/setup/admin",
                status_code=303,
            )

        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context=context(
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

        if not valid_csrf(
            request,
            str(
                form.get(
                    "csrf_token",
                    "",
                )
            ),
        ):
            return RedirectResponse(
                "/login",
                status_code=303,
            )

        admin = await get_web_admin(
            database_path
        )

        username = str(
            form.get(
                "username",
                "",
            )
        )

        password = str(
            form.get(
                "password",
                "",
            )
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
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context=context(
                    request,
                    error=(
                        "Invalid username "
                        "or password."
                    ),
                ),
                status_code=400,
            )

        request.session.clear()

        request.session[
            "authenticated"
        ] = True

        request.session[
            "username"
        ] = username

        request.session[
            "csrf_token"
        ] = secrets.token_urlsafe(
            32
        )

        return RedirectResponse(
            "/setup/instance",
            status_code=303,
        )

    @app.post("/logout")
    async def logout(
        request: Request,
    ):
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
                "Invalid CSRF token.",
                status_code=403,
            )

        request.session.clear()

        return RedirectResponse(
            "/login",
            status_code=303,
        )


    @app.get(
        "/setup/instance",
        response_class=HTMLResponse,
    )
    async def instance_page(
        request: Request,
    ):
        redirect = await require_auth(
            request
        )

        if redirect:
            return redirect

        return templates.TemplateResponse(
            request=request,
            name="setup_instance.html",
            context=context(
                request,
                current_app_name=(
                    await store.get(
                        "app_name",
                        "Firestarter",
                    )
                ),
                current_service_name=(
                    await store.get(
                        "public_service_name",
                        "",
                    )
                ),
            ),
        )

    @app.post(
        "/setup/instance",
    )
    async def instance_save(
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
                "Invalid CSRF token.",
                status_code=403,
            )

        app_name = str(
            form.get(
                "app_name",
                "Firestarter",
            )
        ).strip()

        service_name = str(
            form.get(
                "service_name",
                "",
            )
        ).strip()

        if not app_name:
            app_name = "Firestarter"

        if not service_name:
            return HTMLResponse(
                "Service name is required.",
                status_code=400,
            )

        await store.set(
            "app_name",
            app_name,
            secret=False,
        )

        await store.set(
            "public_service_name",
            service_name,
            secret=False,
        )

        return RedirectResponse(
            "/setup/discord",
            status_code=303,
        )

    @app.get(
        "/setup/discord",
        response_class=HTMLResponse,
    )
    async def discord_page(
        request: Request,
    ):
        redirect = await require_auth(
            request
        )

        if redirect:
            return redirect

        token = await store.get(
            "discord_token",
            "",
        )

        identity = None
        guilds = []
        roles = []

        if token:
            identity = (
                await get_discord_identity(
                    token
                )
            )

            guilds = (
                await get_discord_guilds(
                    token
                )
            )

        selected_guild_id = (
            request.query_params.get(
                "guild_id"
            )
            or await store.get(
                "discord_guild_id",
                "",
            )
        )

        if (
            token
            and selected_guild_id
        ):
            roles = (
                await get_discord_roles(
                    token,
                    selected_guild_id,
                )
            )

        selected_role_id = (
            await store.get(
                "onboarding_role_id",
                "",
            )
        )

        invite_url = None

        if identity:
            query = urlencode({
                "client_id": (
                    identity["id"]
                ),
                "permissions": "19456",
                "scope": (
                    "bot applications.commands"
                ),
            })

            invite_url = (
                "https://discord.com/"
                "oauth2/authorize?"
                + query
            )

        return templates.TemplateResponse(
            request=request,
            name="setup_discord.html",
            context=context(
                request,
                token_configured=bool(token),
                identity=identity,
                guilds=guilds,
                roles=roles,
                selected_guild_id=str(
                    selected_guild_id
                    or ""
                ),
                selected_role_id=str(
                    selected_role_id
                    or ""
                ),
                invite_url=invite_url,
                token_test=(
                    request.query_params.get(
                        "token_test"
                    )
                ),
            ),
        )

    @app.post(
        "/setup/discord/token",
    )
    async def discord_token_save(
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
                "Invalid CSRF token.",
                status_code=403,
            )

        token = str(
            form.get(
                "discord_token",
                "",
            )
        ).strip()

        if not token:
            token = await store.get(
                "discord_token",
                "",
            )

        identity = (
            await get_discord_identity(
                token
            )
            if token
            else None
        )

        if identity is None:
            return RedirectResponse(
                "/setup/discord"
                "?token_test=fail",
                status_code=303,
            )

        await store.set(
            "discord_token",
            token,
            secret=True,
        )

        logger.info(
            "Discord token verified "
            "bot_id=%s",
            identity.get("id"),
        )

        return RedirectResponse(
            "/setup/discord"
            "?token_test=ok",
            status_code=303,
        )

    @app.post(
        "/setup/discord/save",
    )
    async def discord_save(
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
                "Invalid CSRF token.",
                status_code=403,
            )

        token = await store.get(
            "discord_token",
            "",
        )

        guild_id = str(
            form.get(
                "guild_id",
                "",
            )
        )

        role_id = str(
            form.get(
                "role_id",
                "",
            )
        )

        guilds = await get_discord_guilds(
            token
        )

        valid_guild = any(
            str(guild.get("id"))
            == guild_id
            for guild in guilds
        )

        if not valid_guild:
            return HTMLResponse(
                "Invalid Discord server.",
                status_code=400,
            )

        roles = await get_discord_roles(
            token,
            guild_id,
        )

        valid_role = any(
            str(role.get("id"))
            == role_id
            for role in roles
        )

        if not valid_role:
            return HTMLResponse(
                "Invalid Discord role.",
                status_code=400,
            )

        await store.set(
            "discord_guild_id",
            guild_id,
            secret=False,
        )

        await store.set(
            "onboarding_role_id",
            role_id,
            secret=False,
        )

        return RedirectResponse(
            "/setup/fireshare",
            status_code=303,
        )

    @app.get(
        "/setup/fireshare",
        response_class=HTMLResponse,
    )
    async def fireshare_page(
        request: Request,
    ):
        redirect = await require_auth(
            request
        )

        if redirect:
            return redirect

        return templates.TemplateResponse(
            request=request,
            name="setup_fireshare.html",
            context=context(
                request,
                fireshare_url=(
                    await store.get(
                        "fireshare_url",
                        "",
                    )
                ),
                public_url=(
                    await store.get(
                        "public_fireshare_url",
                        "",
                    )
                ),
                username=(
                    await store.get(
                        "fireshare_username",
                        "",
                    )
                ),
                password_configured=bool(
                    await store.get(
                        "fireshare_password",
                        "",
                    )
                ),
                test_result=(
                    request.query_params.get(
                        "test"
                    )
                ),
            ),
        )

    @app.post(
        "/setup/fireshare",
    )
    async def fireshare_save(
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
                "Invalid CSRF token.",
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
                "public_url",
                "",
            )
        ).strip().rstrip("/")

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

        if not password:
            password = await store.get(
                "fireshare_password",
                "",
            )

        if not (
            base_url
            and public_url
            and username
            and password
        ):
            return RedirectResponse(
                "/setup/fireshare"
                "?test=fail",
                status_code=303,
            )

        client = FireshareClient(
            base_url,
            username,
            password,
        )

        try:
            await client.start()
            await client.list_users()

        except FireshareError:
            return RedirectResponse(
                "/setup/fireshare"
                "?test=fail",
                status_code=303,
            )

        finally:
            await client.close()

        await store.set(
            "fireshare_url",
            base_url,
            secret=False,
        )

        await store.set(
            "public_fireshare_url",
            public_url,
            secret=False,
        )

        await store.set(
            "fireshare_username",
            username,
            secret=False,
        )

        await store.set(
            "fireshare_password",
            password,
            secret=True,
        )

        return RedirectResponse(
            "/setup/review",
            status_code=303,
        )

    @app.get(
        "/setup/review",
        response_class=HTMLResponse,
    )
    async def review_page(
        request: Request,
    ):
        redirect = await require_auth(
            request
        )

        if redirect:
            return redirect

        config_status = (
            await configuration_status()
        )

        return templates.TemplateResponse(
            request=request,
            name="setup_review.html",
            context=context(
                request,
                config_status=config_status,
                ready=all(
                    config_status.values()
                ),
                app_value=(
                    await store.get(
                        "app_name",
                        ""
                    )
                ),
                service_value=(
                    await store.get(
                        "public_service_name",
                        ""
                    )
                ),
                guild_value=(
                    await store.get(
                        "discord_guild_id",
                        ""
                    )
                ),
                role_value=(
                    await store.get(
                        "onboarding_role_id",
                        ""
                    )
                ),
                fireshare_value=(
                    await store.get(
                        "public_fireshare_url",
                        ""
                    )
                ),
            ),
        )

    @app.post(
        "/setup/finish",
    )
    async def setup_finish(
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
                "Invalid CSRF token.",
                status_code=403,
            )

        status = (
            await configuration_status()
        )

        if not all(
            status.values()
        ):
            return RedirectResponse(
                "/setup/review",
                status_code=303,
            )

        await store.set(
            "setup_complete",
            "1",
            secret=False,
        )

        async def handoff():
            await asyncio.sleep(2)

            logger.info(
                "Setup complete. "
                "Requesting runtime handoff."
            )

            request_runtime_handoff()

        asyncio.create_task(
            handoff()
        )

        return templates.TemplateResponse(
            request=request,
            name="setup_complete.html",
            context=context(
                request
            ),
        )

    return app
