"""Tests for the per-MCP tool-call permission policy (allow / deny / elicit).

Three layers:
  * config — `PermissionPolicy` parsing, precedence, global⊕server⊕auto_approve
    merge, and the off-by-default guarantee.
  * runtime — the per-session elicitation grant cache.
  * middleware — `ToolProcessingMiddleware._enforce_permission` / `_elicit_permission`:
    allow/deny surfaces, elicit accept-once / accept-session / decline / cancel,
    session-grant reuse, and the elicit-unavailable fallback.

All hermetic — no subprocesses, no real MCP client.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp.exceptions import ToolError
from fastmcp.server.elicitation import (
    AcceptedElicitation,
    CancelledElicitation,
    DeclinedElicitation,
)

from mcp_combiner import permissions
from mcp_combiner.config import (
    CombinerConfig,
    PermissionAction,
    PermissionPolicy,
    ResolvedPolicy,
    ServerConfig,
    _merge_policies,
)
from mcp_combiner.runtime import SessionRegistry

A = PermissionAction


# ── config: PermissionPolicy parsing & resolution ──────────────────


class TestPermissionPolicyParsing:
    def test_empty_is_none(self) -> None:
        assert PermissionPolicy.from_dict(None) is None
        assert PermissionPolicy.from_dict({}) is None

    def test_parses_fields(self) -> None:
        p = PermissionPolicy.from_dict(
            {
                "default": "elicit",
                "allow": ["read_*"],
                "deny": ["*_delete"],
                "elicit": ["write_*"],
                "elicitUnavailable": "allow",
            }
        )
        assert p is not None
        assert p.default is A.ELICIT
        assert p.allow == ["read_*"]
        assert p.deny == ["*_delete"]
        assert p.elicit == ["write_*"]
        assert p.elicit_unavailable is A.ALLOW

    def test_snake_case_elicit_unavailable(self) -> None:
        p = PermissionPolicy.from_dict({"elicit_unavailable": "deny", "deny": ["x"]})
        assert p is not None and p.elicit_unavailable is A.DENY

    def test_invalid_action_rejected(self) -> None:
        with pytest.raises(ValueError):
            PermissionPolicy.from_dict({"default": "maybe"})

    def test_unset_scalars_are_none(self) -> None:
        p = PermissionPolicy.from_dict({"deny": ["x"]})
        assert p is not None
        assert p.default is None
        assert p.elicit_unavailable is None


class TestResolvedPolicyPrecedence:
    def _rp(self, **kw: Any) -> ResolvedPolicy:
        return ResolvedPolicy(
            default=kw.get("default", A.ALLOW),
            allow=tuple(kw.get("allow", ())),
            deny=tuple(kw.get("deny", ())),
            elicit=tuple(kw.get("elicit", ())),
            elicit_unavailable=kw.get("elicit_unavailable", A.DENY),
        )

    def test_deny_beats_elicit_beats_allow(self) -> None:
        rp = self._rp(deny=["danger_*"], elicit=["danger_write"], allow=["danger_read"])
        # A name matching multiple sets resolves to the highest-precedence one.
        assert rp.resolve("danger_x") is A.DENY  # deny only
        rp2 = self._rp(elicit=["w_*"], allow=["w_read"])
        assert rp2.resolve("w_read") is A.ELICIT  # elicit beats allow

    def test_unmatched_uses_default(self) -> None:
        assert self._rp(default=A.ELICIT, allow=["read_*"]).resolve("write_x") is A.ELICIT
        assert self._rp(default=A.ALLOW).resolve("anything") is A.ALLOW

    def test_is_active(self) -> None:
        assert not self._rp().is_active()  # default allow, no patterns → off
        assert not self._rp(allow=["*"]).is_active()  # allow-only is still a no-op gate
        assert self._rp(deny=["x"]).is_active()
        assert self._rp(elicit=["x"]).is_active()
        assert self._rp(default=A.ELICIT).is_active()


class TestMergePolicies:
    def test_off_by_default(self) -> None:
        rp = _merge_policies(None, None, [])
        assert not rp.is_active()
        assert rp.default is A.ALLOW
        assert rp.elicit_unavailable is A.DENY  # secure fallback

    def test_server_overrides_global_scalars(self) -> None:
        g = PermissionPolicy.from_dict({"default": "elicit", "elicitUnavailable": "deny"})
        s = PermissionPolicy.from_dict({"default": "deny"})
        rp = _merge_policies(g, s, [])
        assert rp.default is A.DENY  # server wins
        assert rp.elicit_unavailable is A.DENY  # inherited from global

    def test_pattern_sets_are_unioned(self) -> None:
        g = PermissionPolicy.from_dict({"deny": ["global_*"]})
        s = PermissionPolicy.from_dict({"deny": ["server_*"], "elicit": ["write_*"]})
        rp = _merge_policies(g, s, [])
        assert rp.resolve("global_x") is A.DENY
        assert rp.resolve("server_x") is A.DENY
        assert rp.resolve("write_x") is A.ELICIT

    def test_auto_approve_folds_into_allow(self) -> None:
        g = PermissionPolicy.from_dict({"default": "elicit"})
        rp = _merge_policies(g, None, ["note_lookup", "read_*"])
        assert rp.resolve("note_lookup") is A.ALLOW
        assert rp.resolve("read_x") is A.ALLOW
        assert rp.resolve("write_x") is A.ELICIT  # global default

    def test_effective_policy_via_combiner_config(self) -> None:
        cfg = CombinerConfig(
            permissions=PermissionPolicy.from_dict({"default": "elicit", "deny": ["*_delete"]}),
            servers={
                "gws": ServerConfig(
                    name="gws",
                    auto_approve=["read_*"],
                    permissions=PermissionPolicy.from_dict({"elicit": ["send_*"]}),
                ),
                "plain": ServerConfig(name="plain"),
            },
        )
        gws = cfg.effective_policy("gws")
        assert gws.resolve("read_mail") is A.ALLOW  # auto_approve
        assert gws.resolve("send_mail") is A.ELICIT  # server elicit
        assert gws.resolve("thread_delete") is A.DENY  # global deny
        assert gws.resolve("random") is A.ELICIT  # global default
        # A server with no block still inherits the *global* policy.
        assert cfg.effective_policy("plain").resolve("random") is A.ELICIT
        assert cfg.effective_policy("plain").resolve("x_delete") is A.DENY

    def test_unknown_server_is_global_only(self) -> None:
        cfg = CombinerConfig(permissions=None, servers={})
        assert not cfg.effective_policy("nope").is_active()


# ── runtime: per-session grant cache ───────────────────────────────


class TestSessionGrants:
    def test_grant_and_check(self) -> None:
        reg = SessionRegistry()
        assert not reg.is_granted("s1", "gws/send_mail")
        reg.grant("s1", "gws/send_mail")
        assert reg.is_granted("s1", "gws/send_mail")
        # scoped per session and per key
        assert not reg.is_granted("s2", "gws/send_mail")
        assert not reg.is_granted("s1", "gws/other")

    def test_clear(self) -> None:
        reg = SessionRegistry()
        reg.grant("s1", "a")
        reg.clear_granted("s1")
        assert not reg.is_granted("s1", "a")


# ── middleware: the gate ───────────────────────────────────────────


def _elicit_ctx(session_id: str | None, *, result: Any = None, exc: Exception | None = None):
    """A fake FastMCP request context with a scripted `elicit`."""
    calls: list[str] = []

    async def elicit(message: str, response_type: Any = None) -> Any:
        calls.append(message)
        if exc is not None:
            raise exc
        return result

    return SimpleNamespace(session_id=session_id, elicit=elicit, _calls=calls)


def _rp(**kw: Any) -> ResolvedPolicy:
    return ResolvedPolicy(
        default=kw.get("default", A.ALLOW),
        allow=tuple(kw.get("allow", ())),
        deny=tuple(kw.get("deny", ())),
        elicit=tuple(kw.get("elicit", ())),
        elicit_unavailable=kw.get("elicit_unavailable", A.DENY),
    )


class TestEnforcePermission:
    @pytest.fixture(autouse=True)
    def _clean_grants(self):
        from mcp_combiner.runtime import RUNTIME

        RUNTIME.sessions.clear_granted("sess")
        yield
        RUNTIME.sessions.clear_granted("sess")

    async def test_allow_passes(self) -> None:
        ctx = _elicit_ctx("sess")
        # allow-matched → no raise, no elicit
        await permissions.enforce(ctx, "gws", "read_mail", _rp(allow=["read_*"]))
        assert ctx._calls == []

    async def test_deny_raises(self) -> None:
        ctx = _elicit_ctx("sess")
        with pytest.raises(ToolError, match="denied by the combiner permission policy"):
            await permissions.enforce(ctx, "gws", "acct_delete", _rp(deny=["*_delete"]))
        assert ctx._calls == []  # deny never prompts

    async def test_elicit_allow_once_passes_without_caching(self) -> None:
        from mcp_combiner.runtime import RUNTIME

        ctx = _elicit_ctx("sess", result=AcceptedElicitation(data="Allow once"))
        await permissions.enforce(ctx, "gws", "send_mail", _rp(elicit=["send_*"]))
        assert len(ctx._calls) == 1
        assert not RUNTIME.sessions.is_granted("sess", "gws/send_mail")  # not cached

    async def test_elicit_allow_for_session_caches(self) -> None:
        from mcp_combiner.runtime import RUNTIME

        policy = _rp(elicit=["send_*"])
        ctx = _elicit_ctx("sess", result=AcceptedElicitation(data="Allow for session"))
        await permissions.enforce(ctx, "gws", "send_mail", policy)
        assert RUNTIME.sessions.is_granted("sess", "gws/send_mail")

        # A second call is not re-prompted (grant short-circuits).
        ctx2 = _elicit_ctx("sess", result=AcceptedElicitation(data="Deny"))
        await permissions.enforce(ctx2, "gws", "send_mail", policy)
        assert ctx2._calls == []

    async def test_elicit_deny_raises(self) -> None:
        ctx = _elicit_ctx("sess", result=AcceptedElicitation(data="Deny"))
        with pytest.raises(ToolError, match="denied by the user"):
            await permissions.enforce(ctx, "gws", "send_mail", _rp(elicit=["send_*"]))

    async def test_elicit_declined_raises(self) -> None:
        ctx = _elicit_ctx("sess", result=DeclinedElicitation())
        with pytest.raises(ToolError, match="denied by the user"):
            await permissions.enforce(ctx, "gws", "send_mail", _rp(elicit=["send_*"]))

    async def test_elicit_cancelled_raises(self) -> None:
        ctx = _elicit_ctx("sess", result=CancelledElicitation())
        with pytest.raises(ToolError, match="denied by the user"):
            await permissions.enforce(ctx, "gws", "send_mail", _rp(elicit=["send_*"]))

    async def test_elicit_unavailable_fallback_deny(self) -> None:
        ctx = _elicit_ctx("sess", exc=RuntimeError("no elicitation capability"))
        with pytest.raises(ToolError, match="denied by the user"):
            await permissions.enforce(
                ctx, "gws", "send_mail", _rp(elicit=["send_*"], elicit_unavailable=A.DENY)
            )

    async def test_elicit_unavailable_fallback_allow(self) -> None:
        ctx = _elicit_ctx("sess", exc=RuntimeError("no elicitation capability"))
        # fallback=allow → the call proceeds despite the failed prompt
        await permissions.enforce(
            ctx, "gws", "send_mail", _rp(elicit=["send_*"], elicit_unavailable=A.ALLOW)
        )
