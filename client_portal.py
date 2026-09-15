from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import streamlit as st

DISCLOSURE_VERSION = "2026-09-15-paper-v1"
DISCLOSURE_TEXT = """This application is provided for educational, informational, and paper-trading
purposes only. It does not provide financial, investment, tax, accounting, or legal advice and
nothing displayed is an offer, solicitation, or recommendation to buy or sell a security.

Strategy profiles are generated from limited information supplied by the user and may not reflect
the user's complete circumstances, objectives, or ability to bear loss. All trading involves risk.
Backtests and simulated results do not guarantee future performance and may omit slippage, fees,
spread, latency, liquidity constraints, market disruptions, and execution failures.

This build is restricted to simulated paper trading. No profit or outcome is promised. Users are
responsible for reviewing all settings and should consult appropriately licensed professionals
before making financial decisions."""


class PortalError(RuntimeError):
    pass


def _portal_secrets() -> Any:
    try:
        return st.secrets["client_portal"]
    except (KeyError, FileNotFoundError):
        return None


def portal_enabled() -> bool:
    portal = _portal_secrets()
    return bool(portal and portal.get("enabled", False))


def _user_value(name: str, default: str = "") -> str:
    try:
        value = getattr(st.user, name, None)
        if value is None and hasattr(st.user, "get"):
            value = st.user.get(name)
        return str(value or default)
    except Exception:
        return default


def current_user() -> dict[str, str]:
    return {
        "user_id": _user_value("sub"),
        "email": _user_value("email"),
        "name": _user_value("name", _user_value("email", "Client")),
    }


def is_admin(user: dict[str, str]) -> bool:
    portal = _portal_secrets()
    if not portal:
        return True
    configured = portal.get("admin_emails", "")
    if isinstance(configured, str):
        emails = {value.strip().lower() for value in configured.split(",") if value.strip()}
    else:
        emails = {str(value).strip().lower() for value in configured}
    return user["email"].lower() in emails


def _supabase_request(method: str, table: str, *, query: str = "", body: Any = None) -> Any:
    portal = _portal_secrets()
    if not portal:
        raise PortalError("Client portal settings are missing.")
    url = str(portal.get("supabase_url", "")).rstrip("/")
    key = str(portal.get("supabase_service_role_key", ""))
    if not url or not key:
        raise PortalError("Supabase URL or service-role key is missing from Streamlit secrets.")
    target = f"{url}/rest/v1/{table}{query}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = Request(
        target,
        data=data,
        method=method,
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation,resolution=merge-duplicates",
        },
    )
    try:
        with urlopen(request, timeout=15) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise PortalError(f"Supabase rejected the request ({exc.code}): {detail}") from exc
    except URLError as exc:
        raise PortalError(f"Could not reach Supabase: {exc.reason}") from exc


def _upsert_profile(user: dict[str, str]) -> None:
    _supabase_request(
        "POST",
        "client_profiles",
        body={
            "user_id": user["user_id"],
            "email": user["email"],
            "display_name": user["name"],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def _has_accepted(user_id: str) -> bool:
    query = (
        f"?user_id=eq.{quote(user_id, safe='')}&disclosure_version="
        f"eq.{quote(DISCLOSURE_VERSION, safe='')}&select=id&limit=1"
    )
    return bool(_supabase_request("GET", "disclosure_acceptances", query=query))


def _record_acceptance(user: dict[str, str]) -> None:
    _supabase_request(
        "POST",
        "disclosure_acceptances",
        body={
            "user_id": user["user_id"],
            "disclosure_version": DISCLOSURE_VERSION,
            "accepted_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def require_client_access() -> dict[str, Any]:
    if not portal_enabled():
        return {"enabled": False, "user_id": "local-owner", "email": "", "name": "Owner", "admin": True}

    if not st.user.is_logged_in:
        st.title("Liquidity Sweep Paper Trader")
        st.warning("Educational and paper-trading use only. This application does not provide financial advice.")
        st.write("Sign in to create your private client profile and complete the paper-trading assessment.")
        if st.button("Continue with Google", type="primary"):
            st.login("google")
        st.stop()

    user = current_user()
    if not user["user_id"] or not user["email"]:
        st.error("Google did not return the required account identifier and email address.")
        if st.button("Sign out"):
            st.logout()
        st.stop()

    try:
        _upsert_profile(user)
        accepted = _has_accepted(user["user_id"])
    except PortalError as exc:
        st.error("Client account storage is not ready.")
        st.code(str(exc))
        st.info("Run supabase_schema.sql in Supabase, then check the client_portal values in Streamlit secrets.")
        st.stop()

    if not accepted:
        st.title("Paper-Trading Disclosure")
        st.info(DISCLOSURE_TEXT)
        agreed = st.checkbox("I have read and accept this disclosure and understand that this is paper trading, not financial advice.")
        age = st.checkbox("I confirm that I am at least 18 years old.")
        if st.button("Accept and create my profile", type="primary", disabled=not (agreed and age)):
            try:
                _record_acceptance(user)
                st.rerun()
            except PortalError as exc:
                st.error(str(exc))
        if st.button("Sign out"):
            st.logout()
        st.stop()

    return {**user, "enabled": True, "admin": is_admin(user)}


def load_user_data(user_id: str) -> dict[str, Any]:
    if not portal_enabled():
        return {}
    query = f"?user_id=eq.{quote(user_id, safe='')}&select=strategy_config,risk_result&limit=1"
    rows = _supabase_request("GET", "client_profiles", query=query)
    return rows[0] if rows else {}


def save_user_data(user_id: str, **values: Any) -> None:
    if not portal_enabled():
        return
    allowed = {key: value for key, value in values.items() if key in {"strategy_config", "risk_result"}}
    allowed["updated_at"] = datetime.now(timezone.utc).isoformat()
    query = f"?user_id=eq.{quote(user_id, safe='')}"
    _supabase_request("PATCH", "client_profiles", query=query, body=allowed)

