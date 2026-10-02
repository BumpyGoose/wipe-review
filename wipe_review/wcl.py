"""Warcraft Logs API v2 client: OAuth2 client-credentials auth (public data
only, no user login) and GraphQL queries - see
https://www.warcraftlogs.com/api/docs. Standard library only."""

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CREDENTIALS_PATH = ROOT / "credentials.local.json"
TOKEN_CACHE_PATH = ROOT / "token-cache.local.json"
TOKEN_URL = "https://www.warcraftlogs.com/oauth/token"
API_URL = "https://www.warcraftlogs.com/api/v2/client"


class WclError(Exception):
    pass


class MissingCredentials(WclError):
    pass


def load_credentials():
    if not CREDENTIALS_PATH.exists():
        raise MissingCredentials(
            f"Missing {CREDENTIALS_PATH.name} - create a client at https://www.warcraftlogs.com/api/clients/ "
            "and save its client_id/client_secret there.")
    creds = json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
    if not creds.get("client_id") or not creds.get("client_secret"):
        raise MissingCredentials(f"{CREDENTIALS_PATH.name} needs client_id and client_secret.")
    return creds


def save_credentials(client_id, client_secret):
    CREDENTIALS_PATH.write_text(json.dumps({"client_id": client_id, "client_secret": client_secret}, indent=2), encoding="utf-8")
    TOKEN_CACHE_PATH.unlink(missing_ok=True)


def _post(url, data, headers, timeout=60):
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        err = WclError(f"HTTP {e.code} from Warcraft Logs: {body}")
        err.status = e.code
        raise err from None
    except urllib.error.URLError as e:
        raise WclError(f"Couldn't reach Warcraft Logs: {e.reason}") from None


class WclClient:
    def __init__(self):
        self._token = None
        self._expires_at = 0

    def _get_token(self, force=False):
        now = time.time()
        if not force and self._token and self._expires_at - now > 300:
            return self._token
        if not force and TOKEN_CACHE_PATH.exists():
            try:
                cached = json.loads(TOKEN_CACHE_PATH.read_text(encoding="utf-8"))
                if cached["expires_at"] - now > 300:
                    self._token, self._expires_at = cached["access_token"], cached["expires_at"]
                    return self._token
            except (ValueError, KeyError, OSError):
                pass  # corrupt cache - fetch a fresh token

        creds = load_credentials()
        basic = base64.b64encode(f"{creds['client_id']}:{creds['client_secret']}".encode()).decode()
        resp = _post(TOKEN_URL, urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
                     {"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"})
        self._token = resp["access_token"]
        self._expires_at = now + resp["expires_in"]
        TOKEN_CACHE_PATH.write_text(json.dumps({"access_token": self._token, "expires_at": self._expires_at}), encoding="utf-8")
        return self._token

    def query(self, query, variables=None):
        body = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
        for attempt in (0, 1):
            headers = {"Authorization": f"Bearer {self._get_token(force=attempt > 0)}", "Content-Type": "application/json"}
            try:
                resp = _post(API_URL, body, headers)
                break
            except WclError as e:
                if getattr(e, "status", None) == 401 and attempt == 0:
                    continue  # token revoked/expired early - retry once with a fresh one
                raise
        if resp.get("errors"):
            raise WclError("WCL query failed: " + "; ".join(e.get("message", "?") for e in resp["errors"]))
        return resp["data"]

    def events(self, code, items):
        """Fetch several event streams in one request.

        Each item is a dict: alias, dataType, start, end and optionally
        sourceID, fightIDs, hostility, filter, resources. They become aliased
        events() fields of one GraphQL query; any alias that comes back with a
        nextPageTimestamp is then paged on its own until complete.
        Returns {alias: [event, ...]}.
        """
        result = {it["alias"]: [] for it in items}
        pending = list(items)
        while pending:
            variables = {"code": code}
            decl = ["$code: String!"]
            fields = []
            for i, it in enumerate(pending):
                args = [f"dataType: {it['dataType']}", f"startTime: {int(it['start'])}", f"endTime: {int(it['end'])}", "limit: 10000"]
                if it.get("sourceID") is not None:
                    args.append(f"sourceID: {it['sourceID']}")
                if it.get("fightIDs"):
                    args.append(f"fightIDs: [{','.join(str(f) for f in it['fightIDs'])}]")
                if it.get("hostility"):
                    args.append(f"hostilityType: {it['hostility']}")
                if it.get("resources"):
                    args.append("includeResources: true")
                if it.get("filter"):
                    variables[f"f{i}"] = it["filter"]
                    decl.append(f"$f{i}: String")
                    args.append(f"filterExpression: $f{i}")
                fields.append(f"{it['alias']}: events({', '.join(args)}) {{ data nextPageTimestamp }}")
            q = f"query({', '.join(decl)}) {{ reportData {{ report(code: $code) {{ {' '.join(fields)} }} }} }}"
            report = self.query(q, variables)["reportData"]["report"]

            next_pending = []
            for it in pending:
                page = report.get(it["alias"]) or {}
                result[it["alias"]].extend(page.get("data") or [])
                if page.get("nextPageTimestamp"):
                    next_pending.append({**it, "start": page["nextPageTimestamp"]})
            pending = next_pending
        return result
