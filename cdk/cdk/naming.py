"""Deterministic, stack-prefix-scoped physical resource names."""

import re


_STACK_PREFIX_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,47}$")


def resource_prefix(node) -> str:
    """Return a service-safe lowercase prefix derived from eda:stack_prefix."""
    raw = str(node.try_get_context("eda:stack_prefix") or "Eda")
    if not _STACK_PREFIX_PATTERN.fullmatch(raw):
        raise ValueError(
            "eda:stack_prefix must start with a letter, contain only letters, "
            "digits, or hyphens, and be 48 characters or fewer"
        )
    return raw.lower()


def ssm_path(node, suffix: str) -> str:
    return f"/{resource_prefix(node)}/{suffix.lstrip('/')}"


def foundation_export_name(node, component: str, output_name: str) -> str:
    """Return a stable CloudFormation Export name for shared Foundation values."""
    return f"{resource_prefix(node)}:{component}:{output_name}"
