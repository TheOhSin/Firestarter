import asyncio
import logging

import aiohttp


logger = logging.getLogger("firestarter.fireshare")


CONTRIBUTOR_PERMISSIONS = [
    "view_private",
    "upload",
    "edit_own",
    "delete_own",
    "manage_tags",
]


class FireshareError(Exception):
    pass


class FireshareClient:
    def __init__(
        self,
        base_url,
        username,
        password,
    ):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.session = None
        self.authenticated = False
        self.login_lock = asyncio.Lock()

    async def start(self):
        self.session = aiohttp.ClientSession(
            cookie_jar=aiohttp.CookieJar(
                unsafe=True
            ),
            timeout=aiohttp.ClientTimeout(
                total=30
            ),
        )

    async def close(self):
        if self.session:
            await self.session.close()

    async def login(self, force=False):
        if self.authenticated and not force:
            return

        async with self.login_lock:
            if self.authenticated and not force:
                return

            try:
                async with self.session.post(
                    f"{self.base_url}/api/login",
                    json={
                        "username": self.username,
                        "password": self.password,
                    },
                ) as response:

                    if response.status != 200:
                        logger.error(
                            "Fireshare login failed status=%s",
                            response.status,
                        )
                        raise FireshareError(
                            "Firestarter could not authenticate "
                            "with Fireshare."
                        )

                    try:
                        data = await response.json()
                    except Exception:
                        data = {}

                    if data.get("mfa_required"):
                        raise FireshareError(
                            "The Firestarter Fireshare service "
                            "account has MFA enabled."
                        )

                    self.authenticated = True

            except FireshareError:
                raise

            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
            ) as exc:
                logger.error(
                    "Fireshare connection failure: %s",
                    type(exc).__name__,
                )

                raise FireshareError(
                    "Fireshare is temporarily unavailable."
                )

    async def request(
        self,
        method,
        path,
        expected,
        json=None,
    ):
        await self.login()

        for attempt in range(2):
            try:
                async with self.session.request(
                    method,
                    f"{self.base_url}{path}",
                    json=json,
                ) as response:

                    if (
                        response.status == 401
                        and attempt == 0
                    ):
                        self.authenticated = False
                        await self.login(force=True)
                        continue

                    try:
                        data = await response.json(
                            content_type=None
                        )
                    except Exception:
                        data = {}

                    return response.status, data

            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
            ):
                raise FireshareError(
                    "Fireshare is temporarily unavailable."
                )

        raise FireshareError(
            "Firestarter could not authenticate "
            "with Fireshare."
        )

    async def list_users(self):
        status, data = await self.request(
            "GET",
            "/api/admin/users",
            200,
        )

        if status != 200:
            raise FireshareError(
                "Firestarter could not read "
                "Fireshare accounts."
            )

        users = data.get("users")

        if not isinstance(users, list):
            raise FireshareError(
                "Fireshare returned an unexpected "
                "account response."
            )

        return users

    async def get_user(
        self,
        user_id=None,
        username=None,
    ):
        users = await self.list_users()

        if user_id is not None:
            for user in users:
                if user.get("id") != user_id:
                    continue

                if username is None:
                    return user

                if (
                    str(
                        user.get(
                            "username",
                            "",
                        )
                    ).casefold()
                    == username.casefold()
                ):
                    return user

                return None

            return None

        if username:
            wanted = username.casefold()

            for user in users:
                if (
                    str(
                        user.get(
                            "username",
                            "",
                        )
                    ).casefold()
                    == wanted
                ):
                    return user

        return None

    async def create_contributor(
        self,
        username,
        display_name=None,
    ):
        body = {
            "username": username,
            "admin": False,
            "permissions": CONTRIBUTOR_PERMISSIONS,
            "invite": True,
            "profile_public": True,
        }

        if display_name:
            body["display_name"] = display_name

        status, data = await self.request(
            "POST",
            "/api/admin/users",
            201,
            json=body,
        )

        if status == 409:
            raise FireshareError(
                "That username is already taken."
            )

        if status != 201:
            raise FireshareError(
                "Fireshare could not create "
                "that account."
            )

        if (
            not data.get("user")
            or not data.get("invite_url")
        ):
            raise FireshareError(
                "Fireshare returned an unexpected "
                "account creation response."
            )

        return data

    async def set_disabled(
        self,
        user_id,
        disabled=True,
    ):
        status, data = await self.request(
            "PUT",
            f"/api/admin/users/{user_id}",
            200,
            json={
                "disabled": bool(disabled),
            },
        )

        if status == 404:
            return None

        if status != 200:
            raise FireshareError(
                "Fireshare could not update "
                "that account."
            )

        return data.get("user")

    async def create_invite(
        self,
        user_id,
    ):
        status, data = await self.request(
            "POST",
            f"/api/admin/users/{user_id}/invite",
            200,
        )

        if status == 404:
            return None

        if status != 200:
            raise FireshareError(
                "Fireshare could not generate "
                "a password setup link."
            )

        return data
