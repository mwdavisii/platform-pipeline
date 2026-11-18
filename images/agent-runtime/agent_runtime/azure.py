"""The app's Azure workload identity (its binding's azure_identity): an Entra token for one scope, from the federated token the cluster's
webhook projects into the pod (AZURE_FEDERATED_TOKEN_FILE), as azure-identity's WorkloadIdentityCredential gets one. A token is kept
until a minute before it expires and never longer than an hour (token freshness, not a budget). The file is read for each new token
(the kubelet rotates it)."""

import time
from pathlib import Path

import httpx

from .tools import ToolError

ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
KEEP_AT_MOST = 3600  # seconds a token is kept, whatever expires_in says


class Tokens:
    def __init__(self, azure, http, clock=time.time):
        self.azure, self.http, self.clock, self._cache = azure, http, clock, {}

    def token(self, scope: str) -> str:
        cached = self._cache.get(scope)
        if cached and cached[1] > self.clock():
            return cached[0]
        try:
            assertion = Path(self.azure.token_file).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            raise ToolError("the app's identity token file can't be read") from None
        try:
            response = self.http.post(f"{self.azure.authority}{self.azure.tenant_id}/oauth2/v2.0/token", timeout=None, data={
                "client_id": self.azure.client_id, "scope": scope, "grant_type": "client_credentials",
                "client_assertion_type": ASSERTION_TYPE, "client_assertion": assertion})
        except httpx.HTTPError as exc:
            raise ToolError(f"the identity's token endpoint could not be reached ({type(exc).__name__})") from None
        if response.status_code != 200:
            raise ToolError(f"the app's identity was refused a token ({response.status_code})")
        try:
            doc = response.json()
            token = doc["access_token"]
        except (ValueError, KeyError, TypeError):
            raise ToolError("the identity's token answer has no token") from None
        if not isinstance(token, str) or not token or any(c.isspace() for c in token):
            raise ToolError("the identity's token answer has no token")
        try:
            lifetime = int(doc.get("expires_in", 0))
        except (ValueError, TypeError):
            lifetime = 0  # an expiry that can't be read: the token is used once, not kept
        self._cache[scope] = (token, self.clock() + min(max(lifetime - 60, 0), KEEP_AT_MOST))
        return token
