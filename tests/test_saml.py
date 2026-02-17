"""Tests for SSO Deep Integration (SAML).

Covers:
- SAMLAuthProvider.parse_saml_response - valid response, status failure, expired
- SAMLAuthProvider.extract_claims - attribute extraction
- SAMLAuthProvider.map_groups_to_roles - mapping precedence
- SAMLAuthProvider.validate_signature - xmlsec path and fallback
- SAMLAuthProvider.build_auth_request - redirect URL construction
- IdPConfig SQLAlchemy model - basic field validation
- SSO API endpoints: /providers CRUD, /saml/metadata, /saml/login
- Multi-IdP per tenant scenarios
"""

from __future__ import annotations

import base64
import uuid
from datetime import datetime, timedelta, timezone
from textwrap import dedent
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.auth.saml import (
    IdPConfiguration,
    SAMLAuthProvider,
    SAMLSignatureError,
    SAMLValidationError,
)
from src.models.user import UserRole

# ---------------------------------------------------------------------------
# Sample SAML XML fixtures
# ---------------------------------------------------------------------------

_NOW = datetime.now(timezone.utc)
_FMT = "%Y-%m-%dT%H:%M:%SZ"
_VALID_NOT_BEFORE = (_NOW - timedelta(minutes=5)).strftime(_FMT)
_VALID_NOT_ON_OR_AFTER = (_NOW + timedelta(hours=1)).strftime(_FMT)
_EXPIRED_NOT_ON_OR_AFTER = (_NOW - timedelta(minutes=1)).strftime(_FMT)
_FUTURE_NOT_BEFORE = (_NOW + timedelta(hours=1)).strftime(_FMT)

_TENANT_ID = uuid.uuid4()
_IDP_ID = uuid.uuid4()

SAMPLE_IDP_CONFIG = IdPConfiguration(
    idp_id=_IDP_ID,
    tenant_id=_TENANT_ID,
    entity_id="https://idp.example.com",
    sso_url="https://idp.example.com/sso",
    certificate_pem=(
        "-----BEGIN CERTIFICATE-----\n"
        "MIICpDCCAYwCCQDU+pQ4pHgSpDANBgkqhkiG9w0BAQsFADAUMRIwEAYDVQQDDAls\n"
        "b2NhbGhvc3QwHhcNMjMwMTAxMDAwMDAwWhcNMjQwMTAxMDAwMDAwWjAUMRIwEAYD\n"
        "VQQDDAlsb2NhbGhvc3QwggEiMA0GCSqGSIb3DQEBAQUAA4IBDwAwggEKAoIBAQC7\n"
        "-----END CERTIFICATE-----"
    ),
    group_role_mapping={
        "platform-admins": "admin",
        "platform-operators": "operator",
        "platform-users": "viewer",
    },
)


def _make_saml_response(
    status_code: str = "urn:oasis:names:tc:SAML:2.0:status:Success",
    not_before: str = _VALID_NOT_BEFORE,
    not_on_or_after: str = _VALID_NOT_ON_OR_AFTER,
    name_id: str = "alice@example.com",
    email: str = "alice@example.com",
    display_name: str = "Alice Smith",
    groups: list[str] | None = None,
    issuer: str = "https://idp.example.com",
) -> str:
    """Build a minimal SAML Response XML for testing."""
    if groups is None:
        groups = ["platform-users"]

    group_attr_values = "".join(
        f'<saml:AttributeValue xsi:type="xs:string">{g}</saml:AttributeValue>'
        for g in groups
    )

    return dedent(f"""<?xml version="1.0" encoding="UTF-8"?>
    <samlp:Response
        xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol"
        xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion"
        xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
        ID="_response123"
        Version="2.0"
        IssueInstant="{_NOW.strftime(_FMT)}">
      <samlp:Status>
        <samlp:StatusCode Value="{status_code}"/>
      </samlp:Status>
      <saml:Assertion
          xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion"
          ID="_assertion456"
          Version="2.0"
          IssueInstant="{_NOW.strftime(_FMT)}">
        <saml:Issuer>{issuer}</saml:Issuer>
        <saml:Subject>
          <saml:NameID
              Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress"
              >{name_id}</saml:NameID>
        </saml:Subject>
        <saml:Conditions NotBefore="{not_before}" NotOnOrAfter="{not_on_or_after}">
        </saml:Conditions>
        <saml:AuthnStatement SessionIndex="_session789" AuthnInstant="{_NOW.strftime(_FMT)}">
          <saml:AuthnContext>
            <saml:AuthnContextClassRef>
              urn:oasis:names:tc:SAML:2.0:ac:classes:Password
            </saml:AuthnContextClassRef>
          </saml:AuthnContext>
        </saml:AuthnStatement>
        <saml:AttributeStatement>
          <saml:Attribute Name="email">
            <saml:AttributeValue xsi:type="xs:string">{email}</saml:AttributeValue>
          </saml:Attribute>
          <saml:Attribute Name="displayName">
            <saml:AttributeValue xsi:type="xs:string">{display_name}</saml:AttributeValue>
          </saml:Attribute>
          <saml:Attribute Name="groups">
            {group_attr_values}
          </saml:Attribute>
        </saml:AttributeStatement>
      </saml:Assertion>
    </samlp:Response>
    """).strip()


# ---------------------------------------------------------------------------
# SAMLAuthProvider.parse_saml_response tests
# ---------------------------------------------------------------------------


class TestParseSAMLResponse:
    @pytest.mark.asyncio
    async def test_valid_response_returns_claims(self) -> None:
        provider = SAMLAuthProvider()
        xml = _make_saml_response()

        result = await provider.parse_saml_response(xml, SAMPLE_IDP_CONFIG)

        assert result["name_id"] == "alice@example.com"
        assert result["name_id_format"] == (
            "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress"
        )
        assert result["session_index"] == "_session789"

    @pytest.mark.asyncio
    async def test_valid_response_extracts_attributes(self) -> None:
        provider = SAMLAuthProvider()
        xml = _make_saml_response(
            email="alice@example.com",
            display_name="Alice Smith",
        )

        result = await provider.parse_saml_response(xml, SAMPLE_IDP_CONFIG)

        assert "email" in result["attributes"]
        assert result["attributes"]["email"] == ["alice@example.com"]
        assert result["claims"]["email"] == "alice@example.com"
        assert result["claims"]["name"] == "Alice Smith"

    @pytest.mark.asyncio
    async def test_valid_response_extracts_groups(self) -> None:
        provider = SAMLAuthProvider()
        xml = _make_saml_response(groups=["platform-admins", "platform-operators"])

        result = await provider.parse_saml_response(xml, SAMPLE_IDP_CONFIG)

        assert "platform-admins" in result["groups"]
        assert "platform-operators" in result["groups"]

    @pytest.mark.asyncio
    async def test_status_failure_raises_validation_error(self) -> None:
        provider = SAMLAuthProvider()
        xml = _make_saml_response(
            status_code="urn:oasis:names:tc:SAML:2.0:status:AuthnFailed"
        )

        with pytest.raises(SAMLValidationError, match="authentication failed"):
            await provider.parse_saml_response(xml, SAMPLE_IDP_CONFIG)

    @pytest.mark.asyncio
    async def test_expired_assertion_raises_validation_error(self) -> None:
        provider = SAMLAuthProvider()
        xml = _make_saml_response(
            not_on_or_after=_EXPIRED_NOT_ON_OR_AFTER,
        )

        with pytest.raises(SAMLValidationError, match="expired"):
            await provider.parse_saml_response(xml, SAMPLE_IDP_CONFIG)

    @pytest.mark.asyncio
    async def test_future_not_before_raises_validation_error(self) -> None:
        provider = SAMLAuthProvider()
        xml = _make_saml_response(not_before=_FUTURE_NOT_BEFORE)

        with pytest.raises(SAMLValidationError, match="not yet valid"):
            await provider.parse_saml_response(xml, SAMPLE_IDP_CONFIG)

    @pytest.mark.asyncio
    async def test_wrong_issuer_raises_validation_error(self) -> None:
        provider = SAMLAuthProvider()
        xml = _make_saml_response(issuer="https://evil.example.com")

        with pytest.raises(SAMLValidationError, match="Issuer"):
            await provider.parse_saml_response(xml, SAMPLE_IDP_CONFIG)

    @pytest.mark.asyncio
    async def test_bytes_input_accepted(self) -> None:
        provider = SAMLAuthProvider()
        xml_str = _make_saml_response()
        xml_bytes = xml_str.encode("utf-8")

        result = await provider.parse_saml_response(xml_bytes, SAMPLE_IDP_CONFIG)
        assert result["name_id"] == "alice@example.com"

    @pytest.mark.asyncio
    async def test_no_name_id_raises_validation_error(self) -> None:
        provider = SAMLAuthProvider()
        # Manually remove the NameID element
        xml = _make_saml_response()
        xml_no_nameid = xml.replace(
            '<saml:NameID\n              Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress"\n              >alice@example.com</saml:NameID>',
            "",
        )

        with pytest.raises(SAMLValidationError, match="NameID"):
            await provider.parse_saml_response(xml_no_nameid, SAMPLE_IDP_CONFIG)


# ---------------------------------------------------------------------------
# SAMLAuthProvider.extract_claims tests
# ---------------------------------------------------------------------------


class TestExtractClaims:
    @pytest.mark.asyncio
    async def test_extract_claims_from_element(self) -> None:
        """extract_claims should normalise SAML attributes into flat claims."""
        provider = SAMLAuthProvider()
        xml = _make_saml_response(
            email="bob@corp.com",
            display_name="Bob Jones",
        )
        # Parse and grab the assertion element
        from xml.etree import ElementTree as ET

        root = ET.fromstring(xml)
        ns = {"saml": "urn:oasis:names:tc:SAML:2.0:assertion"}
        assertion = root.find("saml:Assertion", ns)
        assert assertion is not None

        claims = await provider.extract_claims(assertion)

        assert claims["email"] == "bob@corp.com"
        assert claims["name"] == "Bob Jones"
        assert "raw_attributes" in claims


# ---------------------------------------------------------------------------
# SAMLAuthProvider.map_groups_to_roles tests
# ---------------------------------------------------------------------------


class TestMapGroupsToRoles:
    @pytest.mark.asyncio
    async def test_admin_group_maps_to_admin(self) -> None:
        provider = SAMLAuthProvider()
        role = await provider.map_groups_to_roles(
            ["platform-admins"], SAMPLE_IDP_CONFIG
        )
        assert role == UserRole.ADMIN

    @pytest.mark.asyncio
    async def test_operator_group_maps_to_operator(self) -> None:
        provider = SAMLAuthProvider()
        role = await provider.map_groups_to_roles(
            ["platform-operators"], SAMPLE_IDP_CONFIG
        )
        assert role == UserRole.OPERATOR

    @pytest.mark.asyncio
    async def test_viewer_group_maps_to_viewer(self) -> None:
        provider = SAMLAuthProvider()
        role = await provider.map_groups_to_roles(
            ["platform-users"], SAMPLE_IDP_CONFIG
        )
        assert role == UserRole.VIEWER

    @pytest.mark.asyncio
    async def test_multiple_groups_highest_wins(self) -> None:
        """When user is in both operator and admin groups, admin wins."""
        provider = SAMLAuthProvider()
        role = await provider.map_groups_to_roles(
            ["platform-operators", "platform-admins"], SAMPLE_IDP_CONFIG
        )
        assert role == UserRole.ADMIN

    @pytest.mark.asyncio
    async def test_unknown_groups_default_to_viewer(self) -> None:
        provider = SAMLAuthProvider()
        role = await provider.map_groups_to_roles(
            ["random-group", "another-group"], SAMPLE_IDP_CONFIG
        )
        assert role == UserRole.VIEWER

    @pytest.mark.asyncio
    async def test_empty_groups_defaults_to_viewer(self) -> None:
        provider = SAMLAuthProvider()
        role = await provider.map_groups_to_roles([], SAMPLE_IDP_CONFIG)
        assert role == UserRole.VIEWER

    @pytest.mark.asyncio
    async def test_empty_mapping_defaults_to_viewer(self) -> None:
        provider = SAMLAuthProvider()
        idp = IdPConfiguration(
            idp_id=_IDP_ID,
            tenant_id=_TENANT_ID,
            entity_id="https://idp.example.com",
            sso_url="https://idp.example.com/sso",
            certificate_pem="",
            group_role_mapping={},  # no mapping at all
        )
        role = await provider.map_groups_to_roles(["admins"], idp)
        assert role == UserRole.VIEWER

    @pytest.mark.asyncio
    async def test_case_insensitive_group_match(self) -> None:
        """Group names should match case-insensitively."""
        provider = SAMLAuthProvider()
        role = await provider.map_groups_to_roles(
            ["PLATFORM-ADMINS"],  # uppercase from IdP
            SAMPLE_IDP_CONFIG,
        )
        assert role == UserRole.ADMIN


# ---------------------------------------------------------------------------
# SAMLAuthProvider.validate_signature tests
# ---------------------------------------------------------------------------


class TestValidateSignature:
    @pytest.mark.asyncio
    async def test_validates_with_xmlsec(self) -> None:
        """When xmlsec is available and signature is valid, return True."""
        provider = SAMLAuthProvider()
        mock_ctx = MagicMock()
        mock_ctx.verify = MagicMock()  # no exception = valid

        mock_key = MagicMock()
        mock_xmlsec = MagicMock()
        mock_xmlsec.SignatureContext.return_value = mock_ctx
        mock_xmlsec.Key.from_memory.return_value = mock_key
        mock_xmlsec.tree.find_node.return_value = MagicMock()  # non-None
        mock_xmlsec.constants.NodeSignature = "Signature"
        mock_xmlsec.constants.KeyDataFormatCertPem = "certpem"

        import lxml.etree as lxml_et

        with (
            patch.dict("sys.modules", {"xmlsec": mock_xmlsec}),
            patch("lxml.etree.fromstring", return_value=MagicMock()),
        ):
            result = await provider.validate_signature(
                b"<xml/>",
                SAMPLE_IDP_CONFIG.certificate_pem,
            )

        assert result is True

    @pytest.mark.asyncio
    async def test_invalid_signature_returns_false(self) -> None:
        """When xmlsec raises, return False."""
        provider = SAMLAuthProvider()

        mock_xmlsec = MagicMock()
        mock_xmlsec.SignatureContext.return_value.verify.side_effect = Exception(
            "Signature invalid"
        )
        mock_xmlsec.tree.find_node.return_value = MagicMock()
        mock_xmlsec.constants.NodeSignature = "Signature"
        mock_xmlsec.constants.KeyDataFormatCertPem = "certpem"

        with (
            patch.dict("sys.modules", {"xmlsec": mock_xmlsec}),
            patch("lxml.etree.fromstring", return_value=MagicMock()),
        ):
            result = await provider.validate_signature(
                b"<xml/>",
                SAMPLE_IDP_CONFIG.certificate_pem,
            )

        assert result is False

    @pytest.mark.asyncio
    async def test_missing_xmlsec_returns_false(self) -> None:
        """Without xmlsec installed, validation returns False and logs critical."""
        provider = SAMLAuthProvider()

        # Remove xmlsec from available modules
        with patch.dict("sys.modules", {"xmlsec": None, "lxml": None, "lxml.etree": None}):
            result = await provider.validate_signature(
                b"<xml/>",
                SAMPLE_IDP_CONFIG.certificate_pem,
            )

        assert result is False

    @pytest.mark.asyncio
    async def test_no_signature_node_returns_false(self) -> None:
        """If the XML has no Signature element, return False."""
        provider = SAMLAuthProvider()

        mock_xmlsec = MagicMock()
        mock_xmlsec.tree.find_node.return_value = None  # no signature node
        mock_xmlsec.constants.NodeSignature = "Signature"

        with (
            patch.dict("sys.modules", {"xmlsec": mock_xmlsec}),
            patch("lxml.etree.fromstring", return_value=MagicMock()),
        ):
            result = await provider.validate_signature(b"<xml/>", "cert")

        assert result is False


# ---------------------------------------------------------------------------
# SAMLAuthProvider.build_auth_request tests
# ---------------------------------------------------------------------------


class TestBuildAuthRequest:
    @pytest.mark.asyncio
    async def test_build_auth_request_returns_redirect_url(self) -> None:
        provider = SAMLAuthProvider(sp_entity_id="https://sp.example.com")
        result = await provider.build_auth_request(SAMPLE_IDP_CONFIG)

        assert "redirect_url" in result
        assert result["redirect_url"].startswith("https://idp.example.com/sso")
        assert "SAMLRequest=" in result["redirect_url"]
        assert "RelayState=" in result["redirect_url"]

    @pytest.mark.asyncio
    async def test_build_auth_request_has_unique_id(self) -> None:
        provider = SAMLAuthProvider()
        r1 = await provider.build_auth_request(SAMPLE_IDP_CONFIG)
        r2 = await provider.build_auth_request(SAMPLE_IDP_CONFIG)

        assert r1["request_id"] != r2["request_id"]

    @pytest.mark.asyncio
    async def test_build_auth_request_uses_provided_relay_state(self) -> None:
        provider = SAMLAuthProvider()
        result = await provider.build_auth_request(
            SAMPLE_IDP_CONFIG, relay_state="my-custom-state"
        )

        assert result["relay_state"] == "my-custom-state"
        assert "my-custom-state" in result["redirect_url"]


# ---------------------------------------------------------------------------
# Multi-IdP per tenant tests
# ---------------------------------------------------------------------------


class TestMultiIdPPerTenant:
    """Ensure each tenant can hold multiple distinct IdP configurations."""

    def _make_idp(self, entity_id: str) -> IdPConfiguration:
        return IdPConfiguration(
            idp_id=uuid.uuid4(),
            tenant_id=_TENANT_ID,
            entity_id=entity_id,
            sso_url=f"https://{entity_id}/sso",
            certificate_pem="",
            group_role_mapping={"admins": "admin"},
        )

    @pytest.mark.asyncio
    async def test_two_idps_produce_different_auth_requests(self) -> None:
        provider = SAMLAuthProvider()
        idp1 = self._make_idp("idp1.example.com")
        idp2 = self._make_idp("idp2.example.com")

        r1 = await provider.build_auth_request(idp1)
        r2 = await provider.build_auth_request(idp2)

        assert "idp1.example.com" in r1["redirect_url"]
        assert "idp2.example.com" in r2["redirect_url"]
        assert r1["request_id"] != r2["request_id"]

    @pytest.mark.asyncio
    async def test_each_idp_enforces_its_own_entity_id(self) -> None:
        """Assertion from idp1 should be rejected by idp2's config."""
        provider = SAMLAuthProvider()
        idp1 = self._make_idp("https://idp1.example.com")
        idp2 = self._make_idp("https://idp2.example.com")

        xml = _make_saml_response(issuer="https://idp1.example.com")

        # Valid against idp1
        r = await provider.parse_saml_response(xml, idp1)
        assert r["name_id"] == "alice@example.com"

        # Rejected by idp2 (entity_id mismatch)
        with pytest.raises(SAMLValidationError, match="Issuer"):
            await provider.parse_saml_response(xml, idp2)


# ---------------------------------------------------------------------------
# IdPConfig model tests
# ---------------------------------------------------------------------------


class TestIdPConfigModel:
    def test_model_fields_exist(self) -> None:
        from src.models.idp_config import IdPConfig, IdPProviderType

        # Verify model has all required columns via mapper inspection
        from sqlalchemy import inspect as sa_inspect

        mapper = sa_inspect(IdPConfig)
        columns = {c.key for c in mapper.columns}

        required = {
            "id",
            "tenant_id",
            "provider_type",
            "entity_id",
            "sso_url",
            "slo_url",
            "certificate_pem",
            "metadata_xml",
            "group_role_mapping",
            "enabled",
            "created_at",
            "updated_at",
        }
        assert required.issubset(columns)

    def test_provider_type_enum(self) -> None:
        from src.models.idp_config import IdPProviderType

        assert IdPProviderType.OIDC == "oidc"
        assert IdPProviderType.SAML == "saml"

    def test_idp_config_repr(self) -> None:
        from src.models.idp_config import IdPConfig, IdPProviderType

        config = IdPConfig()
        config.id = uuid.uuid4()
        config.tenant_id = uuid.uuid4()
        config.provider_type = IdPProviderType.SAML
        config.entity_id = "https://idp.test"

        repr_str = repr(config)
        assert "IdPConfig" in repr_str
        assert "saml" in repr_str


# ---------------------------------------------------------------------------
# SSO API endpoint tests
# ---------------------------------------------------------------------------


@pytest.fixture
def sso_app():
    """Build a minimal FastAPI app with the SSO router and stubbed auth + DB."""
    from fastapi import FastAPI
    from src.api.sso import router as sso_router
    from src.auth.dependencies import get_current_user, require_role
    from src.database import get_db_session
    from src.models.user import User, UserRole

    app = FastAPI()

    stub_tenant_id = _TENANT_ID
    stub_user = MagicMock()
    stub_user.id = uuid.uuid4()
    stub_user.tenant_id = stub_tenant_id
    stub_user.role = UserRole.ADMIN
    stub_user.is_active = True

    from src.auth.dependencies import AuthenticatedUser

    stub_auth_user = AuthenticatedUser(user=stub_user, claims={})

    # Override all auth dependencies
    app.dependency_overrides[get_current_user] = lambda: stub_auth_user

    # Override require_role for admin - return same user
    def _admin_override():
        return stub_auth_user

    from src.models.user import UserRole as UR

    app.dependency_overrides[require_role(UR.ADMIN)] = _admin_override

    # Mock DB session
    mock_db = AsyncMock()

    async def _get_mock_db():
        yield mock_db

    app.dependency_overrides[get_db_session] = _get_mock_db
    app.include_router(sso_router, prefix="/api/v1")

    return app, mock_db, stub_tenant_id


@pytest.mark.asyncio
async def test_list_providers_returns_empty_list(sso_app):
    import httpx
    from httpx import AsyncClient
    from src.models.idp_config import IdPConfig

    app, mock_db, tenant_id = sso_app

    # Mock DB to return empty result
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_db.execute = AsyncMock(return_value=mock_result)

    async with AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(f"/api/v1/sso/providers/{tenant_id}")

    assert response.status_code == 200
    data = response.json()
    assert data["providers"] == []
    assert data["total"] == 0


@pytest.mark.asyncio
async def test_create_provider_returns_201(sso_app):
    import httpx
    from httpx import AsyncClient
    from src.models.idp_config import IdPConfig, IdPProviderType

    app, mock_db, tenant_id = sso_app

    # First execute() is the uniqueness check (returns None = not found)
    mock_unique_result = MagicMock()
    mock_unique_result.scalar_one_or_none.return_value = None

    # flush() should not raise
    mock_db.flush = AsyncMock()

    # Simulate the ORM object that gets returned
    created_orm = MagicMock(spec=IdPConfig)
    created_orm.id = uuid.uuid4()
    created_orm.tenant_id = tenant_id
    created_orm.provider_type = "saml"
    created_orm.entity_id = "https://new-idp.example.com"
    created_orm.sso_url = "https://new-idp.example.com/sso"
    created_orm.slo_url = None
    created_orm.group_role_mapping = {}
    created_orm.enabled = True
    created_orm.created_at = datetime.now(timezone.utc)
    created_orm.updated_at = datetime.now(timezone.utc)

    mock_db.execute = AsyncMock(return_value=mock_unique_result)
    mock_db.add = MagicMock()

    # Patch model_validate to avoid full ORM round-trip
    with patch("src.api.sso.IdPConfigResponse.model_validate", return_value={
        "id": str(created_orm.id),
        "tenant_id": str(tenant_id),
        "provider_type": "saml",
        "entity_id": "https://new-idp.example.com",
        "sso_url": "https://new-idp.example.com/sso",
        "slo_url": None,
        "group_role_mapping": {},
        "enabled": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }):
        async with AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                f"/api/v1/sso/providers/{tenant_id}",
                json={
                    "provider_type": "saml",
                    "entity_id": "https://new-idp.example.com",
                    "sso_url": "https://new-idp.example.com/sso",
                    "group_role_mapping": {},
                },
            )

    assert response.status_code == 201


@pytest.mark.asyncio
async def test_delete_provider_returns_204(sso_app):
    import httpx
    from httpx import AsyncClient
    from src.models.idp_config import IdPConfig

    app, mock_db, tenant_id = sso_app
    provider_id = uuid.uuid4()

    mock_orm = MagicMock(spec=IdPConfig)
    mock_orm.id = provider_id
    mock_orm.tenant_id = tenant_id

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_orm
    mock_db.execute = AsyncMock(return_value=mock_result)
    mock_db.delete = AsyncMock()

    async with AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.delete(
            f"/api/v1/sso/providers/{tenant_id}/{provider_id}"
        )

    assert response.status_code == 204


@pytest.mark.asyncio
async def test_saml_login_redirects(sso_app):
    """GET /saml/login/{tenant_id} should redirect to IdP SSO URL."""
    import httpx
    from httpx import AsyncClient
    from src.models.idp_config import IdPConfig, IdPProviderType

    app, mock_db, tenant_id = sso_app

    mock_orm = MagicMock(spec=IdPConfig)
    mock_orm.id = _IDP_ID
    mock_orm.tenant_id = tenant_id
    mock_orm.entity_id = "https://idp.example.com"
    mock_orm.sso_url = "https://idp.example.com/sso"
    mock_orm.slo_url = None
    mock_orm.certificate_pem = ""
    mock_orm.group_role_mapping = {}

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_orm
    mock_db.execute = AsyncMock(return_value=mock_result)

    async with AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        response = await client.get(f"/api/v1/sso/saml/login/{tenant_id}")

    # Should be a redirect
    assert response.status_code in (302, 307)
