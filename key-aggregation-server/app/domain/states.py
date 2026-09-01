from enum import StrEnum


class RoundState(StrEnum):
    CREATED = "CREATED"
    REGISTRATION_OPEN = "REGISTRATION_OPEN"
    KEY_SETUP = "KEY_SETUP"
    UPDATE_COLLECTION = "UPDATE_COLLECTION"
    AGGREGATING = "AGGREGATING"
    RESULT_READY = "RESULT_READY"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ABORTED = "ABORTED"
    EXPIRED = "EXPIRED"


TERMINAL_STATES = {RoundState.COMPLETED, RoundState.FAILED, RoundState.ABORTED, RoundState.EXPIRED}
ACTIVE_STATES = set(RoundState) - TERMINAL_STATES
ALLOWED_TRANSITIONS = {
    RoundState.CREATED: {RoundState.REGISTRATION_OPEN},
    RoundState.REGISTRATION_OPEN: {RoundState.KEY_SETUP},
    RoundState.KEY_SETUP: {RoundState.UPDATE_COLLECTION},
    RoundState.UPDATE_COLLECTION: {RoundState.AGGREGATING},
    RoundState.AGGREGATING: {RoundState.RESULT_READY},
    RoundState.RESULT_READY: {RoundState.COMPLETED},
}


def transition_allowed(source: RoundState, target: RoundState) -> bool:
    return target in ALLOWED_TRANSITIONS.get(source, set()) or (
        source in ACTIVE_STATES and target in {RoundState.FAILED, RoundState.ABORTED, RoundState.EXPIRED}
    )
