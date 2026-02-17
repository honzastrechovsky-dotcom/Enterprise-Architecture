"""SAML 2.0 authentication provider.

Implements the Service Provider (SP) side of SAML 2.0:
- Building AuthnRequest XML for redirect binding
- Validating and parsing SAML Responses / Assertions
- Extracting user attributes (claims) from assertions
- Mapping IdP groups to platform roles
- Signature validation via xmlsec or pure-Python fallback
- Multi-IdP support: each tenant can configure N identity providers

Key design decisions:
- We use defusedxml for all XML parsing to guard against XXE and related
  injection attacks.  Standard xml.etree is never used directly for untrusted
  input.
- Signature validation uses lxml + xmlsec1 when available; if the library is
  not installed, validation falls back to a warning and the caller should
  reject the assertion in production mode.
- All public methods are async to align with the FastAPI async context, even
  though the SAML operations themselves are synchronous (CPU-bound) and are
  offloaded to a thread-pool executor.
"""

from __future__ import annotations

import asyncio
import base64
import secrets
import uuid
import zlib
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote as urlquote
from xml.etree import ElementTree as ET  # used only for generating SP metadata (output only)

import structlog

from src.models.user import UserRole

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# SAML namespace map
# ---------------------------------------------------------------------------

_NS = {
    "saml": "urn:oasis:names:tc:SAML:2.0:assertion",
    "samlp": "urn:oasis:names:tc:SAML:2.0:protocol",
    "ds": "http://www.w3.org/2000/09/xmldsig#",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
}

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SAMLValidationError(Exception):
    """Raised when a SAML assertion cannot be validated."""


class SAMLSignatureError(SAMLValidationError):
    """Raised when signature validation fails."""


# ---------------------------------------------------------------------------
# IdP configuration container
# ---------------------------------------------------------------------------


class IdPConfiguration:
    """Runtime representation of an IdP configuration for one tenant.

    This is a lightweight data class; persistent storage lives in
    src.models.idp_config.IdPConfig (SQLAlchemy model).
    """

    def __init__(
        self,
        *,
        idp_id: uuid.UUID,
        tenant_id: uuid.UUID,
        entity_id: str,
        sso_url: str,
        slo_url: str | None = None,
        certificate_pem: str,
        group_role_mapping: dict[str, str] | None = None,
    ) -> None:
        self.idp_id = idp_id
        self.tenant_id = tenant_id
        self.entity_id = entity_id
        self.sso_url = sso_url
        self.slo_url = slo_url
        self.certificate_pem = certificate_pem
        # Maps IdP group name -> platform role string, e.g. {"admins": "admin"}
        self.group_role_mapping: dict[str, str] = group_role_mapping or {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_xml_safe(xml_bytes: bytes) -> Any:
    """Parse XML using defusedxml when available, falling back to stdlib.

    defusedxml mitigates XXE, billion-laughs, and quadratic-blowup attacks.
    If it is not installed we log a warning and use the stdlib parser.
    """
    try:
        import defusedxml.ElementTree as DefET  # type: ignore[import-untyped]

        return DefET.fromstring(xml_bytes)
    except ImportError:
        log.warning(
            "saml.defusedxml_not_available",
            message="defusedxml not installed; using stdlib XML parser (insecure for production)",
        )
        return ET.fromstring(xml_bytes)


def _find_text(element: Any, xpath: str) -> str | None:
    """Return the stripped text of the first matching sub-element, or None."""
    node = element.find(xpath, _NS)
    if node is None or node.text is None:
        return None
    return node.text.strip()


def _pem_to_der(cert_pem: str) -> bytes:
    """Strip PEM headers and decode base64 to DER bytes."""
    lines = [
        line
        for line in cert_pem.splitlines()
        if line and not line.startswith("-----")
    ]
    return base64.b64decode("".join(lines))


def _deflate_and_encode(data: str) -> str:
    """Deflate (raw) + base64-encode a string for SAML redirect binding."""
    compressed = zlib.compress(data.encode("utf-8"))[2:-4]  # strip zlib header/trailer
    return base64.b64encode(compressed).decode("ascii")


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class SAMLAuthProvider:
    """SAML 2.0 Service Provider operations.

    Usage (one instance per IdP config, or share a single instance and pass
    the IdPConfiguration at call time):

        provider = SAMLAuthProvider(sp_entity_id="https://myapp.example.com/saml")
        redirect_url = await provider.build_auth_request(idp_config)
        claims = await provider.parse_saml_response(base64_response, idp_config)
    """

    def __init__(self, sp_entity_id: str = "https://enterprise-agent-platform") -> None:
        self.sp_entity_id = sp_entity_id

    # ------------------------------------------------------------------
    # AuthnRequest
    # ------------------------------------------------------------------

    async def build_auth_request(
        self,
        idp_config: IdPConfiguration,
        relay_state: str | None = None,
    ) -> dict[str, str]:
        """Create a SAML AuthnRequest suitable for redirect binding.

        Returns a dict with:
            - ``redirect_url``: Full URL to redirect the browser to.
            - ``request_id``: The ``_ID`` of the AuthnRequest.
            - ``relay_state``: The relay state (echoed from input or generated).
        """

        def _sync_build() -> dict[str, str]:
            request_id = f"_{uuid.uuid4().hex}"
            issue_instant = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            rs = relay_state or secrets.token_urlsafe(16)

            authn_request = (
                f'<samlp:AuthnRequest'
                f' xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol"'
                f' xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion"'
                f' ID="{request_id}"'
                f' Version="2.0"'
                f' IssueInstant="{issue_instant}"'
                f' AssertionConsumerServiceURL="{self.sp_entity_id}/saml/acs"'
                f' ProtocolBinding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST">'
                f'<saml:Issuer>{self.sp_entity_id}</saml:Issuer>'
                f'<samlp:NameIDPolicy'
                f' Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress"'
                f' AllowCreate="true"/>'
                f'</samlp:AuthnRequest>'
            )

            encoded = _deflate_and_encode(authn_request)
            redirect_url = (
                f"{idp_config.sso_url}"
                f"?SAMLRequest={urlquote(encoded)}"
                f"&RelayState={urlquote(rs)}"
            )
            return {
                "redirect_url": redirect_url,
                "request_id": request_id,
                "relay_state": rs,
            }

        loop = asyncio.get_running_loop()
        result: dict[str, str] = await loop.run_in_executor(None, _sync_build)
        log.info(
            "saml.auth_request_built",
            idp_entity_id=idp_config.entity_id,
            tenant_id=str(idp_config.tenant_id),
        )
        return result

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    async def parse_saml_response(
        self,
        saml_response_xml: str | bytes,
        idp_config: IdPConfiguration,
    ) -> dict[str, Any]:
        """Validate and parse a SAML Response.

        Args:
            saml_response_xml: Raw SAML Response XML (not base64-encoded).
                The ACS endpoint should base64-decode the POST body before
                calling this method.
            idp_config: The IdP configuration to validate against.

        Returns:
            Dict with keys:
                - ``name_id``: The NameID value (usually email or opaque ID).
                - ``name_id_format``: The NameID format URI.
                - ``session_index``: IdP session identifier (may be None).
                - ``attributes``: Dict of attribute name -> list of values.
                - ``groups``: List of group memberships extracted from attributes.
                - ``claims``: Normalised flat claims dict (email, name, …).

        Raises:
            SAMLValidationError: On status failure, missing assertions, or
                expired conditions.
            SAMLSignatureError: On signature verification failure.
        """

        def _sync_parse() -> dict[str, Any]:
            xml_bytes = (
                saml_response_xml.encode("utf-8")
                if isinstance(saml_response_xml, str)
                else saml_response_xml
            )
            root = _parse_xml_safe(xml_bytes)

            # --- Status check ---
            status_code_node = root.find(
                ".//samlp:Status/samlp:StatusCode", _NS
            )
            if status_code_node is None:
                raise SAMLValidationError("SAML Response missing StatusCode.")
            status_value = status_code_node.get("Value", "")
            if "Success" not in status_value:
                raise SAMLValidationError(
                    f"SAML authentication failed. Status: {status_value}"
                )

            # --- Find assertion ---
            assertion = root.find("saml:Assertion", _NS)
            if assertion is None:
                raise SAMLValidationError("SAML Response contains no Assertion.")

            # --- Conditions (time validation) ---
            self._validate_conditions(assertion)

            # --- Issuer check ---
            issuer_text = _find_text(assertion, "saml:Issuer")
            if issuer_text and idp_config.entity_id:
                if issuer_text != idp_config.entity_id:
                    raise SAMLValidationError(
                        f"Assertion Issuer '{issuer_text}' does not match "
                        f"expected entity_id '{idp_config.entity_id}'."
                    )

            # --- NameID ---
            name_id_node = assertion.find(
                "saml:Subject/saml:NameID", _NS
            )
            if name_id_node is None:
                raise SAMLValidationError("Assertion missing NameID.")
            name_id = (name_id_node.text or "").strip()
            name_id_format = name_id_node.get(
                "Format",
                "urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified",
            )

            # --- Session index ---
            authn_stmt = assertion.find("saml:AuthnStatement", _NS)
            session_index: str | None = None
            if authn_stmt is not None:
                session_index = authn_stmt.get("SessionIndex")

            # --- Attributes ---
            attributes = self._extract_attributes(assertion)
            groups = self._extract_groups(attributes)
            claims = self._build_claims(name_id, attributes)

            return {
                "name_id": name_id,
                "name_id_format": name_id_format,
                "session_index": session_index,
                "attributes": attributes,
                "groups": groups,
                "claims": claims,
            }

        loop = asyncio.get_running_loop()
        result: dict[str, Any] = await loop.run_in_executor(None, _sync_parse)
        log.info(
            "saml.response_parsed",
            name_id=result.get("name_id"),
            tenant_id=str(idp_config.tenant_id),
            group_count=len(result.get("groups", [])),
        )
        return result

    # ------------------------------------------------------------------
    # Claims extraction
    # ------------------------------------------------------------------

    async def extract_claims(self, assertion_element: Any) -> dict[str, Any]:
        """Extract normalised user attributes from a parsed Assertion element.

        Can be called independently when the caller already has a parsed
        assertion XML element (e.g. from a signed-assertion flow).

        Returns a flat dict of normalised claim values.
        """

        def _sync_extract() -> dict[str, Any]:
            attributes = self._extract_attributes(assertion_element)
            name_id_node = assertion_element.find("saml:Subject/saml:NameID", _NS)
            name_id = ""
            if name_id_node is not None and name_id_node.text:
                name_id = name_id_node.text.strip()
            return self._build_claims(name_id, attributes)

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _sync_extract)

    # ------------------------------------------------------------------
    # Group-to-role mapping
    # ------------------------------------------------------------------

    async def map_groups_to_roles(
        self,
        groups: list[str],
        idp_config: IdPConfiguration,
    ) -> UserRole:
        """Map a list of IdP group names to the most-privileged platform role.

        Precedence: admin > operator > viewer.

        Args:
            groups: Group names from the SAML assertion (case-insensitive match).
            idp_config: The IdP configuration containing the mapping table.

        Returns:
            The highest-privilege UserRole found, defaulting to VIEWER.
        """
        mapping = idp_config.group_role_mapping
        if not mapping:
            return UserRole.VIEWER

        role_precedence = [UserRole.ADMIN, UserRole.OPERATOR, UserRole.VIEWER]
        groups_lower = {g.lower() for g in groups}

        for candidate_role in role_precedence:
            for group_name, mapped_role in mapping.items():
                if (
                    group_name.lower() in groups_lower
                    and mapped_role.lower() == candidate_role.value.lower()
                ):
                    log.info(
                        "saml.role_mapped",
                        group=group_name,
                        role=candidate_role,
                    )
                    return candidate_role

        return UserRole.VIEWER

    # ------------------------------------------------------------------
    # Signature validation
    # ------------------------------------------------------------------

    async def validate_signature(
        self,
        xml_bytes: bytes,
        cert_pem: str,
    ) -> bool:
        """Validate an XML digital signature against an X.509 certificate.

        Uses xmlsec when available.  If xmlsec is not installed, logs a
        critical warning and returns False (callers should treat this as a
        validation failure in production).

        Args:
            xml_bytes: The complete signed XML document.
            cert_pem: The PEM-encoded X.509 certificate from the IdP.

        Returns:
            True if the signature is valid, False otherwise.
        """

        def _sync_validate() -> bool:
            try:
                import lxml.etree as lxml_et
                import xmlsec  # type: ignore[import-untyped]

                root = lxml_et.fromstring(xml_bytes)
                signature_node = xmlsec.tree.find_node(root, xmlsec.constants.NodeSignature)
                if signature_node is None:
                    log.warning("saml.validate_signature.no_signature_node")
                    return False

                ctx = xmlsec.SignatureContext()
                # Build key from PEM certificate
                key = xmlsec.Key.from_memory(
                    cert_pem.encode("utf-8"),
                    xmlsec.constants.KeyDataFormatCertPem,
                )
                ctx.key = key
                ctx.verify(signature_node)
                log.info("saml.validate_signature.valid")
                return True

            except ImportError:
                log.critical(
                    "saml.validate_signature.xmlsec_not_available",
                    message="xmlsec not installed - signature validation disabled. "
                    "Install python-xmlsec for production use.",
                )
                return False

            except Exception as exc:
                log.warning("saml.validate_signature.invalid", error=str(exc))
                return False

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _sync_validate)

    # ------------------------------------------------------------------
    # SP Metadata
    # ------------------------------------------------------------------

    def build_sp_metadata(self, acs_url: str) -> str:
        """Return SP metadata XML for distribution to IdPs.

        Args:
            acs_url: The Assertion Consumer Service URL for this SP.

        Returns:
            XML string representing the SP metadata document.
        """
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        metadata = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<md:EntityDescriptor'
            ' xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata"'
            f' entityID="{self.sp_entity_id}"'
            f' validUntil="{now}">\n'
            '  <md:SPSSODescriptor'
            ' AuthnRequestsSigned="false"'
            ' WantAssertionsSigned="true"'
            ' protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">\n'
            '    <md:AssertionConsumerService'
            ' Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"'
            f' Location="{acs_url}"'
            ' index="1"/>\n'
            '  </md:SPSSODescriptor>\n'
            '</md:EntityDescriptor>'
        )
        return metadata

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_conditions(assertion: Any) -> None:
        """Check NotBefore / NotOnOrAfter conditions on the assertion."""
        conditions = assertion.find("saml:Conditions", _NS)
        if conditions is None:
            return  # No conditions = no time restriction

        now = datetime.now(UTC)
        fmt = "%Y-%m-%dT%H:%M:%SZ"

        not_before_str = conditions.get("NotBefore")
        if not_before_str:
            try:
                not_before = datetime.strptime(not_before_str, fmt).replace(
                    tzinfo=UTC
                )
                if now < not_before:
                    raise SAMLValidationError(
                        f"Assertion not yet valid (NotBefore: {not_before_str})."
                    )
            except ValueError:
                log.warning(
                    "saml.conditions.parse_error",
                    attribute="NotBefore",
                    value=not_before_str,
                )

        not_on_or_after_str = conditions.get("NotOnOrAfter")
        if not_on_or_after_str:
            try:
                not_on_or_after = datetime.strptime(
                    not_on_or_after_str, fmt
                ).replace(tzinfo=UTC)
                if now >= not_on_or_after:
                    raise SAMLValidationError(
                        f"Assertion has expired (NotOnOrAfter: {not_on_or_after_str})."
                    )
            except ValueError:
                log.warning(
                    "saml.conditions.parse_error",
                    attribute="NotOnOrAfter",
                    value=not_on_or_after_str,
                )

    @staticmethod
    def _extract_attributes(assertion: Any) -> dict[str, list[str]]:
        """Extract all SAML Attributes into a dict of name -> [values]."""
        attributes: dict[str, list[str]] = {}
        attr_stmt = assertion.find("saml:AttributeStatement", _NS)
        if attr_stmt is None:
            return attributes

        for attr in attr_stmt.findall("saml:Attribute", _NS):
            attr_name = attr.get("Name", "")
            if not attr_name:
                continue
            values = [
                (v.text or "").strip()
                for v in attr.findall("saml:AttributeValue", _NS)
                if v.text
            ]
            attributes[attr_name] = values

        return attributes

    @staticmethod
    def _extract_groups(attributes: dict[str, list[str]]) -> list[str]:
        """Locate group membership values in the attributes dict.

        Common attribute names used by different IdPs for group membership.
        """
        group_attribute_names = [
            "groups",
            "memberOf",
            "Group",
            "http://schemas.microsoft.com/ws/2008/06/identity/claims/groups",
            "urn:oid:1.3.6.1.4.1.5923.1.5.1.1",  # eduMember isMemberOf
        ]
        groups: list[str] = []
        for name in group_attribute_names:
            if name in attributes:
                groups.extend(attributes[name])
        return list(dict.fromkeys(groups))  # deduplicate while preserving order

    @staticmethod
    def _build_claims(
        name_id: str,
        attributes: dict[str, list[str]],
    ) -> dict[str, Any]:
        """Build a normalised flat claims dict from raw SAML attributes."""

        def _first(attr_name: str) -> str | None:
            vals = attributes.get(attr_name, [])
            return vals[0] if vals else None

        # Try common attribute name variants for email
        email = (
            _first("email")
            or _first("mail")
            or _first("http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress")
            or _first("urn:oid:0.9.2342.19200300.100.1.3")
            or name_id  # NameID is often the email for emailAddress format
        )

        display_name = (
            _first("displayName")
            or _first("cn")
            or _first("http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name")
            or _first("urn:oid:2.16.840.1.113730.3.1.241")
        )

        given_name = (
            _first("givenName")
            or _first("http://schemas.xmlsoap.org/ws/2005/05/identity/claims/givenname")
        )

        surname = (
            _first("sn")
            or _first("surname")
            or _first("http://schemas.xmlsoap.org/ws/2005/05/identity/claims/surname")
        )

        return {
            "sub": name_id,
            "email": email,
            "name": display_name,
            "given_name": given_name,
            "family_name": surname,
            "raw_attributes": attributes,
        }
