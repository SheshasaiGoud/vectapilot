"""The real app plus probe routes that only echo what the auth dependencies gave them (M8 PR 1).

The session routes themselves come with PR 1's routes step; until then these probes are how the
dependencies are tested, without a database here (tests/auth/test_dependencies.py, with
`ScriptedDatabase`) and against Postgres in the canary suite (tests/canary/test_sessions.py, on
the real `Database`). They exist only in tests.
"""

from typing import Annotated, Final

from fastapi import Depends, FastAPI

from vectapilot_api.auth.dependencies import (
    get_customer_session,
    require_same_origin,
    require_staff,
)
from vectapilot_api.auth.store import CustomerSession, StaffRole, StaffSession
from vectapilot_api.config import Settings
from vectapilot_api.main import create_app

CUSTOMER_PROBE: Final = "/probe/customer"
REVIEWER_PROBE: Final = "/probe/reviewer"
ADMIN_PROBE: Final = "/probe/admin"
CSRF_PROBE: Final = "/probe/csrf"


def customer_echo(session: CustomerSession) -> dict[str, str]:
    return {
        "business_id": session.business_id,
        "session_id": str(session.session_id),
        "customer_ref": session.customer_ref,
        "ui_language": session.ui_language,
    }


def staff_echo(session: StaffSession) -> dict[str, str]:
    return {
        "business_id": session.business_id,
        "session_id": str(session.session_id),
        "staff_id": str(session.staff_id),
        "role": session.role.value,
        "display_name": session.display_name,
    }


def probe_app(settings: Settings) -> FastAPI:
    app = create_app(settings)

    @app.get(CUSTOMER_PROBE)
    async def customer(
        session: Annotated[CustomerSession, Depends(get_customer_session)],
    ) -> dict[str, str]:
        return customer_echo(session)

    @app.get(REVIEWER_PROBE)
    async def reviewer(
        session: Annotated[StaffSession, Depends(require_staff(StaffRole.REVIEWER))],
    ) -> dict[str, str]:
        return staff_echo(session)

    @app.get(ADMIN_PROBE)
    async def admin(
        session: Annotated[StaffSession, Depends(require_staff(StaffRole.ADMIN))],
    ) -> dict[str, str]:
        return staff_echo(session)

    @app.post(CSRF_PROBE, dependencies=[Depends(require_same_origin)])
    async def csrf() -> dict[str, bool]:
        return {"passed": True}

    return app
