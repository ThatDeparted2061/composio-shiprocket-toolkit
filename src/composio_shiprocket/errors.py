"""Errors an agent can act on.

Every failure the toolkit raises says what the agent should do next, not
just what went wrong. An LLM that gets "HTTP 422" guesses; one that gets
``FIX_INPUT: delivery_postcode 99999 is not serviceable`` changes its input.
"""

from __future__ import annotations

from enum import Enum


class Action(str, Enum):
    """What the caller should do about a failure."""

    RETRY_LATER = "retry_later"  # transient: throttled, upstream down, timed out
    REAUTHENTICATE = "reauthenticate"  # credentials rejected even after a fresh login
    FIX_INPUT = "fix_input"  # the request is invalid; retrying it unchanged will fail again
    NOT_FOUND = "not_found"  # the order, shipment or AWB does not exist
    GIVE_UP = "give_up"  # permanent: forbidden, plan limits, unexpected response


class ShiprocketError(Exception):
    """A failed Shiprocket call, classified for the agent."""

    def __init__(self, action: Action, message: str, *, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.action = action
        self.message = message
        self.status = status
        self.retry_after = retry_after

    def to_agent(self) -> dict:
        """The structured error returned to the model as the tool result."""
        hints = {
            Action.RETRY_LATER: "Temporary problem on Shiprocket's side. Wait and call the same tool again with the same arguments.",
            Action.REAUTHENTICATE: "The Shiprocket credentials were rejected. Ask the user to reconnect the Shiprocket account; do not retry.",
            Action.FIX_INPUT: "The request was invalid. Correct the arguments named in the message, then try again.",
            Action.NOT_FOUND: "The referenced order, shipment or AWB does not exist. Check the ID; do not retry unchanged.",
            Action.GIVE_UP: "This cannot succeed by retrying. Report the message to the user.",
        }
        out = {"error": self.message, "action": self.action.value, "hint": hints[self.action]}
        if self.status is not None:
            out["status"] = self.status
        if self.retry_after is not None:
            out["retry_after_seconds"] = self.retry_after
        return out

    def __repr__(self) -> str:
        return f"ShiprocketError({self.action.value}, {self.message!r}, status={self.status})"


def classify(status: int, body: str) -> Action:
    """Map an HTTP status (and, for 4xx, the body) to an action."""
    if status == 429 or status >= 500:
        return Action.RETRY_LATER
    if status == 401:
        return Action.REAUTHENTICATE
    if status == 404:
        return Action.NOT_FOUND
    if status in (400, 422):
        return Action.FIX_INPUT
    return Action.GIVE_UP
