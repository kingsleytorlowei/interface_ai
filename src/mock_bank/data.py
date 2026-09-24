"""Synthetic members. All names and numbers are fictional."""

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass
class Account:
    suffix: str
    description: str
    number: str
    balance: Decimal
    available: Decimal


@dataclass
class Member:
    member_id: str
    name: str
    accounts: list[Account]
    alert: str | None = None  # shows a MEMBER ALERT interstitial before the detail screen
    restricted: bool = False  # employee account: tellers get a permission denial


SUB_ACCOUNT_TYPES = ["Money Market", "Holiday Club", "Share Certificate"]


def seed() -> dict[str, Member]:
    def acct(suffix: str, desc: str, number: str, bal: str, avail: str | None = None) -> Account:
        return Account(suffix, desc, number, Decimal(bal), Decimal(avail or bal))

    members = [
        Member("12345", "Jane Q. Sample", [
            acct("00", "Share Savings", "4417", "4210.37"),
            acct("10", "Share Draft Checking", "4418", "1022.10", "972.10"),
        ]),
        Member("23456", "Robert T. Example", [
            acct("00", "Share Savings", "5521", "815.00"),
        ], alert="Address change pending verification. Confirm identity before servicing."),
        Member("34567", "Maria L. Placeholder", [
            acct("00", "Share Savings", "6610", "12050.00"),
        ], restricted=True),
        Member("45678", "Samuel P. Testcase", [  # a second happy path, for verification
            acct("00", "Share Savings", "7702", "318.45"),
        ]),
    ]
    return {m.member_id: m for m in members}


@dataclass
class Store:
    members: dict[str, Member] = field(default_factory=seed)
    pending: dict[str, dict[str, str]] = field(default_factory=dict)  # review token -> form
    next_confirmation: int = 100231
