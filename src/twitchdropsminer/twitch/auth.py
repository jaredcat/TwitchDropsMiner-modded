"""
Authentication handling for Twitch Drops Miner.

This module contains the _AuthState class and related authentication functionality.
"""

from __future__ import annotations

import re
import sys
import json
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, cast, Any

if sys.platform == "win32":
    from subprocess import CREATE_NO_WINDOW

import aiohttp
from yarl import URL

try:
    from seleniumwire.request import Request
    from selenium.common.exceptions import WebDriverException
    from seleniumwire.undetected_chromedriver import Chrome, ChromeOptions
except ModuleNotFoundError:
    # the dependencies weren't installed, but they're not used either, so skip them
    pass
except ImportError as exc:
    if "_brotli" in exc.msg:
        raise ImportError(
            "You need to install Visual C++ Redist (x86 and x64): "
            "https://support.microsoft.com/en-gb/help/2977003/"
            "the-latest-supported-visual-c-downloads"
        ) from exc
    raise

from ..cache import CurrentSeconds
from ..translate import _
from ..exceptions import (
    MinerException,
    CaptchaRequired,
    LoginException,
    RequestInvalid,
)
from ..utils import (
    CHARS_HEX_LOWER,
    create_nonce,
    first_to_complete,
    task_wrapper,
    ExponentialBackoff,
)
from ..constants import (
    COOKIES_PATH,
    GQL_OPERATIONS,
    ClientType,
)

if TYPE_CHECKING:
    from .client import Twitch
    from ..gui import LoginForm
    from ..constants import ClientInfo, JsonType


class SkipExtraJsonDecoder(json.JSONDecoder):
    def decode(self, s: str, *args):
        # skip whitespace check
        obj, end = self.raw_decode(s)
        return obj


SAFE_LOADS = lambda s: json.loads(s, cls=SkipExtraJsonDecoder)


class _AuthState:
    def __init__(self, twitch: Twitch):
        self._twitch: Twitch = twitch
        self._lock = asyncio.Lock()
        self._logged_in = asyncio.Event()
        self.user_id: int
        self.device_id: str
        self.session_id: str
        self.access_token: str
        self.client_version: str
        self.integrity_token: str
        self.integrity_expires: datetime

    @property
    def integrity_expired(self) -> bool:
        return (
            not hasattr(self, "integrity_expires")
            or datetime.now(timezone.utc) >= self.integrity_expires
        )

    def _hasattrs(self, *attrs: str) -> bool:
        return all(hasattr(self, attr) for attr in attrs)

    def _delattrs(self, *attrs: str) -> None:
        for attr in attrs:
            if hasattr(self, attr):
                delattr(self, attr)

    def clear(self) -> None:
        self._delattrs(
            "user_id",
            "device_id",
            "session_id",
            "access_token",
            "client_version",
            "integrity_token",
            "integrity_expires",
        )
        self._logged_in.clear()

    def interceptor(self, request: Request) -> None:
        if (
            request.method == "POST"
            and request.url == "https://passport.twitch.tv/protected_login"
        ):
            body = request.body.decode("utf-8")
            data = json.loads(body)
            data["client_id"] = self._twitch._client_type.CLIENT_ID
            request.body = json.dumps(data).encode("utf-8")
            del request.headers["Content-Length"]
            request.headers["Content-Length"] = str(len(request.body))

    async def _chrome_login(self) -> None:
        gui_print = self._twitch.gui.print
        login_form: LoginForm = self._twitch.gui.login
        coro_unless_closed = self._twitch.gui.coro_unless_closed

        # open the chrome browser on the Twitch's login page
        # use a separate executor to void blocking the event loop
        loop = asyncio.get_running_loop()
        driver: Chrome | None = None
        while True:
            gui_print(_("login", "chrome", "startup"))
            try:
                version_main = None
                for attempt in range(2):
                    options = ChromeOptions()
                    options.add_argument("--log-level=3")
                    options.add_argument("--disable-web-security")
                    options.add_argument("--allow-running-insecure-content")
                    options.add_argument("--lang=en")
                    options.add_argument("--disable-gpu")
                    options.set_capability("pageLoadStrategy", "eager")
                    try:
                        wire_options: dict[str, Any] = {"proxy": {}}
                        if self._twitch.settings.proxy:
                            wire_options["proxy"]["http"] = str(self._twitch.settings.proxy)
                        driver_coro = loop.run_in_executor(
                            None,
                            lambda: Chrome(
                                options=options,
                                no_sandbox=True,
                                suppress_welcome=True,
                                version_main=version_main,
                                seleniumwire_options=wire_options,
                                service_creationflags=CREATE_NO_WINDOW,
                            )
                        )
                        driver = await coro_unless_closed(driver_coro)
                        break
                    except WebDriverException as exc:
                        message = exc.msg
                        if (
                            message is not None
                            and (
                                match := re.search(
                                    (
                                        r'Chrome version ([\d]+)\n'
                                        r'Current browser version is ((\d+)\.[\d.]+)'
                                    ),
                                    message,
                                )
                            ) is not None
                        ):
                            if not attempt:
                                version_main = int(match.group(3))
                                continue
                            else:
                                raise MinerException(
                                    "Your Chrome browser is out of date\n"
                                    f"Required version: {match.group(1)}\n"
                                    f"Current version: {match.group(2)}"
                                ) from None
                        raise MinerException(
                            "An error occured while boostrapping the Chrome browser"
                        ) from exc
                assert driver is not None
                driver.request_interceptor = self.interceptor
                # driver.set_page_load_timeout(30)
                # page_coro = loop.run_in_executor(None, driver.get, "https://twitch.tv")
                # await coro_unless_closed(page_coro)
                page_coro = loop.run_in_executor(None, driver.get, "https://twitch.tv/login")
                await coro_unless_closed(page_coro)

                # auto login
                # if login_data.username and login_data.password:
                #     driver.find_element("id", "login-username").send_keys(login_data.username)
                #     driver.find_element("id", "password-input").send_keys(login_data.password)
                #     driver.find_element(
                #         "css selector", '[data-a-target="passport-login-button"]'
                #     ).click()
                # token submit button css selectors
                # Button: "screen="two_factor" target="submit_button"
                # Input: <input type="text" autocomplete="one-time-code" data-a-target="tw-input"
                # inputmode="numeric" pattern="[0-9]*" value="">

                # wait for the user to navigate away from the URL, indicating successful login
                # alternatively, they can press on the login button again
                async def url_waiter(driver=driver):
                    while driver.current_url != "https://www.twitch.tv/?no-reload=true":
                        await asyncio.sleep(0.5)

                gui_print(_("login", "chrome", "login_to_complete"))
                await first_to_complete([
                    url_waiter(),
                    coro_unless_closed(login_form.wait_for_login_press()),
                ])

                # cookies = [
                #     {
                #         "domain": ".twitch.tv",
                #         "expiry": 1700000000,
                #         "httpOnly": False,
                #         "name": "auth-token",
                #         "path": "/",
                #         "sameSite": "None",
                #         "secure": True,
                #         "value": "..."
                #     },
                #     ...,
                # ]
                cookies = driver.get_cookies()
                for cookie in cookies:
                    if "twitch.tv" in cookie["domain"] and cookie["name"] == "auth-token":
                        self.access_token = cookie["value"]
                        break
                else:
                    gui_print(_("login", "chrome", "no_token"))
            except WebDriverException:
                gui_print(_("login", "chrome", "closed_window"))
            finally:
                if driver is not None:
                    driver.quit()
                    driver = None
            await coro_unless_closed(login_form.wait_for_login_press())

    async def _oauth_login(self) -> str:
        login_form: LoginForm = self._twitch.gui.login
        client_info: ClientInfo = self._twitch._client_type
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "Accept-Language": "en-US",
            "Cache-Control": "no-cache",
            "Client-Id": client_info.CLIENT_ID,
            "Host": "id.twitch.tv",
            "Origin": str(client_info.CLIENT_URL),
            "Pragma": "no-cache",
            "Referer": str(client_info.CLIENT_URL),
            "User-Agent": client_info.USER_AGENT,
            "X-Device-Id": self.device_id,
        }
        payload = {
            "client_id": client_info.CLIENT_ID,
            "scopes": (
                "channel_read chat:read user_blocks_edit "
                "user_blocks_read user_follows_edit user_read"
            ),
        }
        while True:
            try:
                async with self._twitch.request(
                    "POST", "https://id.twitch.tv/oauth2/device", headers=headers, data=payload
                ) as response:
                    # {
                    #     "device_code": "40 chars [A-Za-z0-9]",
                    #     "expires_in": 1800,
                    #     "interval": 5,
                    #     "user_code": "8 chars [A-Z]",
                    #     "verification_uri": "https://www.twitch.tv/activate"
                    # }
                    now = datetime.now(timezone.utc)
                    response_json: JsonType = await response.json()
                    device_code: str = response_json["device_code"]
                    user_code: str = response_json["user_code"]
                    interval: int = response_json["interval"]
                    expires_at = now + timedelta(seconds=response_json["expires_in"])

                # Print the code to the user, open them the activate page so they can type it in
                await login_form.ask_enter_code(user_code)

                payload = {
                    "client_id": self._twitch._client_type.CLIENT_ID,
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                }
                while True:
                    # sleep first, not like the user is gonna enter the code *that* fast
                    await asyncio.sleep(interval)
                    async with self._twitch.request(
                        "POST",
                        "https://id.twitch.tv/oauth2/token",
                        headers=headers,
                        data=payload,
                        invalidate_after=expires_at,
                    ) as response:
                        # 200 means success, 400 means the user haven't entered the code yet
                        if response.status != 200:
                            continue
                        response_json = await response.json()
                        # {
                        #     "access_token": "40 chars [A-Za-z0-9]",
                        #     "refresh_token": "40 chars [A-Za-z0-9]",
                        #     "scope": [...],
                        #     "token_type": "bearer"
                        # }
                        self.access_token = cast(str, response_json["access_token"])
                        return self.access_token
            except RequestInvalid:
                # the device_code has expired, request a new code
                continue

    async def _login(self) -> str:
        logger = logging.getLogger("TwitchDrops")
        logger.info("Login flow started")
        gui_print = self._twitch.gui.print
        login_form: LoginForm = self._twitch.gui.login
        client_info: ClientInfo = self._twitch._client_type

        token_kind: str = ''
        use_chrome: bool = False
        payload: JsonType = {
            # username and password are added later
            # "username": str,
            # "password": str,
            # client ID to-be associated with the access token
            "client_id": client_info.CLIENT_ID,
            "undelete_user": False,  # purpose unknown
            "remember_me": True,  # persist the session via the cookie
            # "authy_token": str,  # 2FA token
            # "twitchguard_code": str,  # email code
            # "captcha": str,  # self-fed captcha
            # 'force_twitchguard': False,  # force email code confirmation
        }

        while True:
            login_data = await login_form.ask_login()
            payload["username"] = login_data.username
            payload["password"] = login_data.password
            # reinstate the 2FA token, if present
            payload.pop("authy_token", None)
            payload.pop("twitchguard_code", None)
            if login_data.token:
                # if there's no token kind set yet, and the user has entered a token,
                # we can immediately assume it's an authenticator token and not an email one
                if not token_kind:
                    token_kind = "authy"
                if token_kind == "authy":
                    payload["authy_token"] = login_data.token
                elif token_kind == "email":
                    payload["twitchguard_code"] = login_data.token

            # use fancy headers to mimic the twitch android app
            headers = {
                "Accept": "application/vnd.twitchtv.v3+json",
                "Accept-Encoding": "gzip",
                "Accept-Language": "en-US",
                "Client-Id": client_info.CLIENT_ID,
                "Content-Type": "application/json; charset=UTF-8",
                "Host": "passport.twitch.tv",
                "User-Agent": client_info.USER_AGENT,
                "X-Device-Id": self.device_id,
                # "X-Device-Id": ''.join(random.choices('0123456789abcdef', k=32)),
            }
            async with self._twitch.request(
                "POST", "https://passport.twitch.tv/login", headers=headers, json=payload
            ) as response:
                login_response: JsonType = await response.json(loads=SAFE_LOADS)

            # Feed this back in to avoid running into CAPTCHA if possible
            if "captcha_proof" in login_response:
                payload["captcha"] = {"proof": login_response["captcha_proof"]}

            # Error handling
            if "error_code" in login_response:
                error_code: int = login_response["error_code"]
                logger.info(f"Login error code: {error_code}")
                if error_code == 1000:
                    logger.info("1000: CAPTCHA is required")
                    use_chrome = True
                    break
                elif error_code in (2004, 3001):
                    logger.info("3001: Login failed due to incorrect username or password")
                    gui_print(_("login", "incorrect_login_pass"))
                    if error_code == 2004:
                        # invalid username
                        login_form.clear(login=True)
                    login_form.clear(password=True)
                    continue
                elif error_code in (
                    3012,  # Invalid authy token
                    3023,  # Invalid email code
                ):
                    logger.info("3012/23: Login failed due to incorrect 2FA code")
                    if error_code == 3023:
                        token_kind = "email"
                        gui_print(_("login", "incorrect_email_code"))
                    else:
                        token_kind = "authy"
                        gui_print(_("login", "incorrect_twofa_code"))
                    login_form.clear(token=True)
                    continue
                elif error_code in (
                    3011,  # Authy token needed
                    3022,  # Email code needed
                ):
                    # 2FA handling
                    logger.info("3011/22: 2FA token required")
                    # user didn't provide a token, so ask them for it
                    if error_code == 3022:
                        token_kind = "email"
                        gui_print(_("login", "email_code_required"))
                    else:
                        token_kind = "authy"
                        gui_print(_("login", "twofa_code_required"))
                    continue
                elif error_code >= 5000:
                    # Special errors, usually from Twitch telling the user to "go away"
                    # We print the code out to inform the user, and just use chrome flow instead
                    # {
                    #     "error_code":5023,
                    #     "error":"Please update your app to continue",
                    #     "error_description":"client is not supported for this feature"
                    # }
                    # {
                    #     "error_code":5027,
                    #     "error":"Please update your app to continue",
                    #     "error_description":"client blocked from this operation"
                    # }
                    gui_print(_("login", "error_code").format(error_code=error_code))
                    logger.info(str(login_response))
                    use_chrome = True
                    break
                else:
                    ext_msg = str(login_response)
                    logger.info(ext_msg)
                    raise LoginException(ext_msg)
            # Success handling
            if "access_token" in login_response:
                self.access_token = cast(str, login_response["access_token"])
                logger.info("Access token granted")
                login_form.clear()
                break

        if use_chrome:
            # await self._chrome_login()
            raise CaptchaRequired()

        if hasattr(self, "access_token"):
            return self.access_token
        raise MinerException("Login flow finished without setting the access token")

    def headers(
        self, *, user_agent: str = '', gql: bool = False, integrity: bool = False
    ) -> JsonType:
        client_info: ClientInfo = self._twitch._client_type
        headers = {
            "Accept": "*/*",
            "Accept-Encoding": "gzip",
            "Accept-Language": "en-US",
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
            "Client-Id": client_info.CLIENT_ID,
        }
        if user_agent:
            headers["User-Agent"] = user_agent
        if hasattr(self, "session_id"):
            headers["Client-Session-Id"] = self.session_id
        # if hasattr(self, "client_version"):
            # headers["Client-Version"] = self.client_version
        if hasattr(self, "device_id"):
            headers["X-Device-Id"] = self.device_id
        if gql:
            headers["Origin"] = str(client_info.CLIENT_URL)
            headers["Referer"] = str(client_info.CLIENT_URL)
            headers["Authorization"] = f"OAuth {self.access_token}"
        if integrity:
            headers["Client-Integrity"] = self.integrity_token
        return headers

    async def validate(self):
        async with self._lock:
            await self._validate()

    async def _validate(self):
        if not hasattr(self, "session_id"):
            self.session_id = create_nonce(CHARS_HEX_LOWER, 16)
        if not self._hasattrs("device_id", "access_token", "user_id"):
            session = await self._twitch.get_session()
            jar = cast(aiohttp.CookieJar, session.cookie_jar)
            client_info: ClientInfo = self._twitch._client_type
        if not self._hasattrs("device_id"):
            async with self._twitch.request(
                "GET", client_info.CLIENT_URL, headers=self.headers()
            ) as response:
                page_html = await response.text("utf8")
                assert page_html is not None
            #     match = re.search(r'twilightBuildID="([-a-z0-9]+)"', page_html)
            # if match is None:
            #     raise MinerException("Unable to extract client_version")
            # self.client_version = match.group(1)
            # doing the request ends up setting the "unique_id" value in the cookie
            cookie = jar.filter_cookies(client_info.CLIENT_URL)
            self.device_id = cookie["unique_id"].value
        if not self._hasattrs("access_token", "user_id"):
            # looks like we're missing something
            login_form: LoginForm = self._twitch.gui.login
            logger = logging.getLogger("TwitchDrops")
            logger.info("Checking login")
            login_form.update(_("gui", "login", "logging_in"), None)
            for attempt in range(2):
                cookie = jar.filter_cookies(client_info.CLIENT_URL)
                if "auth-token" not in cookie:
                    self.access_token = await self._oauth_login()
                    cookie["auth-token"] = self.access_token
                elif not hasattr(self, "access_token"):
                    logger.info("Restoring session from cookie")
                    self.access_token = cookie["auth-token"].value
                # validate the auth token, by obtaining user_id
                async with self._twitch.request(
                    "GET",
                    "https://id.twitch.tv/oauth2/validate",
                    headers={"Authorization": f"OAuth {self.access_token}"}
                ) as response:
                    status = response.status
                    if status == 401:
                        # the access token we have is invalid - clear the cookie and reauth
                        logger.info("Restored session is invalid")
                        assert client_info.CLIENT_URL.host is not None
                        jar.clear_domain(client_info.CLIENT_URL.host)
                        continue
                    elif status == 200:
                        validate_response = await response.json()
                        break
            else:
                raise RuntimeError("Login verification failure")
            if validate_response["client_id"] != client_info.CLIENT_ID:
                raise MinerException("You're using an old cookie file, please generate a new one.")
            self.user_id = int(validate_response["user_id"])
            cookie["persistent"] = str(self.user_id)
            logger.info(f"Login successful, user ID: {self.user_id}")
            login_form.update(_("gui", "login", "logged_in"), self.user_id)
            # update our cookie and save it
            jar.update_cookies(cookie, client_info.CLIENT_URL)
            jar.save(COOKIES_PATH)
        # if not self._hasattrs("integrity_token") or self.integrity_expired:
        #     async with self._twitch.request(
        #         "POST",
        #         "https://gql.twitch.tv/integrity",
        #         headers=self.gql_headers(integrity=False)
        #     ) as response:
        #         self._last_request = datetime.now(timezone.utc)
        #         response_json: JsonType = await response.json()
        #     self.integrity_token = cast(str, response_json["token"])
        #     now = datetime.now(timezone.utc)
        #     expiration = datetime.fromtimestamp(response_json["expiration"] / 1000, timezone.utc)
        #     self.integrity_expires = ((expiration - now) * 0.9) + now
        #     # verify the integrity token's contents for the "is_bad_bot" flag
        #     stripped_token: str = self.integrity_token.split('.')[2] + "=="
        #     messy_json: str = urlsafe_b64decode(stripped_token.encode()).decode(errors="ignore")
        #     match = re.search(r'(.+)(?<="}).+$', messy_json)
        #     if match is None:
        #         raise MinerException("Unable to parse the integrity token")
        #     decoded_header: JsonType = json.loads(match.group(1))
        #     if decoded_header.get("is_bad_bot", "false") != "false":
        #         self._twitch.print(
        #             "Twitch has detected this miner as a \"Bad Bot\". "
        #             "You're proceeding at your own risk!"
        #         )
        #         await asyncio.sleep(8)
        self._logged_in.set()

    def invalidate(self, *, auth: bool = False, integrity: bool = False):
        if auth:
            self._delattrs("access_token")
        if integrity:
            self._delattrs("client_version")
            self.integrity_expires = datetime.now(timezone.utc)