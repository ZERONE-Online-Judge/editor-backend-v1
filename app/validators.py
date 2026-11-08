"""Resolve selected validators without rewriting authored documents."""


def validator_names(doc):
    """Keep the basic validator first and each selected source only once."""
    return list(dict.fromkeys(name for name in [doc["validator"], *doc["extra_validators"]] if name))
