"""The scorecard App's installation tokens: one repository, the named permissions, nothing broader, the key never shown."""
import json
import urllib.error

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from golden_road import github_app
from golden_road.io import InputError

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()).decode()
PUB = KEY.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
TOKEN = "ghs_" + "a" * 36
# GitHub's stateless installation tokens (rolled out 2026-10-02): ghs_<app id>_<JWT>, about 520 characters, with dots.
STATELESS = "ghs_1234_eyJhbGciOiJSUzI1NiJ9." + "eyJpc3MiOjEyMzR9" * 20 + "." + "s-_" * 110
NOW = 1_800_000_000


class Resp:
    def __init__(self, doc, status=200):
        self.body, self.headers = json.dumps(doc).encode(), {"Link": ""}

    def read(self, n):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeGitHub:
    def __init__(self, installed=True, grant=None, repos=None, slug="scorecard-x", token=TOKEN):
        self.calls, self.installed, self.slug, self.token = [], installed, slug, token
        self.grant, self.repos = grant, repos

    def __call__(self, req, timeout):
        self.calls.append((req.get_method(), req.full_url, req.headers.get("Authorization"), req.data))
        if req.full_url == "https://api.github.com/app":
            if self.slug is None:
                raise urllib.error.HTTPError(req.full_url, 403, "x", {}, None)
            return Resp({"id": 1234, "slug": self.slug})
        if req.full_url.endswith("/installation"):
            if not self.installed:
                raise urllib.error.HTTPError(req.full_url, 404, "x", {}, None)
            return Resp({"id": 77})
        asked = json.loads(req.data)
        return Resp({"token": self.token, "expires_at": "2027-01-15T09:00:00Z",
                     "permissions": self.grant or {**asked["permissions"], "metadata": "read"},
                     "repositories": self.repos if self.repos is not None else [{"full_name": "o/app"}]}, 201)


def mint(fake, perms=None, **kw):
    return github_app.installation_token("https://api.github.com", 1234, PEM, "o/app", perms or {"contents": "read"},
                                         now=NOW, opener=fake, **kw)


def test_the_app_jwt_is_rs256_for_the_app_and_lives_under_ten_minutes():
    claims = jwt.decode(github_app.app_jwt(1234, PEM, NOW), PUB, algorithms=["RS256"], options={"verify_exp": False, "verify_iat": False})
    assert claims == {"iat": NOW - 60, "exp": NOW + 540, "iss": "1234"}


def test_the_token_is_for_one_repository_and_only_the_named_permissions():
    fake = FakeGitHub()
    doc = mint(fake, {"checks": "write", "pull_requests": "read"})
    (m0, u0, a0, _), (m1, u1, a1, _), (m2, u2, a2, body) = fake.calls
    assert (m0, u0) == ("GET", "https://api.github.com/app") and a0 == a1, "the App's slug is read once, with the App JWT"
    assert (m1, u1) == ("GET", "https://api.github.com/repos/o/app/installation")
    assert (m2, u2) == ("POST", "https://api.github.com/app/installations/77/access_tokens")
    assert a1 == a2 and a1.startswith("Bearer ey")  # the App JWT, never the key
    assert json.loads(body) == {"repositories": ["app"], "permissions": {"checks": "write", "pull_requests": "read"}}
    assert doc == {"token": TOKEN, "expires_at": "2027-01-15T09:00:00Z", "repository": "o/app",
                   "permissions": {"checks": "write", "pull_requests": "read"}, "app_slug": "scorecard-x"}


@pytest.mark.parametrize("grant,repos", [
    ({"contents": "read", "issues": "write", "metadata": "read"}, None),   # an extra permission
    ({"contents": "write", "metadata": "read"}, None),                     # write instead of read
    (None, [{"full_name": "o/app"}, {"full_name": "o/other"}]),            # a second repository
    (None, [{"full_name": "x/elsewhere"}]),                                # the wrong repository
    (None, []),                                                            # no repository at all
])
def test_a_token_broader_than_asked_for_is_refused(grant, repos):
    with pytest.raises(InputError, match="broader"):
        mint(FakeGitHub(grant=grant, repos=repos))


@pytest.mark.parametrize("pairs", [["administration=write"], ["contents=write"], ["workflows=write"], ["issues=read"],
                                   ["contents=read", "contents=read"], [], ["contents"]])
def test_permissions_outside_the_allowlist_are_refused_before_any_call(pairs):
    with pytest.raises(InputError):
        github_app.parse_permissions(pairs)


def test_an_app_not_installed_on_the_repo_says_so():
    with pytest.raises(InputError, match="not installed on o/app"):
        mint(FakeGitHub(installed=False))


@pytest.mark.parametrize("key", ["not a key", PEM.replace("A", "B", 40), "-----BEGIN RSA PRIVATE KEY-----\nAAAA\n-----END RSA PRIVATE KEY-----\n"])
def test_a_bad_key_is_refused_and_never_appears_in_the_message(key):
    with pytest.raises(InputError) as err:
        github_app.app_jwt(1234, key, NOW)
    assert key[:30] not in str(err.value) and "PRIVATE" not in str(err.value) and err.value.__cause__ is None


@pytest.mark.parametrize("repo", ["o", "o/..", "../o", "o/a/b", ""])
def test_odd_repositories_are_refused_before_any_call(repo):
    fake = FakeGitHub()
    with pytest.raises(InputError):
        github_app.installation_token("https://api.github.com", 1234, PEM, repo, {"contents": "read"}, now=NOW, opener=fake)
    assert fake.calls == []


@pytest.mark.parametrize("token", [TOKEN, STATELESS])
def test_both_the_old_and_the_stateless_token_formats_are_accepted(token):
    assert mint(FakeGitHub(token=token))["token"] == token


@pytest.mark.parametrize("doc", [{"token": "not-a-token", "expires_at": "2027-01-15T09:00:00Z"},
                                 {"token": TOKEN, "expires_at": "tomorrow"}, {},
                                 {"token": STATELESS[:100] + "\nnot-a-token", "expires_at": "2027-01-15T09:00:00Z"},
                                 {"token": STATELESS[:100] + " not-a-token", "expires_at": "2027-01-15T09:00:00Z"},
                                 {"token": "ghs_" + "a" * 4097, "expires_at": "2027-01-15T09:00:00Z"}])
def test_an_odd_token_answer_is_refused_and_not_echoed(doc):
    def odd(req, timeout):
        return Resp({"id": 77}) if req.full_url.endswith("/installation") else Resp(doc)
    with pytest.raises(InputError) as err:
        mint(odd)
    assert "not-a-token" not in str(err.value)


def test_the_token_file_is_private_and_never_replaced(tmp_path):
    path = tmp_path / "t.json"
    github_app.write_token(path, {"token": TOKEN})
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        github_app.write_token(path, {"token": "other"})


@pytest.mark.parametrize("slug", [None, "", "Has Space", "../x", "UPPER", "a" * 40, 5])
def test_an_unreadable_or_odd_slug_is_refused_before_a_token_is_minted(slug):
    fake = FakeGitHub(slug=slug)
    if slug in ("", "Has Space", "../x", "UPPER", "a" * 40, 5):
        original = fake.__call__

        def odd(req, timeout):
            return Resp({"slug": slug}) if req.full_url == "https://api.github.com/app" else original(req, timeout)
        fake = odd
    with pytest.raises(InputError):
        mint(fake)


def test_contents_write_is_only_the_deploy_apps_pin_set():
    # golden-road-deploy's bump workflow: a pin branch and its PR, nothing else. contents=write alone, or with anything else, is refused.
    assert github_app.parse_permissions(["contents=write", "pull_requests=write"], "acme/golden-road-deploy") == {"contents": "write", "pull_requests": "write"}
    for repo in (None, "acme/hello-app", "acme/golden-road-deploy-x", "acme/not-golden-road-deploy/x"):
        with pytest.raises(InputError, match="golden-road-deploy"):
            github_app.parse_permissions(["contents=write", "pull_requests=write"], repo)
    for pairs in (["contents=write"], ["contents=write", "pull_requests=read"], ["contents=write", "pull_requests=write", "checks=write"],
                  ["contents=write", "issues=write"]):
        with pytest.raises(InputError):
            github_app.parse_permissions(pairs, "acme/golden-road-deploy")


def test_the_token_call_refuses_contents_write_for_any_other_repo_before_any_call():
    with pytest.raises(InputError, match="golden-road-deploy"):
        github_app.installation_token("https://api.github.com", 1234, "unused", "o/app", {"contents": "write", "pull_requests": "write"},
                                      opener=lambda *a, **k: pytest.fail("no call may be made"))
