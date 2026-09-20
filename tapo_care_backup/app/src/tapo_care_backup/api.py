"""HTTP clients for Tapo cloud and Tapo Care cloud video APIs."""
from __future__ import annotations

from dataclasses import dataclass
import getpass
import json
import os
import time
import uuid
from typing import Any, Callable

import requests
import urllib3

from .config import StoredSession
from .region import KNOWN_REGIONS, app_server_url_for_region, care_base_url, region_from_app_server_url
from .signing import content_md5, x_authorization

LEGACY_CLOUD_URL = "https://eu-wap.tplinkcloud.com/"
SIGNED_CLOUD_URL = "https://n-wap-gw.tplinkcloud.com"
APP_TYPE_LEGACY = "Tapo_Android"
APP_TYPE_SIGNED = "TP-Link_Tapo_Android"

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class TapoApiError(RuntimeError):
    """Raised for TP-Link/Tapo API errors."""


TRANSIENT_REQUEST_ERRORS = (
    requests.exceptions.ConnectTimeout,
    requests.exceptions.ReadTimeout,
    requests.exceptions.Timeout,
    requests.exceptions.ConnectionError,
)


def _request_with_retries(send: Callable[[], requests.Response], attempts: int = 3, base_delay: float = 1.0) -> requests.Response:
    """Run a requests call with bounded retries for transient network failures."""
    last_error: requests.exceptions.RequestException | None = None
    for attempt in range(attempts):
        try:
            return send()
        except TRANSIENT_REQUEST_ERRORS as exc:
            last_error = exc
            if attempt < attempts - 1:
                time.sleep(base_delay * (2**attempt))
    if last_error is not None:
        raise last_error
    raise TapoApiError("request retry helper exhausted without an error")


@dataclass(frozen=True)
class TapoDevice:
    device_id: str
    alias: str
    device_type: str
    model: str | None = None
    app_server_url: str | None = None
    status: int | None = None


def _check_legacy_response(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("error_code") not in (0, None):
        raise TapoApiError(payload.get("msg") or payload.get("error_msg") or json.dumps(payload))
    return payload.get("result") or {}


def _check_tapo_care_response(response: requests.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise TapoApiError(f"Non-JSON Tapo Care response: HTTP {response.status_code}") from exc
    if response.status_code >= 400 or payload.get("code"):
        msg = payload.get("message") or f"HTTP {response.status_code}: {payload}"
        code = payload.get("code")
        if code is not None:
            msg = f"{msg} (code={code}, HTTP {response.status_code})"
        raise TapoApiError(msg)
    return payload


def _device_from_payload(item: dict[str, Any]) -> TapoDevice:
    return TapoDevice(
        device_id=item.get("deviceId") or item.get("device_id") or "",
        alias=item.get("alias") or item.get("nickname") or item.get("deviceName") or "camera",
        device_type=item.get("deviceType") or item.get("type") or "",
        model=item.get("deviceModel") or item.get("model"),
        app_server_url=item.get("appServerUrl"),
        status=item.get("status"),
    )


class TapoCloudClient:
    """Small client for account login and device discovery."""

    def __init__(self, http: requests.Session | None = None, verify_tls: bool = True):
        self.http = http or requests.Session()
        self.verify_tls = verify_tls

    def login_legacy(self, email: str, password: str, terminal_uuid: str | None = None) -> StoredSession:
        request = {
            "method": "login",
            "params": {
                "appType": APP_TYPE_LEGACY,
                "cloudUserName": email,
                "cloudPassword": password,
                "terminalUUID": terminal_uuid or uuid.uuid4().hex,
            },
        }
        response = _request_with_retries(lambda: self.http.post(LEGACY_CLOUD_URL, json=request, timeout=30, verify=self.verify_tls))
        result = _check_legacy_response(response.json())
        app_url = result.get("appServerUrl") or result.get("app_server_url")
        if not app_url:
            # Query device list to discover the region from camera's appServerUrl
            try:
                dev_resp = _request_with_retries(
                    lambda: self.http.post(LEGACY_CLOUD_URL, json={"method": "getDeviceList"}, params={"token": result["token"]}, timeout=15, verify=self.verify_tls)
                )
                dev_data = _check_legacy_response(dev_resp.json())
                for item in dev_data.get("deviceList", []):
                    if item.get("appServerUrl"):
                        app_url = item.get("appServerUrl")
                        print(f"[INFO] Discovered camera appServerUrl from device list: {app_url}", flush=True)
                        break
            except Exception:
                pass
        # If the camera lives on a regional server (e.g. use1-wap instead of eu-wap),
        # re-authenticate on that specific regional server so the token is issued by the matching region.
        if app_url:
            norm_target = app_url.rstrip("/") + "/"
            norm_initial = LEGACY_CLOUD_URL.rstrip("/") + "/"
            if norm_target != norm_initial:
                try:
                    print(f"[INFO] Authenticating directly with regional server {norm_target}...", flush=True)
                    reg_resp = _request_with_retries(
                        lambda: self.http.post(norm_target, json=request, timeout=30, verify=self.verify_tls)
                    )
                    reg_result = _check_legacy_response(reg_resp.json())
                    if reg_result.get("token"):
                        result = reg_result
                        print(f"[INFO] Successfully obtained regional token from {norm_target}", flush=True)
                except Exception as reg_err:
                    print(f"[WARN] Regional authentication on {norm_target} failed ({reg_err}); keeping initial token", flush=True)

        return StoredSession(
            token=result["token"],
            email=email,
            region=region_from_app_server_url(app_url),
            app_server_url=app_url,
        )

    def login_signed(
        self,
        email: str,
        password: str,
        access_key: str,
        client_secret: str,
        mfa_callback: Callable[[], str] | None = None,
        terminal_uuid: str | None = None,
    ) -> StoredSession:
        terminal_uuid = (terminal_uuid or uuid.uuid4().hex).upper()
        endpoint = "/api/v2/account/login"
        content = json.dumps(
            {
                "appType": APP_TYPE_SIGNED,
                "appVersion": "2.12.705",
                "cloudPassword": password,
                "cloudUserName": email,
                "platform": "Android 12",
                "refreshTokenNeeded": False,
                "terminalMeta": "1",
                "terminalName": "tapo-care-backup",
                "terminalUUID": terminal_uuid,
            },
            separators=(",", ":"),
        )
        response = self._signed_post(endpoint, content, access_key, client_secret)
        payload = response.json()
        if payload.get("error_code") != 0:
            raise TapoApiError(payload.get("msg") or json.dumps(payload))
        result = payload.get("result") or {}
        if "MFAProcessId" in result:
            if mfa_callback is None:
                mfa_callback = lambda: input("Tapo MFA code: ").strip()
            mfa_process_id = result["MFAProcessId"]
            self._request_mfa_code(email, password, terminal_uuid, access_key, client_secret)
            code = mfa_callback()
            result = self._check_mfa_code(email, code, mfa_process_id, access_key, client_secret)
        app_url = result.get("appServerUrl") or result.get("app_server_url")
        return StoredSession(
            token=result["token"],
            email=email,
            region=region_from_app_server_url(app_url),
            app_server_url=app_url,
        )

    def _signed_headers(self, endpoint: str, content: str, access_key: str, client_secret: str) -> dict[str, str]:
        now = str(int(time.time()))
        nonce = str(uuid.uuid1())
        return {
            "Content-Md5": content_md5(content),
            "X-Authorization": x_authorization(content, endpoint, now, nonce, access_key, client_secret),
            "Content-Type": "application/json; charset=UTF-8",
            "User-Agent": "okhttp/3.12.13",
        }

    def _signed_post(
        self,
        endpoint: str,
        content: str,
        access_key: str,
        client_secret: str,
        *,
        base_url: str = SIGNED_CLOUD_URL,
        params: dict[str, str] | None = None,
    ) -> requests.Response:
        return _request_with_retries(
            lambda: self.http.post(
                f"{base_url}{endpoint}",
                data=content,
                headers=self._signed_headers(endpoint, content, access_key, client_secret),
                params=params,
                timeout=30,
                verify=False,
            )
        )

    def _request_mfa_code(self, email: str, password: str, terminal_uuid: str, access_key: str, client_secret: str) -> None:
        endpoint = "/api/v2/account/getPushVC4TerminalMFA"
        content = json.dumps(
            {"appType": APP_TYPE_SIGNED, "cloudPassword": password, "cloudUserName": email, "terminalUUID": terminal_uuid},
            separators=(",", ":"),
        )
        payload = self._signed_post(endpoint, content, access_key, client_secret).json()
        if payload.get("error_code") != 0:
            raise TapoApiError(payload.get("msg") or json.dumps(payload))

    def _check_mfa_code(self, email: str, code: str, mfa_process_id: str, access_key: str, client_secret: str) -> dict[str, Any]:
        endpoint = "/api/v2/account/checkMFACodeAndLogin"
        content = json.dumps(
            {
                "appType": APP_TYPE_SIGNED,
                "cloudUserName": email,
                "code": code,
                "MFAProcessId": mfa_process_id,
                "MFAType": 1,
                "terminalBindEnabled": True,
            },
            separators=(",", ":"),
        )
        payload = self._signed_post(endpoint, content, access_key, client_secret).json()
        if payload.get("error_code") != 0:
            raise TapoApiError(payload.get("msg") or json.dumps(payload))
        return payload.get("result") or {}

    def _list_devices_legacy(self, session: StoredSession) -> list[TapoDevice]:
        request = {"method": "getDeviceList"}
        response = _request_with_retries(lambda: self.http.post(LEGACY_CLOUD_URL, json=request, params={"token": session.token}, timeout=30, verify=self.verify_tls))
        result = _check_legacy_response(response.json())
        return [_device_from_payload(item) for item in result.get("deviceList", [])]

    def _list_devices_signed(self, session: StoredSession, access_key: str, client_secret: str) -> list[TapoDevice]:
        endpoint = "/api/v2/common/getDeviceListByPage"
        content = json.dumps(
            {
                "deviceTypeList": [
                    "SMART.TAPOPLUG",
                    "SMART.TAPOBULB",
                    "SMART.IPCAMERA",
                    "SMART.TAPOROBOVAC",
                    "SMART.TAPOHUB",
                    "SMART.TAPOSENSOR",
                    "SMART.TAPOSWITCH",
                ],
                "index": 0,
                "limit": 100,
            },
            separators=(",", ":"),
        )
        base_url = session.app_server_url or app_server_url_for_region(session.region)
        response = self._signed_post(
            endpoint,
            content,
            access_key,
            client_secret,
            base_url=base_url,
            params={"token": session.token},
        )
        result = _check_legacy_response(response.json())
        return [_device_from_payload(item) for item in result.get("deviceList", [])]

    def list_devices(self, session: StoredSession) -> list[TapoDevice]:
        try:
            return self._list_devices_legacy(session)
        except TapoApiError as exc:
            access_key = os.environ.get("TAPO_CLIENT_ACCESS_KEY")
            client_secret = os.environ.get("TAPO_CLIENT_SECRET")
            if not access_key or not client_secret:
                raise exc
            return self._list_devices_signed(session, access_key, client_secret)


_REGION_FORWARD_PHRASES = ("failed to region forward", "region forward", "regionforward")


def _is_region_forward_error(exc: TapoApiError) -> bool:
    return any(phrase in str(exc).lower() for phrase in _REGION_FORWARD_PHRASES)


class TapoCareClient:
    """Client for Tapo Care cloud video listing and media downloads."""

    def __init__(self, session: StoredSession, http: requests.Session | None = None, verify_tls: bool = False):
        self.session = session
        self.http = http or requests.Session()
        self.verify_tls = verify_tls
        # Populated after a successful region probe so callers can persist it.
        self.discovered_region: str | None = None

    def _list_videos_for_region(
        self,
        region: str,
        device_id: str,
        start_time: str | None,
        end_time: str | None,
        page: int,
        page_size: int,
        order: str,
    ) -> dict[str, Any]:
        """Low-level video list request for a specific region."""
        params: dict[str, Any] = {
            "deviceId": device_id,
            "page": page,
            "pageSize": page_size,
            "order": order,
        }
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time
        headers = {"Authorization": f"ut|{self.session.token}", "X-App-Name": APP_TYPE_SIGNED}
        base = care_base_url(region)
        response = _request_with_retries(
            lambda: self.http.get(f"{base}/v2/videos/list", params=params, headers=headers, timeout=30, verify=self.verify_tls)
        )
        try:
            return _check_tapo_care_response(response)
        except TapoApiError as v2_err:
            # If pageSize was large, try a smaller page size in case of cloud query limits
            if params.get("pageSize", 0) > 100:
                try:
                    p_retry = dict(params, pageSize=100)
                    resp_retry = _request_with_retries(
                        lambda: self.http.get(f"{base}/v2/videos/list", params=p_retry, headers=headers, timeout=30, verify=self.verify_tls)
                    )
                    return _check_tapo_care_response(resp_retry)
                except TapoApiError:
                    pass
            # Older server versions expose /v1/videos; retry once for compatibility.
            try:
                response = _request_with_retries(
                    lambda: self.http.get(f"{base}/v1/videos", params=params, headers=headers, timeout=30, verify=self.verify_tls)
                )
                return _check_tapo_care_response(response)
            except TapoApiError:
                raise v2_err

    def _probe_region(
        self,
        device_id: str,
        start_time: str | None,
        end_time: str | None,
        page: int,
        page_size: int,
        order: str,
    ) -> tuple[str, dict[str, Any]]:
        """Try every known region and return (region, payload) for the first that works.

        Skips the current session region (already failed) and tries the others in a
        well-known order that favours regions common for South-American accounts first.
        """
        current = self.session.region or ""
        candidates = [r for r in KNOWN_REGIONS if r != current] + ([current] if current and current not in KNOWN_REGIONS else [])
        errors: list[str] = []
        for region in candidates:
            try:
                print(f"[INFO] Probing Tapo Care region '{region}'...", flush=True)
                payload = self._list_videos_for_region(region, device_id, start_time, end_time, page, page_size, order)
                print(f"[INFO] Region '{region}' accepted — updating session.", flush=True)
                return region, payload
            except Exception as exc:
                print(f"[INFO] Region '{region}' rejected: {exc}", flush=True)
                errors.append(f"{region}: {exc}")
        raise TapoApiError(f"All regions failed during probe: {'; '.join(errors)}")

    def list_videos(
        self,
        device_id: str,
        start_time: str | None = None,
        end_time: str | None = None,
        page: int = 0,
        page_size: int = 3000,
        order: str = "desc",
    ) -> dict[str, Any]:
        # Use the previously discovered region if available.
        region = self.discovered_region or self.session.region
        try:
            return self._list_videos_for_region(region, device_id, start_time, end_time, page, page_size, order)
        except TapoApiError as exc:
            err_msg = str(exc).lower()
            if not (_is_region_forward_error(exc) or "internal error" in err_msg):
                raise
            print(
                f"[WARN] Tapo Care region '{region}' returned '{exc}'. "
                "Starting region auto-discovery...",
                flush=True,
            )
            found_region, payload = self._probe_region(device_id, start_time, end_time, page, page_size, order)
            self.discovered_region = found_region
            return payload

    def iter_video_pages(
        self,
        device_id: str,
        start_time: str | None = None,
        end_time: str | None = None,
        page_size: int = 3000,
        order: str = "desc",
    ):
        """Yield every Tapo Care video-list page until the API total is exhausted."""
        seen = 0
        page = 0
        while True:
            payload = self.list_videos(device_id, start_time, end_time, page=page, page_size=page_size, order=order)
            index = payload.get("index") or []
            yield payload
            seen += len(index)
            total = payload.get("total")
            if not index:
                break
            if isinstance(total, int) and seen >= total:
                break
            if len(index) < page_size and not isinstance(total, int):
                break
            page += 1

    def download_bytes(self, url: str) -> bytes:
        response = _request_with_retries(lambda: self.http.get(url, timeout=120))
        response.raise_for_status()
        return response.content


def login_from_env_or_prompt(auth_mode: str = "legacy", verify_tls: bool = True) -> StoredSession:
    email = os.environ.get("TAPO_USERNAME") or input("TP-Link ID email: ").strip()
    password = os.environ.get("TAPO_PASSWORD") or getpass.getpass("TP-Link password: ")
    client = TapoCloudClient(verify_tls=verify_tls)
    if auth_mode == "legacy":
        return client.login_legacy(email, password)
    if auth_mode == "signed":
        access_key = os.environ.get("TAPO_CLIENT_ACCESS_KEY")
        client_secret = os.environ.get("TAPO_CLIENT_SECRET")
        if not access_key or not client_secret:
            raise TapoApiError("signed auth requires TAPO_CLIENT_ACCESS_KEY and TAPO_CLIENT_SECRET")
        return client.login_signed(email, password, access_key, client_secret)
    raise ValueError(f"Unsupported auth mode: {auth_mode}")
