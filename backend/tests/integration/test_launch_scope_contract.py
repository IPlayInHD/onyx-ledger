"""What Onyx advertises must be something the engine can actually price.

THE FAILURE THIS PREVENTS. The frontend used to keep its own hard-coded list of
provinces and tax years. It drifted from the engine immediately and offered
Quebec — which the engine has brackets for but no QPP or QPIP handling, so a
Quebec customer would have received a confident figure computed with the wrong
payroll contributions. A wrong number presented exactly like a right one.

The launch scope is now a SETTING, served by `/api/v1/config/launch-scope`, and
these tests are the reason that setting cannot become a second tax registry:
every pair it advertises is checked against the engine's own resolved dataset,
which remains the authority on what is computable. The setting may narrow that
authority. It may not exceed it.
"""
from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.database.session import unit_of_work
from app.services.tax_engine.core.provider import TaxDataProvider


@pytest.fixture(autouse=True)
async def _dispose_engine():
    yield
    from app.database.session import engine

    await engine.dispose()


def test_the_launch_scope_is_not_empty() -> None:
    """Non-vacuity. Every assertion below is "everything advertised resolves",
    which an empty list satisfies perfectly while offering nobody anything."""
    assert get_settings().launch_tax_years, "no tax year is offered"
    assert get_settings().launch_provinces, "no province is offered"


async def test_every_advertised_pair_resolves_in_the_engine() -> None:
    """The whole point: the offer is a subset of the capability.

    `dataset.provinces` is the same set the analysis service refuses against, so
    this asks the authority itself rather than restating it.
    """
    async with unit_of_work(actor_type="system") as session:
        provider = TaxDataProvider(session)
        for year in get_settings().launch_tax_years:
            dataset = await provider.resolve(year)
            for province in get_settings().launch_provinces:
                assert province in dataset.provinces, (
                    f"launch scope offers {province} for {year}, but the engine's "
                    f"resolved dataset for {year} contains only "
                    f"{sorted(dataset.provinces)}. A customer choosing it would "
                    "be refused at analysis time, or worse, priced wrongly."
                )


async def test_the_endpoint_serves_exactly_the_configured_scope(client) -> None:
    """Anonymous on purpose — onboarding needs it before an account exists."""
    response = await client.get("/api/v1/config/launch-scope")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["tax_years"] == list(get_settings().launch_tax_years)
    assert [p["code"] for p in body["provinces"]] == list(get_settings().launch_provinces)
    for province in body["provinces"]:
        assert province["name"], f"{province['code']} has no display name"


async def test_the_launch_scope_excludes_quebec(client) -> None:
    """A named regression, not a general principle.

    Quebec is the specific jurisdiction that was offered and could not be
    answered for. If it is ever added back it must be because QPP and QPIP are
    implemented — and this test is where somebody has to say so out loud.
    """
    body = (await client.get("/api/v1/config/launch-scope")).json()
    assert "QC" not in [p["code"] for p in body["provinces"]]
