from __future__ import annotations

from typing import TypeAlias

from .adp_recruiting import AdpRecruitingAdapter
from .apple import AppleAdapter
from .base import JobAdapter
from .discovered_api import DiscoveredApiAdapter
from .eightfold import EightfoldAdapter
from .html_multi_index import HtmlMultiIndexAdapter
from .html_paginated import HtmlPaginatedAdapter
from .lever import LeverAdapter
from .oracle_hcm import OracleHcmAdapter
from .phenom import PhenomAdapter
from .stealth_html import StealthHtmlAdapter
from .successfactors_rmk import SuccessFactorsRmkAdapter
from .unsupported import UnsupportedAdapter
from .workday import WorkdayAdapter

AdapterType: TypeAlias = type[JobAdapter]
ADAPTERS: dict[str, AdapterType] = {
    "workday": WorkdayAdapter,
    "successfactors_rmk": SuccessFactorsRmkAdapter,
    "lever": LeverAdapter,
    "oracle_hcm": OracleHcmAdapter,
    "phenom": PhenomAdapter,
    "html_paginated": HtmlPaginatedAdapter,
    "html_multi_index": HtmlMultiIndexAdapter,
    "discovered_api": DiscoveredApiAdapter,
    "stealth_html": StealthHtmlAdapter,
    "adp_recruiting": AdpRecruitingAdapter,
    "apple": AppleAdapter,
    "eightfold": EightfoldAdapter,
    "unsupported": UnsupportedAdapter,
}


def adapter_class(name: str) -> AdapterType:
    try:
        return ADAPTERS[name]
    except KeyError as exc:
        raise ValueError(f"Unknown adapter: {name}") from exc
