"""Tenant variants of the same vendor product ("CoreOne Teller"), a stand-in for two
institutions running differently configured and versioned installs of one core system.

The flows are identical; labels, branding, product version and the accounts table layout differ.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Variant:
    key: str
    institution: str
    product_version: str
    color: str
    member_id_label: str
    search_label: str
    name_label: str
    balance_column: str
    show_available_column: bool


VARIANTS = {
    v.key: v
    for v in [
        Variant(
            key="pinnacle",
            institution="Pinnacle Community Credit Union",
            product_version="7.2.3",
            color="#003366",
            member_id_label="Member ID",
            search_label="Search",
            name_label="Name:",
            balance_column="Balance",
            show_available_column=False,
        ),
        Variant(
            key="riverbend",
            institution="Riverbend Federal Credit Union",
            product_version="7.4.1",
            color="#5a2d0c",
            member_id_label="Member #",
            search_label="Find",
            name_label="Member Name:",
            balance_column="Current Bal.",
            show_available_column=True,
        ),
    ]
}
