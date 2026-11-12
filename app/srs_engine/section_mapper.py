"""Maps requirement types to SRS sections (the template manifest holds the headings)."""
SECTION_FOR_TYPE = {
    "FR": "functional_requirements", "NFR": "non_functional_requirements",
    "SEC": "security_requirements", "BUS_RULE": "business_rules", "BR": "business_rules",
    "INT": "integrations", "CON": "other_requirements", "ASM": "other_requirements",
    "DEP": "other_requirements", "REPORT": "other_requirements", "UI_SCREEN": "other_requirements",
    "TECH": "other_requirements", "SCOPE_EXCLUSION": "other_requirements",
    "FUTURE_SCOPE": "other_requirements",
}


def section_for(req_type: str) -> str:
    return SECTION_FOR_TYPE.get(req_type, "other_requirements")
